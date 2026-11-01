from __future__ import annotations

import inspect
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from .context import redact_text, sanitize_value, utc_now_iso
from .policy import RecoveryPolicy, SPEND_ACTIONS, TERMINAL_ACTIONS

AsyncCallback = Callable[..., Awaitable[Mapping[str, Any]]]

_NO_ARGS = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
_SECRET_ARGUMENT_RE = re.compile(
    r"(?i)(cookie|password|api[_\- ]?key|authorization|token)"
)

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "restart_browser",
        "description": (
            "Restart the managed browser context for the current account. "
            "Use when the browser/context is dead or every page cannot load. "
            "Does not submit or generate a video."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "switch_page",
        "description": (
            "Switch to the task's persisted Doubao conversation tab in the "
            "current account window, or open exactly that URL if missing. "
            "Never guesses another task tab. Optional url must match the binding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Optional exact URL persisted for this task.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "refresh_page",
        "description": (
            "Reload the task's bound conversation page. Use for stale DOM, "
            "stuck loading, or wrong UI state. Does not submit or change inputs."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "inspect_dom",
        "description": (
            "Read the latest task page DOM state, controls, upload markers, "
            "video-card count, generation state, and failed network resources. "
            "Text only; screenshots are never used."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "dismiss_popup",
        "description": (
            "Dismiss only nuisance dialogs with an explicit close control. "
            "Never dismisses login, CAPTCHA, generation confirmation, or the "
            "video detail/player state."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "locate_task_page",
        "description": (
            "Re-open or foreground the exact persisted task page and persist "
            "its live session URL/id. Does not fall back to another task or tab."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "verify_task_status",
        "description": (
            "Read the persisted job state plus current page state and verify "
            "whether the original flow has finished or is still running. "
            "Never changes task state."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "check_assets",
        "description": (
            "Verify the current task's referenced image assets still exist and "
            "match the persisted asset records. Read-only."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "recall_existing_result",
        "description": (
            "Use the existing recall worker to bind/download an already generated "
            "result for the current job. Prefer before regenerating whenever a "
            "result may already exist. Never overwrites an archived video."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "download_video",
        "description": (
            "Download the current task's already generated, bound video card "
            "through the existing recall/download path. This never submits a "
            "generation and never guesses a card."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "retry_h3_push",
        "description": (
            "Retry delivery of the existing archived video to H3 through the "
            "existing outbox endpoint. Never generates a video."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "restart_auto",
        "description": (
            "Prepare and submit the current provisionally-unsubmitted task again "
            "in automatic mode. Only call when evidence proves no submission and "
            "no quota spend. May consume Doubao quota once it runs."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "regenerate",
        "description": (
            "Clone the failed job into a NEW queued job and let the scheduler run "
            "it. Only for a provably unsubmitted task, within the spend budget. "
            "Never overwrites existing archived videos."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "release_account",
        "description": (
            "Release a stale machine lane owned by this job without cancelling "
            "the task or deleting results. Cannot release another task's lane. "
            "The recovery lease is released when this recovery run ends."
        ),
        "input_schema": _NO_ARGS,
    },
]


@dataclass
class RecoveryBackend:
    restart_browser: AsyncCallback
    refresh_page: AsyncCallback
    recall_existing_result: AsyncCallback
    retry_h3_push: AsyncCallback
    restart_auto: AsyncCallback | None = None
    regenerate: AsyncCallback | None = None
    switch_page: AsyncCallback | None = None
    inspect_dom: AsyncCallback | None = None
    dismiss_popup: AsyncCallback | None = None
    locate_task_page: AsyncCallback | None = None
    verify_task_status: AsyncCallback | None = None
    check_assets: AsyncCallback | None = None
    download_video: AsyncCallback | None = None
    release_account: AsyncCallback | None = None


class RecoveryToolExecutor:
    def __init__(self, backend: RecoveryBackend, policy: RecoveryPolicy):
        self.backend = backend
        self.policy = policy
        self._attempts: Counter[str] = Counter()
        self._spend_used = 0

    async def execute(
        self,
        action: str,
        context: Mapping[str, Any],
        arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        args = dict(arguments or {})
        record: dict[str, Any] = {
            "name": action,
            "arguments": sanitize_value(args),
            "executed_at": utc_now_iso(),
            "executed": False,
            "success": False,
            "verified": False,
            "recovery_complete": False,
            "detail": "",
        }
        unsafe_arg = self._unsafe_argument(args)
        if unsafe_arg:
            record["detail"] = f"工具参数包含疑似敏感字段 {unsafe_arg}，已拒绝执行"
            return record

        decision = self.policy.decide(action, context)
        if not decision.allowed:
            record["detail"] = decision.reason
            return record

        if sum(self._attempts.values()) >= self.policy.max_actions():
            record["detail"] = "已达本次异常的工具动作上限，请停止自动恢复"
            return record

        if self._attempts[action] >= self.policy.max_attempts_per_action():
            record["detail"] = (
                f"动作 {action} 已达到本次恢复的重复执行上限，请切换方案"
            )
            return record

        if action in SPEND_ACTIONS:
            if self._spend_used >= self.policy.spend_budget:
                record["detail"] = (
                    "额度消耗预算已用尽，禁止继续重新生成或重新提交"
                )
                record["spend_budget"] = self.policy.spend_budget
                record["spend_used"] = self._spend_used
                return record
            self._spend_used += 1
            record["spend_attempt"] = self._spend_used

        callback = getattr(self.backend, action, None)
        if callback is None:
            record["detail"] = "恢复后端未配置该动作"
            return record

        self._attempts[action] += 1
        record["executed"] = True
        record["attempt"] = self._attempts[action]
        try:
            result = await self._invoke(callback, str(context.get("job_id") or ""), args)
            normalized = dict(result or {})
            success = bool(normalized.get("success"))
            verified = bool(normalized.get("verified"))
            terminal_default = (
                action in TERMINAL_ACTIONS and success and verified
            )
            record.update(
                {
                    "success": success,
                    "verified": verified,
                    "recovery_complete": bool(
                        normalized.get("recovery_complete", terminal_default)
                    ),
                    "detail": redact_text(normalized.get("detail") or "", 800),
                    "result": sanitize_value(normalized),
                }
            )
            for key in ("status", "accepted", "new_job_id"):
                if normalized.get(key) is not None:
                    record[key] = sanitize_value(normalized[key])
        except Exception as exc:
            record["detail"] = redact_text(
                f"恢复动作失败：{type(exc).__name__}: {exc}", 800
            )
        return record

    @staticmethod
    def _unsafe_argument(arguments: Mapping[str, Any]) -> str:
        for key in arguments:
            if _SECRET_ARGUMENT_RE.search(str(key)):
                return redact_text(key, 80)
        return ""

    @staticmethod
    async def _invoke(
        callback: AsyncCallback, jobid: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Call old one-argument callbacks without masking errors inside them."""
        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            return await callback(jobid)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
        ]
        accepts_varargs = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values()
        )
        if accepts_varargs or len(positional) >= 2:
            return await callback(jobid, arguments)
        return await callback(jobid)

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from .context import redact_text, utc_now_iso
from .policy import RecoveryPolicy

AsyncCallback = Callable[[str], Awaitable[Mapping[str, Any]]]

_NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "restart_browser",
        "description": (
            "Restart the existing managed browser for the current account. "
            "Use when the browser/context is dead or pages cannot load. "
            "Does not submit or generate a video."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "refresh_page",
        "description": (
            "Reload the existing task page. Use for stale DOM, stuck loading, or "
            "wrong UI state. Does not submit or change prompts/assets."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "recall_existing_result",
        "description": (
            "Use the existing recall worker to bind/download an already generated "
            "result for the current job. Prefer this over regenerating whenever a "
            "result may already exist. Never overwrites an archived video."
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
            "Restart the current job from the beginning in automatic mode. "
            "Only allowed when the task provably never spent quota (it failed "
            "before submission). This may consume Doubao quota once it runs."
        ),
        "input_schema": _NO_ARGS,
    },
    {
        "name": "regenerate",
        "description": (
            "Clone the failed job into a NEW queued job and let the scheduler run "
            "it. Use when the current job is unrecoverable and no quota was "
            "spent. Never overwrites existing archived videos."
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


class RecoveryToolExecutor:
    def __init__(self, backend: RecoveryBackend, policy: RecoveryPolicy):
        self.backend = backend
        self.policy = policy

    async def execute(
        self, action: str, context: Mapping[str, Any]
    ) -> dict[str, Any]:
        decision = self.policy.decide(action, context)
        record: dict[str, Any] = {
            "name": action,
            "executed_at": utc_now_iso(),
            "executed": decision.allowed,
            "success": False,
            "verified": False,
            "detail": decision.reason,
        }
        if not decision.allowed:
            return record

        callback = getattr(self.backend, action, None)
        if callback is None:
            record["detail"] = "恢复后端未配置该动作"
            return record
        try:
            result = await callback(str(context.get("job_id") or ""))
            normalized = dict(result or {})
            record.update(
                {
                    "success": bool(normalized.get("success")),
                    "verified": bool(normalized.get("verified")),
                    "detail": redact_text(normalized.get("detail") or "", 500),
                }
            )
            if normalized.get("status"):
                record["status"] = redact_text(normalized["status"], 120)
            if normalized.get("accepted") is not None:
                record["accepted"] = bool(normalized.get("accepted"))
            if normalized.get("new_job_id"):
                record["new_job_id"] = redact_text(normalized["new_job_id"], 120)
        except Exception as exc:
            record["detail"] = redact_text(
                f"恢复动作失败：{type(exc).__name__}: {exc}", 500
            )
        return record

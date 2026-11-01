from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

SAFE_ACTIONS = {
    "restart_browser",
    "switch_page",
    "refresh_page",
    "inspect_dom",
    "dismiss_popup",
    "locate_task_page",
    "verify_task_status",
    "check_assets",
    "recall_existing_result",
    "download_video",
    "retry_h3_push",
    "restart_auto",
    "regenerate",
    "release_account",
}

OBSERVATION_ACTIONS = {
    "inspect_dom",
    "verify_task_status",
    "check_assets",
}

NON_TERMINAL_ACTIONS = {
    "restart_browser",
    "switch_page",
    "refresh_page",
    "inspect_dom",
    "dismiss_popup",
    "locate_task_page",
    "verify_task_status",
    "check_assets",
    "release_account",
}

TERMINAL_ACTIONS = SAFE_ACTIONS - NON_TERMINAL_ACTIONS

HARD_BLOCK_CODES = {
    "LOGIN_REQUIRED",
    "VERIFICATION_REQUIRED",
    "ACCOUNT_LIMIT",
}

SPEND_SENSITIVE_CODES = {
    "SUBMISSION_UNCONFIRMED",
    "CONFIRM_REQUIRED",
    "CONFIRM_NOT_SENT",
    "MANUAL_SEND_INTERRUPTED",
    "INTERRUPTED_REVIEW_REQUIRED",
    "GENERATION_FAILED",
    "DURATION_MISMATCH",
    "RESULT_BINDING_MISSING",
}

CONFIRMED_UNSUBMITTED_CODES = {
    "CONFIRM_NOT_SENT",
}

SPEND_ACTIONS = {"restart_auto", "regenerate"}

# "submitting" is deliberately absent: a click may already have reached Doubao.
PRE_SUBMIT_STAGES = {
    "",
    "queued",
    "opening",
    "configuring",
    "uploading",
}

_SPEND_UNCERTAIN_RE = re.compile(
    r"无法确认是否发送成功|无法证明.*(?:提交|发出)|提交状态不确定|"
    r"确认语.*(?:不确定|无法确认)|可能已(?:提交|扣费|发送|消耗)"
)
_SECRET_RE = re.compile(
    r"(?i)(cookie|password|api[_\- ]?key|authorization|token)\s*[:=]\s*\S+"
)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_nonnegative_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


class RecoveryPolicy:
    """Policy boundary for autonomous recovery.

    Claude chooses the recovery path. The code only enforces irreversible safety
    boundaries: human login/CAPTCHA, account ownership, result preservation, and
    a bounded number of potentially quota-spending attempts.
    """

    def __init__(self, mode: str, spend_budget: int | None = None):
        self.mode = mode
        self.spend_budget = (
            max(0, int(spend_budget))
            if spend_budget is not None
            else _env_nonnegative_int("DOUBAO_AGENT_SPEND_BUDGET", 1)
        )

    def can_act(self, context: Mapping[str, Any]) -> bool:
        if self.mode == "diagnose_only":
            return False
        return not self.manual_reason(context)

    def max_actions(self) -> int:
        default = 12 if self.mode == "autonomous" else 4
        return _env_int("DOUBAO_AGENT_MAX_ACTIONS", default)

    def max_attempts_per_action(self) -> int:
        default = 3 if self.mode == "autonomous" else 1
        return _env_int("DOUBAO_AGENT_MAX_ACTION_ATTEMPTS", default)

    def manual_reason(self, context: Mapping[str, Any]) -> str:
        code = str(context.get("error_code") or "")
        message = str(context.get("error_message") or "")
        dom = context.get("dom_state") or {}
        if code in HARD_BLOCK_CODES:
            if code == "ACCOUNT_LIMIT":
                return "账号额度不足必须由人工处理，禁止换号或绕过限制"
            return f"{code} 必须由人工完成，禁止自动恢复"
        if context.get("account_owned_by_job") is False:
            owner = str(context.get("account_owner_job_id") or "")
            suffix = f"（当前占用任务 {owner}）" if owner else ""
            return f"账号当前由其他任务占用{suffix}，为避免两个任务抢占已停止自动操作"
        if dom.get("login_required") or re.search(
            r"登录后才能|请先登录|扫码登录", message
        ):
            return "登录失效必须由人工完成登录"
        if dom.get("verification_required") or re.search(
            r"人机验证|安全验证|验证码|captcha", message, re.I
        ):
            return "验证码或安全验证必须由人工完成"
        if dom.get("account_limit") or re.search(
            r"额度不足|次数已用完|已达上限|请开通会员", message
        ):
            return "账号额度不足必须由人工处理，禁止换号或绕过限制"
        if _SECRET_RE.search(message):
            return "异常信息疑似包含敏感凭据，请人工核查"
        return ""

    def decide(self, action: str, context: Mapping[str, Any]) -> PolicyDecision:
        if action not in SAFE_ACTIONS:
            return PolicyDecision(False, f"未知或未授权的恢复工具：{action}")
        if self.mode == "diagnose_only":
            return PolicyDecision(False, "当前为 diagnose_only，禁止执行恢复动作")
        manual = self.manual_reason(context)
        if manual:
            return PolicyDecision(False, manual)

        if action in SPEND_ACTIONS:
            blocked = self.spend_block_reason(context)
            if blocked:
                return PolicyDecision(False, blocked)

        if action in {"recall_existing_result", "download_video"}:
            if context.get("has_archived_video"):
                return PolicyDecision(
                    False, "已有归档视频，禁止通过召回或下载覆盖已生成内容"
                )
            if not context.get("page_url"):
                return PolicyDecision(False, "缺少已绑定的原任务会话页，不能召回")
        if action == "retry_h3_push":
            if not context.get("has_archived_video"):
                return PolicyDecision(False, "没有归档视频，禁止执行 H3 回推")
            if not context.get("has_result_push_target"):
                return PolicyDecision(False, "缺少 H3 回推目标，禁止执行")
        if action == "regenerate" and context.get("has_archived_video"):
            return PolicyDecision(False, "已有归档视频，禁止重新生成覆盖结果")
        if action in {
            "restart_browser",
            "switch_page",
            "refresh_page",
            "inspect_dom",
            "dismiss_popup",
            "locate_task_page",
            "release_account",
        }:
            if not context.get("account_id"):
                return PolicyDecision(False, "任务所属账号已删除，无法操作浏览器")
        if action in {"switch_page", "locate_task_page"} and not context.get(
            "page_url"
        ):
            return PolicyDecision(False, "任务未持久化具体会话页，禁止猜测其他标签页")
        return PolicyDecision(True, "动作符合恢复策略")

    def spend_block_reason(self, context: Mapping[str, Any]) -> str:
        code = str(context.get("error_code") or "")
        stage = str(context.get("last_operational_stage") or "")
        quota_state = str(context.get("quota_state") or "").lower()
        message = str(context.get("error_message") or "")
        submission_state = str(
            context.get("submission_state") or ""
        ).strip().lower()
        if quota_state in {"unknown", "spent", "consumed"}:
            return "无法证明额度尚未消耗，禁止自动重新生成"
        confirmed_unsubmitted = self.mode == "autonomous" and (
            code in CONFIRMED_UNSUBMITTED_CODES
            or submission_state
            in {"not_submitted", "unsubmitted", "confirmed_not_submitted"}
        )
        if _SPEND_UNCERTAIN_RE.search(message):
            return "提交或确认状态不确定，禁止自动重新生成"
        if code in SPEND_SENSITIVE_CODES and not confirmed_unsubmitted:
            return f"错误码 {code} 可能已消耗额度，禁止自动重新生成"

        stage_is_pre_submit = bool(stage) and stage in PRE_SUBMIT_STAGES
        if not confirmed_unsubmitted and not stage_is_pre_submit:
            return "无法证明任务尚未提交且未消耗额度，禁止自动重新生成"
        if (
            not confirmed_unsubmitted
            and stage
            and stage not in PRE_SUBMIT_STAGES
        ):
            return f"任务已进入 {stage} 阶段，可能已消耗额度，禁止自动重新生成"
        return ""

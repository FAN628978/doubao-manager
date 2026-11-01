from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

# 代理可用的全部恢复工具。收编顺序即“代理可以做的全部事情”。
SAFE_ACTIONS = {
    "restart_browser",
    "refresh_page",
    "recall_existing_result",
    "retry_h3_push",
    "restart_auto",
    "regenerate",
}

# 不可自动跨越的红线：即使放开权限，这些情况也只能由人处理。
# 其余异常代理都可以自行尝试多个恢复动作。
HARD_BLOCK_CODES = {
    # 登录 / 验证码必须由人完成，自动化无法也不应代劳。
    "LOGIN_REQUIRED",
    "VERIFICATION_REQUIRED",
    # 账号额度不足：可以重试原账号，但不能换号绕过限制。
    "ACCOUNT_LIMIT",
}

# 当额度可能已经被消耗（不确定是否出片/扣费）时，禁止再花钱重新生成。
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

# 会花掉额度、产生新视频的动作。
SPEND_ACTIONS = {"restart_auto", "regenerate"}

# 预提交阶段：此时重新执行不会产生重复成片。
PRE_SUBMIT_STAGES = {
    "",
    "queued",
    "opening",
    "configuring",
    "uploading",
    "submitting",
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


class RecoveryPolicy:
    """放开式的自动恢复策略。

    设计取舍：
    - 默认允许代理对一个异常连续尝试多个恢复动作，并允许它自行重新生成；
    - 但三条硬边界永远保留：登录/验证码必须人工、额度不足不得换号绕过、
      额度可能已被消耗时不得再自动花钱重发；
    - 另有“硬性诊断期”保护：某些明显不可自动恢复的错误码直接不给代理执行工具。
    """

    def __init__(self, mode: str):
        self.mode = mode

    # ---- 代理是否被允许动手 ----
    def can_act(self, context: Mapping[str, Any]) -> bool:
        if self.mode == "diagnose_only":
            return False
        return not self.manual_reason(context)

    def max_actions(self) -> int:
        """一次异常最多可连续执行的恢复动作数。"""
        return _env_int("DOUBAO_AGENT_MAX_ACTIONS", 4)

    # ---- 不可自动处理的硬原因 ----
    def manual_reason(self, context: Mapping[str, Any]) -> str:
        code = str(context.get("error_code") or "")
        message = str(context.get("error_message") or "")
        dom = context.get("dom_state") or {}
        if code in HARD_BLOCK_CODES:
            if code == "ACCOUNT_LIMIT":
                return "账号额度不足必须由人工处理，禁止换号或绕过限制"
            return f"{code} 必须由人工完成，禁止自动恢复"
        if dom.get("login_required") or re.search(r"登录后才能|请先登录|扫码登录", message):
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

    # ---- 单个动作是否允许 ----
    def decide(self, action: str, context: Mapping[str, Any]) -> PolicyDecision:
        if action not in SAFE_ACTIONS:
            return PolicyDecision(False, f"未知或未授权的恢复工具：{action}")
        if self.mode == "diagnose_only":
            return PolicyDecision(False, "当前为 diagnose_only，禁止执行恢复动作")
        manual = self.manual_reason(context)
        if manual:
            return PolicyDecision(False, manual)

        # 花钱类动作的安全门：避免为一次已经花过钱的生成再埋单。
        if action in SPEND_ACTIONS:
            blocked = self.spend_block_reason(context)
            if blocked:
                return PolicyDecision(False, blocked)

        if action == "recall_existing_result":
            if context.get("has_archived_video"):
                return PolicyDecision(False, "已有归档视频，禁止通过召回覆盖已生成内容")
            if not context.get("page_url"):
                return PolicyDecision(False, "缺少已绑定的原任务会话页，不能召回")
        if action == "retry_h3_push":
            if not context.get("has_archived_video"):
                return PolicyDecision(False, "没有归档视频，禁止执行 H3 回推")
            if not context.get("has_result_push_target"):
                return PolicyDecision(False, "缺少 H3 回推目标，禁止执行")
        if action == "regenerate" and context.get("has_archived_video"):
            return PolicyDecision(False, "已有归档视频，禁止重新生成覆盖结果")
        if action in {"restart_browser", "refresh_page"}:
            if not context.get("account_id"):
                return PolicyDecision(False, "任务所属账号已删除，无法操作浏览器")
        return PolicyDecision(True, "动作符合恢复策略")

    # ---- 花钱类动作是否安全 ----
    def spend_block_reason(self, context: Mapping[str, Any]) -> str:
        code = str(context.get("error_code") or "")
        stage = str(context.get("last_operational_stage") or "")
        quota_state = str(context.get("quota_state") or "")
        message = str(context.get("error_message") or "")
        if quota_state == "unknown":
            return "无法判断是否已经扣费，禁止自动重新生成"
        if code in SPEND_SENSITIVE_CODES:
            return f"错误码 {code} 可能已消耗额度，禁止自动重新生成"
        if _SPEND_UNCERTAIN_RE.search(message):
            return "提交或确认状态不确定，禁止自动重新生成"
        if stage and stage not in PRE_SUBMIT_STAGES:
            return f"任务已进入 {stage} 阶段，可能已消耗额度，禁止自动重新生成"
        return ""

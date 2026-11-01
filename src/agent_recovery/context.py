from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

MAX_TEXT = 2000
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"),
    re.compile(r"(?i)(cookie\s*[:=]\s*)[^\r\n]+"),
    re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\bsk-ant-[A-Za-z0-9._-]+\b"),
)
_NETWORK_ERROR_RE = re.compile(
    r"(?:net::ERR_[A-Z0-9_]+|HTTP\s+[45]\d\d|urlopen error[^>]*>)", re.I
)
_PAGE_LOAD_ERROR_RE = re.compile(
    r"(?:net::ERR_[A-Z0-9_]+|page\.goto|页面加载|打开.*页面|navigation)",
    re.I,
)
_BROWSER_START_ERROR_RE = re.compile(
    r"(?:new_page|launch.*browser|browser.*(?:start|launch)|"
    r"启动浏览器|浏览器.*(?:启动|连接|不可用))",
    re.I,
)
_BROWSER_RUNTIME_ERROR_RE = re.compile(
    r"(?:Target page|context or browser|browser has been closed|"
    r"\bpage\.|\bbrowser\b|页面已关闭|浏览器.*(?:关闭|崩溃|断开))",
    re.I,
)


def safe_url(value: Any) -> str:
    """Keep origin/path only; query strings and fragments may contain secrets."""
    try:
        parsed = urlsplit(str(value or ""))
        if not parsed.scheme or not parsed.netloc:
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[:500]
    except Exception:
        return ""


def redact_text(value: Any, limit: int = MAX_TEXT) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(
            lambda match: (
                match.group(1) + "[REDACTED]"
                if match.lastindex
                else "[REDACTED]"
            ),
            text,
        )
    return text[:limit]


def sanitize_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        return {
            redact_text(key, 120): sanitize_value(item, depth=depth + 1)
            for key, item in list(value.items())[:80]
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item, depth=depth + 1) for item in value[:40]]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(value)


async def capture_page_snapshot(page: Any, timeout: float = 1.5) -> dict[str, Any]:
    """Capture text-only DOM state. Screenshots and OCR are never used."""
    if page is None:
        return {"available": False, "reason": "no_page_handle"}
    try:
        if getattr(page, "is_closed", None) and page.is_closed():
            return {"available": False, "reason": "page_closed"}
        snapshot = await asyncio.wait_for(
            page.evaluate(
                r"""() => {
                  const visible = (selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return {exists:false, visible:false, enabled:false};
                    const hidden = el.hidden || getComputedStyle(el).visibility === 'hidden'
                      || getComputedStyle(el).display === 'none';
                    return {exists:true, visible:!hidden,
                      enabled:!el.disabled && el.getAttribute('aria-disabled') !== 'true'};
                  };
                  const lastAssistant = document.querySelector(
                    '[data-testid="receive_message"], [data-testid="message_content"]'
                  );
                  const messages = document.querySelectorAll(
                    '[data-testid="receive_message"], [data-testid="message_content"]'
                  );
                  const assistant = messages.length ? messages[messages.length - 1] : lastAssistant;
                  const uploads = {};
                  for (const el of document.querySelectorAll('[data-slot][data-asset-status]')) {
                    const slot = el.getAttribute('data-slot') || 'unknown';
                    uploads[slot] = {
                      status: el.getAttribute('data-asset-status') || '',
                      count: Number(el.getAttribute('data-count') || 0)
                    };
                  }
                  const input = document.querySelector(
                    '[data-testid="chat_input_input"] [contenteditable="true"],'
                    + '[role="textbox"][contenteditable="true"],div[contenteditable="true"],textarea'
                  );
                  const value = input ? ('value' in input ? input.value : input.innerText || '') : '';
                  let saveButton = document.querySelector(
                    '[data-testid="edit_image_download_button"]'
                  );
                  if (!saveButton) {
                    saveButton = Array.from(
                      document.querySelectorAll('[role="button"],button')
                    ).find((el) => (el.innerText || '').trim() === '保存');
                  }
                  const resources = performance.getEntriesByType('resource')
                    .filter((entry) => Number(entry.responseStatus || 0) >= 400)
                    .slice(-10)
                    .map((entry) => ({
                      status: Number(entry.responseStatus || 0),
                      path: (() => { try { return new URL(entry.name).pathname; } catch (_) { return ''; } })()
                    }));
                  return {
                    url: location.href,
                    title: (document.title || '').slice(0, 200),
                    last_assistant_message: assistant ? (assistant.innerText || '').slice(-2000) : '',
                    buttons: {
                      submit: visible('[data-testid="chat_input_send_button"],'
                        + '[data-testid="chat_input"] button[type="submit"]'),
                      save: saveButton ? {
                        exists: true,
                        visible: !saveButton.hidden
                          && getComputedStyle(saveButton).visibility !== 'hidden'
                          && getComputedStyle(saveButton).display !== 'none',
                        enabled: !saveButton.disabled
                          && saveButton.getAttribute('aria-disabled') !== 'true'
                      } : {exists:false, visible:false, enabled:false}
                    },
                    input: {
                      exists: !!input,
                      enabled: !!input && !input.disabled && input.getAttribute('aria-disabled') !== 'true',
                      prompt_chars: value.trim().length
                    },
                    uploads,
                    attachment_count: document.querySelectorAll(
                      '[data-testid="attachment-image-card"]'
                    ).length,
                    video_card_count: document.querySelectorAll(
                      '[class*="block-video"], [data-testid="video-result-card"]'
                    ).length,
                    running: !!document.querySelector('[data-generation-status="running"]'),
                    completed: !!document.querySelector('[data-generation-status="completed"]'),
                    login_required: /登录后才能|请先登录|扫码登录/.test(document.body?.innerText || ''),
                    verification_required: /人机验证|安全验证|验证码|captcha/i.test(
                      document.body?.innerText || ''
                    ),
                    account_limit: /额度不足|次数已用完|已达上限|请开通会员/.test(
                      document.body?.innerText || ''
                    ),
                    network_errors: resources
                  };
                }"""
            ),
            timeout=timeout,
        )
        if not isinstance(snapshot, dict):
            return {"available": False, "reason": "invalid_snapshot"}
        snapshot["url"] = safe_url(snapshot.get("url"))
        return sanitize_value({"available": True, **snapshot})
    except asyncio.TimeoutError:
        return {"available": False, "reason": "snapshot_timeout"}
    except Exception as exc:
        return {
            "available": False,
            "reason": redact_text(type(exc).__name__, 120),
        }


def error_code_from_exception(exc: BaseException) -> str:
    code = getattr(exc, "code", "")
    if code:
        return str(code)[:120]
    message = redact_text(getattr(exc, "detail", "") or exc)
    if re.search(r"登录", message):
        return "LOGIN_REQUIRED"
    if re.search(r"人机验证|安全验证|验证码|captcha", message, re.I):
        return "VERIFICATION_REQUIRED"
    if re.search(r"额度不足|次数已用完|已达上限|请开通会员", message):
        return "ACCOUNT_LIMIT"
    if re.search(r"无法确认是否发送成功|无法证明.*发出|提交状态", message):
        return "SUBMISSION_UNCONFIRMED"
    if re.search(r"确认语没有发出去|确认生成的消息没有发出去", message):
        return "CONFIRM_NOT_SENT"
    if _PAGE_LOAD_ERROR_RE.search(message):
        return "PAGE_LOAD_FAILED"
    if _BROWSER_START_ERROR_RE.search(message):
        return "BROWSER_START_FAILED"
    if _BROWSER_RUNTIME_ERROR_RE.search(message):
        return "WEB_AUTOMATION_ERROR"
    return type(exc).__name__.upper()


def _network_errors(error_message: str, page_snapshot: Mapping[str, Any] | None) -> list:
    errors: list[Any] = list((page_snapshot or {}).get("network_errors") or [])
    for match in _NETWORK_ERROR_RE.findall(error_message or ""):
        if match not in errors:
            errors.append(match)
    return sanitize_value(errors[-10:])


def build_exception_context(
    *,
    job_id: str,
    account_id: str,
    error_type: str,
    error_code: str,
    error_message: str,
    job_state: Mapping[str, Any] | None,
    recent_events: list[Mapping[str, Any]] | None,
    page_snapshot: Mapping[str, Any] | None,
    recent_network_errors: list[Any] | None = None,
) -> dict[str, Any]:
    state = dict(job_state or {})
    events = [dict(item) for item in (recent_events or [])[-30:]]
    page_data = dict(page_snapshot or {})
    terminal_or_error = {"needs_review", "failed"}
    last_operational_stage = ""
    for event in reversed(events):
        status = str(event.get("status") or "")
        if status and status not in terminal_or_error:
            last_operational_stage = status
            break

    message = redact_text(error_message, 1000)
    network_errors = list(recent_network_errors or [])
    for item in _network_errors(message, page_data):
        if item not in network_errors:
            network_errors.append(item)

    return sanitize_value(
        {
            "job_id": job_id,
            "account_id": account_id or state.get("account_id") or "",
            "current_status": state.get("status") or "",
            "current_phase": state.get("phase") or "",
            "last_operational_stage": last_operational_stage,
            "error_type": error_type,
            "error_code": error_code,
            "error_message": message,
            "recent_events": events,
            "page_url": safe_url(
                page_data.get("url") or state.get("web_page_url")
            ),
            "last_assistant_message": page_data.get("last_assistant_message") or "",
            "dom_state": {
                "available": bool(page_data.get("available")),
                "reason": page_data.get("reason") or "",
                "title": page_data.get("title") or "",
                "buttons": page_data.get("buttons") or {},
                "input": page_data.get("input") or {},
                "uploads": page_data.get("uploads") or {},
                "attachment_count": page_data.get("attachment_count") or 0,
                "video_card_count": page_data.get("video_card_count") or 0,
                "running": bool(page_data.get("running")),
                "completed": bool(page_data.get("completed")),
                "login_required": bool(page_data.get("login_required")),
                "verification_required": bool(page_data.get("verification_required")),
                "account_limit": bool(page_data.get("account_limit")),
            },
            "quota_state": state.get("quota_state") or "",
            "submit_mode": state.get("submit_mode") or "",
            "current_status": state.get("status") or "",
            "attempt": state.get("attempt") or 0,
            "has_archived_video": bool(state.get("video_path")),
            "has_result_push_target": bool(state.get("result_push_url")),
            "delivery_state": state.get("delivery_state") or "",
            "recent_network_errors": network_errors[-10:],
        }
    )


def serialize_context(context: Mapping[str, Any]) -> str:
    return json.dumps(context, ensure_ascii=False, sort_keys=True, indent=2)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

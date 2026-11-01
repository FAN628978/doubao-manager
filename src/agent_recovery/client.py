from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import httpx

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_TIMEOUT_SECONDS = 30.0
# 推理模型会先输出 thinking 再输出正文；上限太小会让 token 全被思考吃光，
# 正文一个字都拿不到（stop_reason=max_tokens），诊断因此变成“未返回可解析诊断”。
DEFAULT_MAX_TOKENS = 4096
MIN_MAX_TOKENS = 2048
LOCAL_SETTINGS_PATH = (
    Path(__file__).resolve().parents[2] / ".claude" / "settings.local.json"
)


def _local_settings_env(path: str | Path | None = None) -> dict[str, str]:
    settings_path = Path(path) if path is not None else LOCAL_SETTINGS_PATH
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    env = data.get("env") if isinstance(data, Mapping) else None
    if not isinstance(env, Mapping):
        return {}
    return {
        str(key): str(value)
        for key, value in env.items()
        if value is not None
    }


def _setting(
    name: str, local_env: Mapping[str, str], default: str = ""
) -> str:
    value = os.getenv(name)
    if value is None:
        value = local_env.get(name, default)
    return value


def _env_bool(
    name: str, local_env: Mapping[str, str], default: bool = False
) -> bool:
    value = _setting(name, local_env)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(
    name: str, local_env: Mapping[str, str], default: float
) -> float:
    try:
        return max(1.0, float(_setting(name, local_env, str(default))))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, local_env: Mapping[str, str], default: int,
             minimum: int = 128) -> int:
    try:
        return max(minimum, int(_setting(name, local_env, str(default))))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class AgentConfig:
    enabled: bool = False
    mode: str = "diagnose_only"
    api_key: str = field(default="", repr=False)
    model: str = ""
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_tokens: int = DEFAULT_MAX_TOKENS

    @classmethod
    def from_env(
        cls, local_settings_path: str | Path | None = None
    ) -> "AgentConfig":
        local_env = _local_settings_env(local_settings_path)
        mode = (
            _setting("DOUBAO_AGENT_MODE", local_env) or "diagnose_only"
        ).strip().lower()
        if mode not in {"diagnose_only", "auto_safe"}:
            mode = "diagnose_only"
        return cls(
            enabled=_env_bool("DOUBAO_AGENT_ENABLED", local_env),
            mode=mode,
            api_key=(_setting("ANTHROPIC_API_KEY", local_env) or "").strip(),
            model=(_setting("ANTHROPIC_MODEL", local_env) or "").strip(),
            base_url=(
                _setting("ANTHROPIC_BASE_URL", local_env) or DEFAULT_BASE_URL
            ).strip(),
            timeout_seconds=_env_float(
                "DOUBAO_AGENT_TIMEOUT_SECONDS",
                local_env,
                DEFAULT_TIMEOUT_SECONDS,
            ),
            max_tokens=_env_int(
                "DOUBAO_AGENT_MAX_TOKENS", local_env, DEFAULT_MAX_TOKENS,
                minimum=MIN_MAX_TOKENS,
            ),
        )

    @property
    def ready(self) -> bool:
        return bool(self.api_key and self.model and self.base_url)


class ClaudeAPIError(RuntimeError):
    """A sanitized failure from the Anthropic Messages API."""


class ClaudeClient:
    """Small asynchronous client for the Anthropic Messages API.

    The project already depends on ``httpx``; no additional SDK dependency is
    required. API credentials stay in request headers and are never logged.
    """

    def __init__(self, config: AgentConfig):
        self.config = config

    def _messages_url(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/v1"):
            return base + "/messages"
        return base + "/v1/messages"

    async def create_message(
        self,
        *,
        system: str,
        messages: list[Mapping[str, Any]],
        tools: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.config.ready:
            raise ClaudeAPIError(
                "Claude 配置不完整：请设置 ANTHROPIC_API_KEY 和 ANTHROPIC_MODEL"
            )

        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        timeout = httpx.Timeout(self.config.timeout_seconds, connect=10.0)
        max_tokens = self.config.max_tokens
        body: dict[str, Any] = {}
        # 推理模型可能把预算全花在 thinking 上（stop_reason=max_tokens 且没有 text）。
        # 这种情况自动加大预算重试，避免诊断永久变成“未返回可解析诊断”。
        for attempt in range(3):
            payload: dict[str, Any] = {
                "model": self.config.model,
                "max_tokens": max_tokens,
                "temperature": 0,
                "system": system,
                "messages": messages,
            }
            if tools:
                payload["tools"] = tools
            body = await self._post(headers, timeout, payload)
            if not self._budget_exhausted(body):
                return body
            max_tokens = min(max_tokens * 2, 16000)
        return body

    @staticmethod
    def _content_text(body: Mapping[str, Any]) -> str:
        content = body.get("content") or []
        if not isinstance(content, list):
            return ""
        return "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
        ).strip()

    @classmethod
    def _budget_exhausted(cls, body: Mapping[str, Any]) -> bool:
        """输出预算被思考耗尽：停止原因是 token 上限且没有可用正文。"""
        return (
            str(body.get("stop_reason") or "") == "max_tokens"
            and not cls._content_text(body)
        )

    async def _post(self, headers: Mapping[str, str], timeout: httpx.Timeout,
                    payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self._messages_url(), headers=dict(headers), json=payload
                )
        except httpx.TimeoutException as exc:
            raise ClaudeAPIError("Claude API 请求超时") from exc
        except httpx.HTTPError as exc:
            raise ClaudeAPIError(f"Claude API 网络失败：{type(exc).__name__}") from exc

        if response.is_error:
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("error", {}).get("message", ""))[:300]
            except Exception:
                detail = response.text[:300]
            raise ClaudeAPIError(
                f"Claude API HTTP {response.status_code}"
                + (f"：{detail}" if detail else "")
            )

        try:
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("content"), list):
                raise ValueError("missing content")
            return body
        except (ValueError, TypeError) as exc:
            raise ClaudeAPIError("Claude API 返回格式无效") from exc

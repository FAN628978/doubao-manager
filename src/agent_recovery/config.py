from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .cli import find_claude_cli

DEFAULT_CLI_TIMEOUT_SECONDS = 900.0
DEFAULT_SPEND_BUDGET = 1
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
    value = _setting(name, local_env, str(default).lower())
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(
    name: str, local_env: Mapping[str, str], default: float
) -> float:
    try:
        return max(1.0, float(_setting(name, local_env, str(default))))
    except (TypeError, ValueError):
        return default


def _env_nonnegative_int(
    name: str, local_env: Mapping[str, str], default: int
) -> int:
    try:
        return max(0, int(_setting(name, local_env, str(default))))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class AgentConfig:
    enabled: bool = True
    mode: str = "autonomous"
    cli_path: str = ""
    cli_timeout_seconds: float = DEFAULT_CLI_TIMEOUT_SECONDS
    cli_permission_mode: str = "bypassPermissions"
    cli_python_path: str = ""
    spend_budget: int = DEFAULT_SPEND_BUDGET

    @classmethod
    def from_env(
        cls, local_settings_path: str | Path | None = None
    ) -> "AgentConfig":
        local_env = _local_settings_env(local_settings_path)
        explicit_cli = _setting("DOUBAO_CLAUDE_CLI_PATH", local_env)
        return cls(
            enabled=_env_bool(
                "DOUBAO_AGENT_ENABLED", local_env, default=True
            ),
            cli_path=find_claude_cli(explicit_cli),
            cli_timeout_seconds=_env_float(
                "DOUBAO_AGENT_CLI_TIMEOUT_SECONDS",
                local_env,
                DEFAULT_CLI_TIMEOUT_SECONDS,
            ),
            cli_permission_mode="bypassPermissions",
            cli_python_path=(
                _setting("DOUBAO_AGENT_PYTHON_PATH", local_env).strip()
            ),
            spend_budget=_env_nonnegative_int(
                "DOUBAO_AGENT_SPEND_BUDGET",
                local_env,
                DEFAULT_SPEND_BUDGET,
            ),
        )

    @property
    def cli_ready(self) -> bool:
        return bool(self.cli_path)

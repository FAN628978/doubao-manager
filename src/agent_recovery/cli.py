from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .context import redact_text
from .tools import TOOL_DEFINITIONS

ProcessStarted = Callable[[int, str, str], None]

_FINAL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "root_cause": {"type": "string"},
        "confidence": {"type": "number"},
        "recovery_suggestion": {"type": "string"},
        "recovery_complete": {"type": "boolean"},
        "needs_human": {"type": "boolean"},
        "manual_reason": {"type": "string"},
    },
    "required": [
        "summary",
        "root_cause",
        "confidence",
        "recovery_suggestion",
        "recovery_complete",
        "needs_human",
        "manual_reason",
    ],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class CLIProcessResult:
    exit_code: int | None
    timed_out: bool
    diagnosis: dict[str, Any]
    error: str = ""
    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)
    process_id: int | None = None


def find_claude_cli(explicit: str = "") -> str:
    """Find the local Claude Code launcher, preferring native/CreateProcess-safe files."""
    candidates: list[str] = []
    if explicit.strip():
        candidates.append(explicit.strip())
    for name in ("claude.exe", "claude.cmd", "claude.ps1", "claude"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    appdata = os.getenv("APPDATA")
    if appdata:
        candidates.extend(
            [
                str(Path(appdata) / "npm" / "claude.exe"),
                str(Path(appdata) / "npm" / "claude.cmd"),
                str(Path(appdata) / "npm" / "claude.ps1"),
            ]
        )
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen:
            continue
        seen.add(key)
        path = Path(candidate)
        if path.is_file():
            return str(path.resolve())
    return ""


def _launcher_command(cli_path: str, args: list[str]) -> list[str]:
    """Build a Windows-safe command for npm's .cmd shim."""
    suffix = Path(cli_path).suffix.lower()
    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        comspec = os.getenv("COMSPEC") or "cmd.exe"
        return [
            comspec,
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline([cli_path, *args]),
        ]
    if os.name == "nt" and suffix == ".ps1":
        pwsh = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        return [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            cli_path,
            *args,
        ]
    return [cli_path, *args]


def _json_candidate(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if text.startswith("```"):
        text = text.replace("```json", "```").replace("```", "").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    except (TypeError, ValueError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, Mapping):
                return dict(parsed)
        except (TypeError, ValueError):
            pass
    return {}


def parse_cli_result(stdout: str) -> dict[str, Any]:
    """Extract Claude's final structured result from print/json output."""
    outer = _json_candidate(stdout)
    candidates = [
        outer.get("structured_output"),
        outer.get("result"),
        outer.get("response"),
        stdout,
    ]
    for candidate in candidates:
        parsed = _json_candidate(candidate)
        if parsed:
            return parsed
    return {
        "summary": redact_text(stdout, 1500) or "Claude CLI 未返回可解析结果",
        "root_cause": "",
        "confidence": 0,
        "recovery_suggestion": "",
        "recovery_complete": False,
        "needs_human": True,
        "manual_reason": "Claude CLI 输出无法解析，请人工检查恢复日志",
    }


async def _terminate_pid(pid: int) -> None:
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            await asyncio.wait_for(killer.wait(), timeout=10)
        except Exception:
            pass
    else:
        try:
            os.killpg(pid, 15)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, 15)
            except (ProcessLookupError, PermissionError, OSError):
                pass


async def terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """Terminate a Claude process and its Windows/npm descendants."""
    if process.returncode is not None:
        return
    await _terminate_pid(process.pid)
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        pass


async def _process_command_line(pid: int) -> str:
    try:
        if os.name == "nt":
            powershell = (
                shutil.which("pwsh")
                or shutil.which("powershell")
                or "powershell.exe"
            )
            process = await asyncio.create_subprocess_exec(
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    f"(Get-CimInstance Win32_Process -Filter "
                    f"'ProcessId={int(pid)}').CommandLine"
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            process = await asyncio.create_subprocess_exec(
                "ps",
                "-p",
                str(int(pid)),
                "-o",
                "args=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=8)
        return output.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


async def terminate_stale_process(pid: int, marker: str) -> bool:
    """Kill a persisted Claude PID only when its command line has our marker."""
    if pid <= 0 or not marker:
        return False
    command_line = await _process_command_line(pid)
    if not command_line or marker not in command_line:
        return False
    await _terminate_pid(pid)
    return True


class ClaudeCLIRunner:
    """One non-interactive Claude Code process per abnormal job."""

    def __init__(
        self,
        *,
        cli_path: str,
        project_root: str | Path,
        log_dir: str | Path,
        bridge_url: str,
        timeout_seconds: float = 900.0,
        permission_mode: str = "bypassPermissions",
        python_executable: str = "",
    ):
        self.cli_path = cli_path
        self.project_root = Path(project_root).resolve()
        self.log_dir = Path(log_dir)
        self.bridge_url = bridge_url.rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.permission_mode = permission_mode
        self.python_executable = python_executable or sys.executable

    async def run(
        self,
        *,
        job_id: str,
        token: str,
        prompt: str,
        on_started: ProcessStarted | None = None,
    ) -> CLIProcessResult:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex[:10]
        safe_job = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in job_id
        )[:80]
        log_path = self.log_dir / f"cli-{safe_job}-{run_id}.log"
        marker = token[:12]
        mcp_path = self.log_dir / f".recovery-{safe_job}-{marker}-{run_id}.json"
        settings_path = self.log_dir / f".settings-{safe_job}-{run_id}.json"
        mcp_config = {
            "mcpServers": {
                "doubao_recovery": {
                    "command": self.python_executable,
                    "args": ["-m", "agent_recovery.mcp_server"],
                    "env": {
                        "PYTHONPATH": str(self.project_root / "src"),
                        "DOUBAO_RECOVERY_JOB_ID": job_id,
                        "DOUBAO_RECOVERY_TOKEN": token,
                        "DOUBAO_RECOVERY_BRIDGE_URL": self.bridge_url,
                        "PYTHONUTF8": "1",
                    },
                }
            }
        }
        mcp_path.write_text(
            json.dumps(mcp_config, ensure_ascii=False), encoding="utf-8"
        )
        settings_path.write_text(
            json.dumps(
                {
                    "disableAllHooks": True,
                    "permissions": {
                        "deny": [
                            "Bash",
                            "Read",
                            "Write",
                            "Edit",
                            "MultiEdit",
                            "NotebookEdit",
                            "WebFetch",
                            "WebSearch",
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        if os.name != "nt":
            mcp_path.chmod(0o600)
            settings_path.chmod(0o600)
        args = [
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(_FINAL_SCHEMA, separators=(",", ":")),
            "--permission-mode",
            self.permission_mode,
            "--permission-prompts",
            "none",
            "--setting-sources",
            "user",
            "--settings",
            str(settings_path),
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp_path),
            "--tools",
            "",
            "--allowedTools",
            ",".join(
                f"mcp__doubao_recovery__{tool['name']}"
                for tool in TOOL_DEFINITIONS
            ),
            "--disallowedTools",
            (
                "Bash,Read,Write,Edit,MultiEdit,NotebookEdit,WebFetch,"
                "WebSearch,Glob,Grep,Task,Agent,TodoWrite"
            ),
            "--append-system-prompt",
            (
                "You are an autonomous exception-recovery operator. Use only the "
                "doubao_recovery MCP tools. Never edit source code, credentials, or "
                "unrelated files. Never delete/replace original videos. Do not "
                "bypass login, CAPTCHA, account limits, or cross-account ownership. "
                "Verify every claimed recovery through tool results."
            ),
        ]
        command = _launcher_command(self.cli_path, args)
        env = os.environ.copy()
        env.update(
            {
                "PYTHONUTF8": "1",
                "DOUBAO_RECOVERY_JOB_ID": job_id,
                "DOUBAO_RECOVERY_TOKEN": token,
                "DOUBAO_RECOVERY_BRIDGE_URL": self.bridge_url,
                "PYTHONPATH": os.pathsep.join(
                    filter(
                        None,
                        [
                            str(self.project_root / "src"),
                            env.get("PYTHONPATH", ""),
                        ],
                    )
                ),
            }
        )
        # Use Claude CLI's own login instead of inheriting a local model gateway.
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_MODEL",
        ):
            env.pop(name, None)
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if os.name == "nt"
            else 0
        )
        process: asyncio.subprocess.Process | None = None
        timed_out = False
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.project_root),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            if on_started is not None:
                on_started(process.pid, marker, str(log_path))
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(prompt.encode("utf-8")),
                    timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError:
                timed_out = True
                await terminate_process_tree(process)
                stdout_bytes, stderr_bytes = await process.communicate()
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            await asyncio.to_thread(
                self._write_log, log_path, prompt, stdout, stderr, timed_out
            )
            diagnosis = parse_cli_result(stdout)
            error = ""
            if timed_out:
                error = (
                    f"Claude CLI 运行超过 {self.timeout_seconds:g} 秒，"
                    "进程树已终止"
                )
            elif process.returncode not in (0, None):
                error = f"Claude CLI 退出码 {process.returncode}"
                if stderr.strip():
                    error += "：" + redact_text(stderr, 500)
            return CLIProcessResult(
                exit_code=process.returncode,
                timed_out=timed_out,
                diagnosis=diagnosis,
                error=error,
                stdout=stdout,
                stderr=stderr,
                process_id=process.pid,
            )
        except asyncio.CancelledError:
            if process is not None:
                await terminate_process_tree(process)
            raise
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Claude CLI 启动失败：找不到可执行文件 {self.cli_path}"
            ) from exc
        finally:
            mcp_path.unlink(missing_ok=True)
            settings_path.unlink(missing_ok=True)

    @staticmethod
    def _write_log(
        path: Path,
        prompt: str,
        stdout: str,
        stderr: str,
        timed_out: bool,
    ) -> None:
        text = (
            "# Claude CLI recovery prompt\n"
            + redact_text(prompt, 200000)
            + "\n\n# stdout\n"
            + redact_text(stdout, 500000)
            + "\n\n# stderr\n"
            + redact_text(stderr, 200000)
            + "\n\n# timeout\n"
            + str(timed_out)
            + "\n"
        )
        path.write_text(text, encoding="utf-8")

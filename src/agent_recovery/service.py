from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .cli import ClaudeCLIRunner, terminate_stale_process
from .config import AgentConfig
from .context import (
    build_exception_context,
    redact_text,
    sanitize_value,
    utc_now_iso,
)
from .policy import RecoveryPolicy
from .tools import (
    TOOL_DEFINITIONS,
    RecoveryBackend,
    RecoveryToolExecutor,
)

ContextLoader = Callable[[str], Mapping[str, Any]]
CLIRunnerFactory = Callable[[AgentConfig], Any]
EnabledProvider = Callable[[], bool]

NON_BROWSER_RECOVERY_CODES = frozenset({"RESULT_PUSH_FAILED"})
RESUMED_TASK_STATUSES = frozenset(
    {
        "queued",
        "opening",
        "configuring",
        "uploading",
        "submitting",
        "confirming",
        "generating",
        "downloading",
        "downloaded",
    }
)
ACTIVE_RECOVERY_STATES = frozenset(
    {
        "waiting",
        "starting",
        "analyzing",
        "acting",
        "verifying",
    }
)


@dataclass
class _ToolSession:
    context: dict[str, Any]
    executor: RecoveryToolExecutor
    token: str
    job_id: str
    ledger: list[dict[str, Any]]
    lock: asyncio.Lock
    policy: RecoveryPolicy


class ExceptionRecoveryService:
    """Non-blocking exception entry point.

    Claude Code CLI is the only decision maker.
    """

    def __init__(
        self,
        *,
        backend: RecoveryBackend,
        context_loader: ContextLoader,
        log_dir: str | Path,
        cli_runner_factory: CLIRunnerFactory | None = None,
        project_root: str | Path | None = None,
        tool_bridge_url: str = "",
        enabled_provider: EnabledProvider | None = None,
    ):
        self.backend = backend
        self.context_loader = context_loader
        self.log_path = Path(log_dir) / "exceptions.jsonl"
        self.process_log_path = Path(log_dir) / "processes.jsonl"
        self.log_dir = Path(log_dir)
        self.cli_runner_factory = cli_runner_factory
        self.project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else Path(__file__).resolve().parents[2]
        )
        self.tool_bridge_url = (
            tool_bridge_url.strip()
            or os.getenv("DOUBAO_RECOVERY_BRIDGE_URL", "").strip()
            or f"http://127.0.0.1:{os.getenv('DOUBAO_PORT', '8765')}"
        ).rstrip("/")
        self.enabled_provider = enabled_provider
        self._tasks: set[asyncio.Task] = set()
        self._active_job_ids: set[str] = set()
        self._log_lock = asyncio.Lock()
        self._last_scheduled: dict[str, float] = {}
        self._active_accounts: dict[str, int] = {}
        self._account_locks: dict[str, asyncio.Lock] = {}
        self._tool_sessions: dict[str, _ToolSession] = {}
        self._active_processes: dict[str, dict[str, Any]] = {}
        self._recovery_status: dict[str, dict[str, Any]] = {}
        self._concurrency_limit = asyncio.Semaphore(
            self._max_concurrency_from_env()
        )

    @property
    def pending_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    def account_pending(self, account_id: str) -> bool:
        return bool(account_id and self._active_accounts.get(account_id, 0))

    def status_for_job(self, job_id: str) -> dict[str, Any]:
        """Return the live Claude CLI status shown by the management UI."""
        status = self._recovery_status.get(job_id)
        return sanitize_value(dict(status)) if status else {}

    def job_active(self, job_id: str) -> bool:
        status = self._recovery_status.get(job_id) or {}
        return bool(status.get("active"))

    def enabled(self) -> bool:
        """Return the effective runtime switch state.

        The environment remains an emergency master switch. The optional local
        provider lets the management UI disable or enable recovery at runtime.
        """
        if not AgentConfig.from_env().enabled:
            return False
        if self.enabled_provider is None:
            return True
        try:
            return bool(self.enabled_provider())
        except Exception:
            return False

    def _set_status(
        self,
        job_id: str,
        *,
        state: str,
        phase: str = "",
        detail: str = "",
        action: str = "",
        cli_pid: int | None = None,
        manual_reason: str = "",
    ) -> None:
        now = utc_now_iso()
        previous = self._recovery_status.get(job_id) or {}
        active = state in ACTIVE_RECOVERY_STATES
        entry: dict[str, Any] = {
            "schema_version": 1,
            "backend": "claude-cli",
            "state": state,
            "phase": phase,
            "detail": redact_text(detail, 500),
            "action": action,
            "active": active,
            "started_at": previous.get("started_at") or now,
            "updated_at": now,
            "finished_at": "" if active else now,
            "manual_reason": redact_text(manual_reason, 800),
        }
        if cli_pid is not None:
            entry["cli_pid"] = int(cli_pid)
        self._recovery_status[job_id] = entry

    @staticmethod
    def _max_concurrency_from_env() -> int:
        try:
            return max(
                1,
                int(os.getenv("DOUBAO_AGENT_MAX_CONCURRENCY", "2")),
            )
        except ValueError:
            return 2

    def notify_exception(
        self,
        *,
        job_id: str,
        error_type: str,
        error_code: str,
        error_message: str,
        account_id: str = "",
        page_snapshot: Mapping[str, Any] | None = None,
        recent_network_errors: list[Any] | None = None,
    ) -> bool:
        """Schedule one isolated Claude CLI process without blocking the worker."""
        if not self.enabled() or not job_id or job_id in self._active_job_ids:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

        try:
            dedupe_seconds = max(
                0.0,
                float(os.getenv("DOUBAO_AGENT_DEDUPE_SECONDS", "300")),
            )
        except ValueError:
            dedupe_seconds = 300.0
        now = time.monotonic()
        last = self._last_scheduled.get(job_id)
        if dedupe_seconds and last is not None and now - last < dedupe_seconds:
            return False
        self._last_scheduled[job_id] = now
        for stale_key, stamp in list(self._last_scheduled.items()):
            if now - stamp > max(dedupe_seconds, 300.0):
                self._last_scheduled.pop(stale_key, None)

        account_key = (
            account_id.strip()
            if account_id and error_code not in NON_BROWSER_RECOVERY_CODES
            else ""
        )
        if account_key:
            self._active_accounts[account_key] = (
                self._active_accounts.get(account_key, 0) + 1
            )
        self._active_job_ids.add(job_id)
        self._set_status(
            job_id,
            state="waiting",
            phase="queued",
            detail="Claude CLI 正在等待接管该异常任务",
        )

        task = loop.create_task(
            self._process_limited(
                job_id=job_id,
                account_id=account_id,
                error_type=error_type,
                error_code=error_code,
                error_message=error_message,
                page_snapshot=page_snapshot,
                recent_network_errors=recent_network_errors,
            )
        )
        self._tasks.add(task)

        def finished(completed: asyncio.Task) -> None:
            self._tasks.discard(completed)
            self._active_job_ids.discard(job_id)
            if not account_key:
                return
            remaining = self._active_accounts.get(account_key, 0) - 1
            if remaining > 0:
                self._active_accounts[account_key] = remaining
            else:
                self._active_accounts.pop(account_key, None)

        task.add_done_callback(finished)
        return True

    async def wait_for_idle(self) -> None:
        while True:
            tasks = [task for task in self._tasks if not task.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for process in list(self._active_processes.values()):
            await terminate_stale_process(
                int(process["pid"]), str(process["marker"])
            )

    async def recover_after_restart(self) -> int:
        """Terminate stale CLI trees recorded by the previous service process."""
        latest: dict[int, dict[str, Any]] = {}
        try:
            lines = await asyncio.to_thread(
                self.process_log_path.read_text, "utf-8"
            )
        except OSError:
            return 0
        for line in lines.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            try:
                pid = int(event.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            if pid:
                latest[pid] = event
        terminated = 0
        for pid, event in latest.items():
            if event.get("event") != "started":
                continue
            if await terminate_stale_process(
                pid, str(event.get("marker") or "")
            ):
                terminated += 1
                await asyncio.to_thread(
                    self._append_process_event,
                    {
                        "event": "cleaned_after_restart",
                        "pid": pid,
                        "job_id": event.get("job_id") or "",
                        "marker": event.get("marker") or "",
                    },
                )
        return terminated

    async def execute_tool(
        self,
        *,
        job_id: str,
        token: str,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Internal MCP bridge entry; token and task scope are mandatory."""
        session = self._tool_sessions.get(job_id)
        if session is None or not hmac.compare_digest(
            session.token, str(token or "")
        ):
            raise PermissionError("恢复工具会话无效或已结束")
        async with session.lock:
            try:
                loaded = await asyncio.to_thread(
                    self.context_loader, session.job_id
                )
                refreshed = build_exception_context(
                    job_id=session.job_id,
                    account_id=str(session.context.get("account_id") or ""),
                    error_type=str(session.context.get("error_type") or ""),
                    error_code=str(session.context.get("error_code") or ""),
                    error_message=str(
                        session.context.get("error_message") or ""
                    ),
                    job_state=loaded.get("job_state") or {},
                    recent_events=list(loaded.get("recent_events") or []),
                    page_snapshot=None,
                    recent_network_errors=list(
                        session.context.get("recent_network_errors") or []
                    ),
                )
                session.context = dict(refreshed)
            except Exception:
                # The backend still re-reads the concrete job before changing it;
                # keep the last known context if the lightweight refresh fails.
                pass
            self._set_status(
                session.job_id,
                state="acting",
                phase="tool",
                action=tool,
                detail=f"Claude 正在执行恢复工具：{tool}",
            )
            result = await session.executor.execute(
                tool, session.context, arguments or {}
            )
            session.ledger.append(
                sanitize_value(
                    {
                        "phase": "action",
                        "at": utc_now_iso(),
                        **result,
                    }
                )
            )
            latest_state: dict[str, Any] = {}
            try:
                loaded = await asyncio.to_thread(
                    self.context_loader, session.job_id
                )
                latest_state = sanitize_value(
                    dict(loaded.get("job_state") or {})
                )
            except Exception as exc:
                latest_state = {
                    "unavailable": True,
                    "reason": redact_text(
                        f"{type(exc).__name__}: {exc}", 300
                    ),
                }
            session.ledger.append(
                sanitize_value(
                    {
                        "phase": "verification",
                        "at": utc_now_iso(),
                        "tool": tool,
                        "success": bool(result.get("success")),
                        "verified": bool(result.get("verified")),
                        "detail": result.get("detail") or "",
                        "latest_state": latest_state,
                    }
                )
            )
            self._set_status(
                session.job_id,
                state="verifying",
                phase="verify",
                action=tool,
                detail=f"Claude 正在核对 {tool} 的执行结果",
            )
            return result

    async def _process_limited(self, **kwargs: Any) -> None:
        account_id = str(kwargs.get("account_id") or "")
        error_code = str(kwargs.get("error_code") or "")
        account_key = (
            account_id
            if account_id
            and error_code not in NON_BROWSER_RECOVERY_CODES
            else ""
        )
        if not account_key:
            async with self._concurrency_limit:
                await self._process(**kwargs)
            return
        lock = self._account_locks.setdefault(account_key, asyncio.Lock())
        async with lock:
            async with self._concurrency_limit:
                await self._process(**kwargs)

    async def _process(
        self,
        *,
        job_id: str,
        account_id: str,
        error_type: str,
        error_code: str,
        error_message: str,
        page_snapshot: Mapping[str, Any] | None,
        recent_network_errors: list[Any] | None,
    ) -> None:
        started_at = utc_now_iso()
        config = AgentConfig.from_env()
        record: dict[str, Any] = {
            "schema_version": 3,
            "exception_time": started_at,
            "job_id": job_id,
            "account_id": account_id,
            "error_type": error_type,
            "error_code": error_code,
            "error_message": redact_text(error_message, 1000),
            "agent_enabled": True,
            "agent_backend": "claude-cli",
            "agent_mode": "autonomous",
            "diagnosis": None,
            "recovery_suggestion": "",
            "actions": [],
            "recovery_success": None,
            "needs_human": True,
            "spend_budget": config.spend_budget,
            "spend_attempts": 0,
            "operation_ledger": [],
            "cli_path": config.cli_path,
            "cli_exit_code": None,
            "cli_timed_out": False,
            "cli_log": "",
            "process_id": None,
            "agent_error": None,
            "completed_at": "",
        }
        self._set_status(
            job_id,
            state="analyzing",
            phase="context",
            detail="Claude 正在读取最新任务状态和异常上下文",
        )
        try:
            loaded = dict(self.context_loader(job_id) or {})
            record["job_state"] = loaded.get("job_state") or {}
            context = build_exception_context(
                job_id=job_id,
                account_id=account_id,
                error_type=error_type,
                error_code=error_code,
                error_message=error_message,
                job_state=loaded.get("job_state") or {},
                recent_events=loaded.get("recent_events") or [],
                page_snapshot=page_snapshot,
                recent_network_errors=recent_network_errors,
            )
            record["context"] = context
            await self._run_cli(config, context, record)
        except asyncio.CancelledError:
            record["agent_error"] = "Claude 恢复任务已取消，子进程树和账号占用将释放"
            record["needs_human"] = True
            self._set_status(
                job_id,
                state="cancelled",
                phase="cancelled",
                detail=record["agent_error"],
                manual_reason=record["agent_error"],
            )
            raise
        except Exception as exc:
            record["agent_error"] = redact_text(
                f"{type(exc).__name__}: {exc}", 1000
            )
            record["needs_human"] = True
            self._set_status(
                job_id,
                state="failed",
                phase="failed",
                detail=record["agent_error"],
                manual_reason=record["agent_error"],
            )
        finally:
            self._tool_sessions.pop(job_id, None)
            record["completed_at"] = utc_now_iso()
            try:
                await self._append_record(record)
            except Exception:
                # Logging failures must never escape into the business worker.
                pass

    async def _run_cli(
        self,
        config: AgentConfig,
        context: Mapping[str, Any],
        record: dict[str, Any],
    ) -> None:
        if not config.cli_ready:
            raise RuntimeError(
                "未检测到 Claude Code CLI；请安装 claude 或设置 "
                "DOUBAO_CLAUDE_CLI_PATH"
            )
        job_id = str(context.get("job_id") or "")
        self._set_status(
            job_id,
            state="starting",
            phase="launch",
            detail="Claude CLI 正在启动",
        )
        token = secrets.token_urlsafe(32)
        policy = RecoveryPolicy("autonomous", spend_budget=config.spend_budget)
        executor = RecoveryToolExecutor(self.backend, policy)
        ledger: list[dict[str, Any]] = []
        session = _ToolSession(
            context=dict(context),
            executor=executor,
            token=token,
            job_id=job_id,
            ledger=ledger,
            lock=asyncio.Lock(),
            policy=policy,
        )
        self._tool_sessions[job_id] = session
        ledger.append(
            {
                "phase": "analysis",
                "at": utc_now_iso(),
                "summary": "Claude CLI 已接管异常，开始自主规划和调用恢复工具",
            }
        )

        def process_started(pid: int, marker: str, log_path: str) -> None:
            record["cli_log"] = log_path
            self._active_processes[job_id] = {
                "pid": pid,
                "marker": marker,
            }
            self._append_process_event(
                {
                    "event": "started",
                    "job_id": job_id,
                    "pid": pid,
                    "marker": marker,
                    "log_path": log_path,
                    "started_at": utc_now_iso(),
                }
            )
            self._set_status(
                job_id,
                state="analyzing",
                phase="cli",
                detail="Claude CLI 已启动，正在分析异常",
                cli_pid=pid,
            )

        runner = (
            self.cli_runner_factory(config)
            if self.cli_runner_factory is not None
            else ClaudeCLIRunner(
                cli_path=config.cli_path,
                project_root=self.project_root,
                log_dir=self.log_dir,
                bridge_url=self.tool_bridge_url,
                timeout_seconds=config.cli_timeout_seconds,
                permission_mode=config.cli_permission_mode,
                python_executable=config.cli_python_path,
            )
        )
        try:
            result = await runner.run(
                job_id=job_id,
                token=token,
                prompt=self._build_cli_prompt(context),
                on_started=process_started,
            )
        finally:
            self._tool_sessions.pop(job_id, None)
            process = self._active_processes.pop(job_id, None)
            if process is not None:
                self._append_process_event(
                    {
                        "event": "ended",
                        "job_id": job_id,
                        "pid": process.get("pid"),
                        "marker": process.get("marker"),
                        "ended_at": utc_now_iso(),
                    }
                )

        ledger.append(
            {
                "phase": "cli_result",
                "at": utc_now_iso(),
                "exit_code": result.exit_code,
                "timed_out": bool(result.timed_out),
                "error": redact_text(result.error, 500),
            }
        )
        diagnosis = dict(result.diagnosis or {})
        diagnosis = {
            "summary": redact_text(diagnosis.get("summary") or "", 1500),
            "root_cause": redact_text(diagnosis.get("root_cause") or "", 1200),
            "confidence": diagnosis.get("confidence", 0),
            "recovery_suggestion": redact_text(
                diagnosis.get("recovery_suggestion") or "", 1200
            ),
            "recovery_complete": bool(
                diagnosis.get("recovery_complete")
            ),
            "needs_human": bool(diagnosis.get("needs_human", True)),
            "manual_reason": redact_text(
                diagnosis.get("manual_reason") or "", 800
            ),
        }
        loaded = dict(self.context_loader(job_id) or {})
        current_status = str(
            (loaded.get("job_state") or {}).get("status") or ""
        )
        resumed = current_status in RESUMED_TASK_STATUSES
        if result.error:
            record["agent_error"] = result.error
        if resumed:
            diagnosis["needs_human"] = False
            diagnosis["manual_reason"] = ""
        elif not diagnosis.get("needs_human"):
            diagnosis["needs_human"] = True
            diagnosis["manual_reason"] = (
                diagnosis.get("manual_reason")
                or f"Claude CLI 已结束，但任务仍停留在 {current_status or 'unknown'}，"
                "没有可用状态证明恢复成功"
            )
        action_items = [
            item for item in ledger if item.get("phase") == "action"
        ]
        record["diagnosis"] = diagnosis
        record["recovery_suggestion"] = diagnosis["recovery_suggestion"]
        record["needs_human"] = bool(diagnosis["needs_human"])
        record["recovery_success"] = bool(resumed)
        record["actions"] = action_items
        record["operation_ledger"] = list(ledger)
        record["action_count"] = sum(
            bool(item.get("executed")) for item in action_items
        )
        record["spend_attempts"] = sum(
            bool(item.get("spend_attempt")) for item in action_items
        )
        record["cli_exit_code"] = result.exit_code
        record["cli_timed_out"] = bool(result.timed_out)
        record["process_id"] = result.process_id
        final_state = "completed" if resumed else "needs_human"
        final_detail = (
            diagnosis.get("summary")
            if resumed
            else diagnosis.get("manual_reason")
            or diagnosis.get("summary")
            or "Claude CLI 已结束，但恢复结果未能验证"
        )
        self._set_status(
            job_id,
            state=final_state,
            phase="completed" if resumed else "needs_human",
            detail=final_detail,
            manual_reason=diagnosis.get("manual_reason") or "",
            cli_pid=result.process_id,
        )

    def _build_cli_prompt(self, context: Mapping[str, Any]) -> str:
        system_prompt = self._read_instruction_file(
            self.project_root / ".claude" / "agents" / "doubao-recovery.md"
        )
        skill_prompt = self._read_instruction_file(
            self.project_root
            / ".claude"
            / "skills"
            / "doubao-recovery"
            / "SKILL.md"
        )
        tools = [
            {
                "name": item["name"],
                "description": item["description"],
            }
            for item in TOOL_DEFINITIONS
        ]
        return (
            system_prompt
            + "\n\n"
            + skill_prompt
            + "\n\n你由异常恢复服务以独立子进程启动。任务必须保持隔离；"
            "只能操作下面 JSON 中指定的 job/account。每轮调用一个 MCP 工具，"
            "读取结果后继续规划。禁止修改生产源代码。\n"
            "可用 MCP 工具：\n"
            + json.dumps(tools, ensure_ascii=False)
            + "\n\n<exception_context>\n"
            + json.dumps(context, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n</exception_context>\n\n"
            "完成或确认无法恢复后，停止调用工具并输出规定 JSON。"
        )

    @staticmethod
    def _read_instruction_file(path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        if not text.startswith("---"):
            return text
        parts = text.split("---", 2)
        return parts[2].lstrip() if len(parts) == 3 else text

    def _append_process_event(self, event: Mapping[str, Any]) -> None:
        line = json.dumps(
            sanitize_value(event), ensure_ascii=False, sort_keys=True
        ) + "\n"
        self.process_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.process_log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    async def _append_record(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        async with self._log_lock:
            await asyncio.to_thread(self._append_sync, line)

    def _append_sync(self, line: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

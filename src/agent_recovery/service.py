from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .agent import RecoveryAgent
from .client import AgentConfig, ClaudeClient
from .context import (
    build_exception_context,
    redact_text,
    utc_now_iso,
)
from .policy import RecoveryPolicy
from .tools import RecoveryBackend, RecoveryToolExecutor

ContextLoader = Callable[[str], Mapping[str, Any]]
ClientFactory = Callable[[AgentConfig], Any]

BROWSER_RECOVERY_CODES = frozenset(
    {
        "WORKER_EXCEPTION",
        "WEB_AUTOMATION_ERROR",
        "BROWSER_START_FAILED",
        "PAGE_LOAD_FAILED",
        "WAIT_TIMEOUT",
        "RESULT_NOT_READY",
        "RESULT_URL_NOT_FOUND",
        "DETAIL_SAVE_NOT_FOUND",
        "RECALL_EXCEPTION",
        "DOWNLOAD_FAILED",
    }
)


class ExceptionRecoveryService:
    """Non-blocking entry point used only by existing exception handlers."""

    def __init__(
        self,
        *,
        backend: RecoveryBackend,
        context_loader: ContextLoader,
        log_dir: str | Path,
        client_factory: ClientFactory | None = None,
    ):
        self.backend = backend
        self.context_loader = context_loader
        self.log_path = Path(log_dir) / "exceptions.jsonl"
        self.client_factory = client_factory or ClaudeClient
        self._tasks: set[asyncio.Task] = set()
        self._log_lock = asyncio.Lock()
        self._last_scheduled: dict[str, float] = {}
        self._active_accounts: dict[str, int] = {}
        self._concurrency_limit = asyncio.Semaphore(
            self._max_concurrency_from_env()
        )

    @property
    def pending_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    def account_pending(self, account_id: str) -> bool:
        return bool(account_id and self._active_accounts.get(account_id, 0))

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
        """Schedule diagnosis without waiting for Claude or recovery work."""
        config = AgentConfig.from_env()
        if not config.enabled or not job_id:
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
        key = job_id
        last = self._last_scheduled.get(key)
        if dedupe_seconds and last is not None and now - last < dedupe_seconds:
            return False
        self._last_scheduled[key] = now
        for stale_key, stamp in list(self._last_scheduled.items()):
            if now - stamp > max(dedupe_seconds, 300.0):
                self._last_scheduled.pop(stale_key, None)

        # H3 delivery recovery does not use a browser and must not delay idle close.
        account_key = (
            account_id.strip() if error_code in BROWSER_RECOVERY_CODES else ""
        )
        if account_key:
            self._active_accounts[account_key] = (
                self._active_accounts.get(account_key, 0) + 1
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

    async def _process_limited(self, **kwargs: Any) -> None:
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
        context: dict[str, Any] = {}
        record: dict[str, Any] = {
            "schema_version": 1,
            "exception_time": started_at,
            "job_id": job_id,
            "account_id": account_id,
            "error_type": error_type,
            "error_code": error_code,
            "error_message": redact_text(error_message, 1000),
            "agent_enabled": True,
            "agent_mode": config.mode,
            "diagnosis": None,
            "recovery_suggestion": "",
            "actions": [],
            "recovery_success": None,
            "needs_human": True,
            "agent_error": None,
            "completed_at": "",
        }
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
            if not config.ready:
                raise RuntimeError(
                    "Claude 配置不完整：请设置 ANTHROPIC_API_KEY 和 ANTHROPIC_MODEL"
                )

            policy = RecoveryPolicy(config.mode)
            client = self.client_factory(config)
            executor = RecoveryToolExecutor(self.backend, policy)
            agent = RecoveryAgent(client, executor, policy)
            result = await agent.diagnose(context, mode=config.mode)
            diagnosis = result["diagnosis"]
            action = result.get("action")
            record["diagnosis"] = diagnosis
            record["recovery_suggestion"] = diagnosis.get("recovery_suggestion", "")
            record["needs_human"] = bool(diagnosis.get("needs_human"))
            record["recovery_success"] = result.get("recovery_success")
            executed = [
                item
                for item in (result.get("actions") or [])
                if item.get("executed")
            ]
            blocked = [
                item
                for item in (result.get("actions") or [])
                if not item.get("executed")
            ]
            if executed:
                record["actions"] = executed
            if blocked:
                record["blocked_action"] = blocked[-1]
            record["action_count"] = len(executed)
        except asyncio.CancelledError:
            record["agent_error"] = "Agent 任务已取消"
            record["needs_human"] = True
            raise
        except Exception as exc:
            record["agent_error"] = redact_text(
                f"{type(exc).__name__}: {exc}", 1000
            )
            record["needs_human"] = True
        finally:
            record["completed_at"] = utc_now_iso()
            try:
                await self._append_record(record)
            except Exception:
                # Logging failures must never escape into the business worker.
                pass

    async def _append_record(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        async with self._log_lock:
            await asyncio.to_thread(self._append_sync, line)

    def _append_sync(self, line: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

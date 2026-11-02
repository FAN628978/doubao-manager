from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Mapping, Optional
from urllib.parse import urlparse

import httpx
from fastapi import Body, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from agent_recovery.context import capture_page_snapshot, error_code_from_exception
from agent_recovery.config import AgentConfig
from agent_recovery.service import ExceptionRecoveryService
from agent_recovery.tools import RecoveryBackend

from .automation import *
from .config import (
    ACTIVE,
    CHAT_PAGE_URL,
    DAILY_VIDEO_SECONDS,
    MANUAL_FAILED,
    MANUAL_WAIT,
    MAX_IMAGE,
    MAX_VIDEO,
    MEMBER_MODEL,
    MODES,
    REAL_CHAT_URL,
    TERMINAL,
)
from .models import (
    ClaudeRecoverySetting,
    ConfirmationPresetInput,
    ConfirmationSendRequest,
    IgnoreQuotaSetting,
    ProcessingLimitSetting,
    QueueAccountRequest,
    ReviewNeeded,
    SubmitModeSetting,
    VideoRequest,
)
from .quota import (
    account_value,
    enforce_account_model,
    local_day,
    quota_held_today,
    quota_summary,
    recover_interrupted_jobs,
    reserve_quota,
    settle_quota,
)
from .schema import default_processing_limit, ensure_schema as ensure_web_schema
from .utils import adaptive_poll, automatic_step_delay, concise_job_title, safe, set_job_page_title, ts

logger = logging.getLogger(__name__)


def session_binding(url: str) -> tuple[str, str]:
    """Return the persisted session URL and id for a recognizable Doubao chat URL."""
    value = (url or "").strip()
    match = re.search(r"/chat/([0-9A-Za-z_-]+)(?:[/?#]|$)", value)
    return value, match.group(1) if match else ""


def mount_video_api(app, *, db, get_account, get_job, browser, base_dir, downloads_dir, serialize,
                    close_browser=None, existing_browser=None, recovery_service=None):
    base = Path(base_dir).resolve()
    downloads = Path(downloads_dir).resolve()
    asset_dir = base / "data" / "web_assets"
    asset_dir.mkdir(parents=True, exist_ok=True)

    def stored_path(path: Path, root: Path) -> str:
        resolved, allowed = path.resolve(), root.resolve()
        if resolved != allowed and allowed not in resolved.parents:
            raise ValueError(f"只能保存 data 目录内的文件路径：{path}")
        return str(resolved.relative_to(base))

    def resolved_path(value: str, root: Path) -> Path:
        raw = Path(value)
        if raw.is_absolute():
            raise HTTPException(409, "data 中存在绝对文件路径，请使用当前版本重新建立该记录")
        resolved, allowed = (base / raw).resolve(), root.resolve()
        if resolved != allowed and allowed not in resolved.parents:
            raise HTTPException(409, "data 中的文件路径超出允许目录")
        return resolved
    # Manual send/recall operations remain serialized per account. Automatic jobs
    # use dedicated pages and may run concurrently in the same browser context.
    locks: dict[str, asyncio.Lock] = {}
    pending: dict[str, asyncio.Task] = {}
    # 每个任务从开始执行起就绑定自己的网页句柄。人工发送、异常复查和关联下载
    # 都优先复用该任务实际执行的标签页，避免按账号的最后一个页面猜测。
    prepared_pages: dict[str, object] = {}
    app.state.video_pending = pending
    app.state.video_prepared_pages = prepared_pages
    # Fire-and-forget tail work (H3 delivery, idle close) must outlive the worker
    # without pinning its account lane.
    background_tasks: set[asyncio.Task] = set()
    app.state.video_background_tasks = background_tasks
    # The processing limit controls how many machine-owned jobs may run at once.
    # Automatic browser work is limited only by the global processing setting.
    # Each task owns a dedicated page, so jobs from the same account can progress
    # concurrently without sharing page state.
    processing_condition = asyncio.Condition()
    active_processing = 0
    processing_job_ids: set[str] = set()
    # Only machine-owned phases occupy a processing slot. Waiting for a human
    # (manual send / review) is already a terminal hand-off: keeping the slot
    # there would let three abnormal jobs block every new task indefinitely.
    processing_statuses = frozenset(ACTIVE)
    account_condition = asyncio.Condition()
    account_owners: dict[str, set[str]] = {}
    idle_close_tasks: dict[str, asyncio.Task] = {}
    app.state.video_idle_close_tasks = idle_close_tasks

    def account_is_busy(account_id: str, ignore_jobid: str = "") -> bool:
        account_pending = getattr(recovery_service, "account_pending", None)
        if account_pending is not None and account_pending(account_id):
            return True
        if account_owners.get(account_id, set()) - {ignore_jobid}:
            return True
        for other_jobid, task in list(pending.items()):
            if other_jobid == ignore_jobid or task.done():
                continue
            try:
                if get_job(other_jobid)["account_id"] == account_id:
                    return True
            except Exception:
                continue
        for other_jobid, page in list(prepared_pages.items()):
            if other_jobid == ignore_jobid:
                continue
            try:
                if get_job(other_jobid)["account_id"] != account_id:
                    continue
                if not page.is_closed():
                    return True
            except Exception:
                continue
        return False

    async def close_account_after_idle(account_id: str, ignore_jobid: str) -> None:
        try:
            try:
                delay = max(
                    0.0,
                    float(os.getenv("DOUBAO_BROWSER_IDLE_CLOSE_SECONDS", "30")),
                )
            except ValueError:
                delay = 30.0
            await asyncio.sleep(delay)
            if close_browser is None or account_is_busy(account_id, ignore_jobid):
                return
            try:
                await close_browser(account_id)
                message = "账号浏览器已空闲关闭"
            except Exception as exc:
                message = f"账号浏览器空闲关闭失败：{str(exc)[:160]}"
            with db() as c:
                current = c.execute(
                    "SELECT status FROM jobs WHERE id=?", (ignore_jobid,)
                ).fetchone()
                c.execute(
                    "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                    "VALUES(?,?,?,?)",
                    (ignore_jobid, current["status"] if current else "",
                     message, ts()),
                )
        finally:
            idle_close_tasks.pop(account_id, None)

    def schedule_idle_close(account_id: str | None, jobid: str) -> None:
        if not account_id or close_browser is None or account_id in idle_close_tasks:
            return
        idle_close_tasks[account_id] = asyncio.create_task(
            close_account_after_idle(account_id, jobid)
        )

    def task_binding_token(jobid: str) -> str:
        ensure_web_schema(db)
        with db() as c:
            row = c.execute(
                "SELECT extension_binding_token FROM video_web_jobs WHERE job_id=?",
                (jobid,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "不是网页视频任务")
            token = (row["extension_binding_token"] or "").strip()
            if not token:
                token = secrets.token_urlsafe(32)
                c.execute(
                    "UPDATE video_web_jobs SET extension_binding_token=? WHERE job_id=?",
                    (token, jobid),
                )
        return token

    async def bind_task_page(jobid: str, page) -> str:
        """Persist and inject an opaque task identity into one exact browser tab."""
        token = task_binding_token(jobid)
        encoded = json.dumps(token)
        script = f"""
            (() => {{
              const token = {encoded};
              const attach = () => {{
                if (!document.documentElement) return;
                let marker = document.querySelector('meta[name="doubao-manager-task-binding"]');
                if (!marker) {{
                  marker = document.createElement('meta');
                  marker.name = 'doubao-manager-task-binding';
                  document.documentElement.appendChild(marker);
                }}
                marker.content = token;
              }};
              attach();
              document.addEventListener('DOMContentLoaded', attach, {{once: true}});
            }})();
        """
        try:
            await page.add_init_script(script)
        except Exception:
            pass
        try:
            await page.evaluate(script)
        except Exception:
            pass
        prepared_pages[jobid] = page
        return token

    app.state.video_bind_task_page = bind_task_page

    async def cleanup_downloaded_browser(
        jobid: str, account_id: str | None, page=None
    ) -> str:
        """Close this task's page, or the account window when it is the last task."""
        bound_page = prepared_pages.pop(jobid, None)
        pages = []
        for candidate in (page, bound_page):
            if candidate is not None and all(candidate is not item for item in pages):
                pages.append(candidate)

        async def close_task_pages() -> None:
            for task_page in pages:
                try:
                    is_closed = getattr(task_page, "is_closed", None)
                    if is_closed is None or not is_closed():
                        await task_page.close()
                except Exception:
                    pass

        if not account_id:
            await close_task_pages()
            return "page"

        idle_close = idle_close_tasks.pop(account_id, None)
        if idle_close is not None and not idle_close.done():
            idle_close.cancel()

        message = ""
        result = "page"
        async with account_condition:
            if account_is_busy(account_id, ignore_jobid=jobid):
                await close_task_pages()
                message = "同账号仍有其他任务，仅关闭当前任务网页"
            elif close_browser is None:
                await close_task_pages()
                message = "未配置浏览器窗口关闭功能，已关闭当前任务网页"
            else:
                try:
                    await close_browser(account_id)
                    result = "window"
                    message = "账号没有其他任务，已关闭浏览器窗口"
                except Exception as exc:
                    await close_task_pages()
                    message = f"关闭账号浏览器窗口失败，已关闭当前任务网页：{str(exc)[:160]}"

        with db() as c:
            current = c.execute(
                "SELECT status FROM jobs WHERE id=?", (jobid,)
            ).fetchone()
            c.execute(
                "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                "VALUES(?,?,?,?)",
                (jobid, current["status"] if current else "downloaded", message, ts()),
            )
        return result

    def local_processing_limit() -> int:
        with db() as c:
            row = c.execute(
                "SELECT value FROM local_settings WHERE key='processing_limit'"
            ).fetchone()
        try:
            return max(0, int(row["value"])) if row else default_processing_limit()
        except (TypeError, ValueError):
            return default_processing_limit()

    def persisted_processing_job_ids() -> set[str]:
        """Return jobs that still occupy the processing queue after a restart."""
        try:
            with db() as c:
                placeholders = ",".join("?" for _ in processing_statuses)
                rows = c.execute(
                    f"SELECT id FROM jobs WHERE status IN ({placeholders})",
                    tuple(processing_statuses),
                ).fetchall()
            return {row["id"] for row in rows}
        except sqlite3.OperationalError:
            return set()

    def sync_processing_job_ids() -> None:
        """Rebuild the in-memory slot set from persisted state.

        Live workers are kept because an admitted job may still be ``queued`` in
        the database until it claims its phase. Everything terminal is pruned so
        a missed release cannot inflate the count forever.
        """
        nonlocal active_processing
        live = persisted_processing_job_ids()
        held = {jid for jid, task in pending.items() if not task.done()}
        stale: set[str] = set()
        for jid in list(processing_job_ids):
            if jid in live or jid in held:
                continue
            try:
                with db() as c:
                    row = c.execute(
                        "SELECT status FROM jobs WHERE id=?", (jid,)
                    ).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row is None or row["status"] in ("downloaded", "failed", "needs_review"):
                stale.add(jid)
        processing_job_ids.difference_update(stale)
        processing_job_ids.update(live)
        active_processing = len(processing_job_ids)

    async def release_processing_job(jobid: str) -> None:
        nonlocal active_processing
        async with processing_condition:
            processing_job_ids.discard(jobid)
            active_processing = len(processing_job_ids)
            processing_condition.notify_all()

    @asynccontextmanager
    async def browser_slot(jobid: str):
        nonlocal active_processing
        async with processing_condition:
            # Recover jobs left in a machine-owned phase by a previous process. The
            # slot belongs to the task until it is archived/failed or handed to a human.
            processing_job_ids.update(persisted_processing_job_ids())
            if jobid not in processing_job_ids:
                await processing_condition.wait_for(
                    lambda: local_processing_limit() == 0
                    or len(processing_job_ids) < local_processing_limit()
                )
                processing_job_ids.add(jobid)
            active_processing = len(processing_job_ids)
        try:
            yield
        finally:
            # The slot is deliberately retained while the task waits for a generated
            # result or downloads it. Archiving, failing, or handing off to a human
            # releases it via emit().
            active_processing = len(processing_job_ids)

    app.state.video_release_processing_job = release_processing_job

    async def release_processing_job_later(jobid: str) -> None:
        try:
            await release_processing_job(jobid)
        except Exception:
            pass

    def schedule_processing_release(jobid: str) -> None:
        try:
            asyncio.get_running_loop().create_task(release_processing_job_later(jobid))
        except RuntimeError:
            processing_job_ids.discard(jobid)

    async def acquire_account_slot(jobid: str, account_id: str) -> None:
        """Track this job as an active user of the account browser context."""
        async with account_condition:
            account_owners.setdefault(account_id, set()).add(jobid)

    async def acquire_account_slot_with_timeout(
        jobid: str, account_id: str
    ) -> bool:
        """Register an account user; retained for existing worker call sites."""
        await acquire_account_slot(jobid, account_id)
        return True

    async def release_account_slot(jobid: str, account_id: str) -> None:
        async with account_condition:
            owners = account_owners.get(account_id)
            if not owners or jobid not in owners:
                return
            owners.discard(jobid)
            if not owners:
                account_owners.pop(account_id, None)
            account_condition.notify_all()

    async def release_account_slot_if_finished(
        jobid: str, account_id: str
    ) -> None:
        """Release the account lane as soon as the machine work ends.

        ``needs_review`` used to keep the lane forever; once every account had one
        such task the automatic queue could never start another job. Only the
        operator-owned manual-send page keeps its lane, because a person is still
        using that tab.
        """
        with db() as c:
            row = c.execute(
                "SELECT status FROM jobs WHERE id=?", (jobid,)
            ).fetchone()
        if not row or row["status"] != MANUAL_WAIT:
            await release_account_slot(jobid, account_id)

    async def release_account_slot_later(jobid: str, account_id: str) -> None:
        try:
            await release_account_slot(jobid, account_id)
        except Exception:
            pass

    def schedule_account_release(account_id: str, jobid: str) -> None:
        if not account_id:
            return
        try:
            asyncio.get_running_loop().create_task(
                release_account_slot_later(jobid, account_id)
            )
        except RuntimeError:
            owners = account_owners.get(account_id)
            if owners:
                owners.discard(jobid)
                if not owners:
                    account_owners.pop(account_id, None)

    # Exposed for the legacy job delete path, which lives outside this module.
    app.state.video_release_account_slot = release_account_slot

    def ensure_schema():
        ensure_web_schema(db)

    def auth(key):
        expected = os.environ.get("DOUBAO_LOCAL_API_KEY", "")
        if expected and key != expected: raise HTTPException(401, "API Key 无效")

    def local_submit_mode() -> str:
        with db() as c:
            row = c.execute(
                "SELECT value FROM local_settings WHERE key='video_submit_mode'"
            ).fetchone()
        value = (row["value"] if row else "manual").strip().lower()
        return value if value in ("auto", "manual") else "manual"

    def local_ignore_quota_check() -> bool:
        with db() as c:
            row = c.execute(
                "SELECT value FROM local_settings WHERE key='ignore_quota_check'"
            ).fetchone()
        return bool(row) and str(row["value"]).strip().lower() in ("1", "true", "yes", "on")

    def local_claude_recovery_enabled() -> bool:
        with db() as c:
            row = c.execute(
                "SELECT value FROM local_settings WHERE key='claude_recovery_enabled'"
            ).fetchone()
        if row is None:
            return True
        return str(row["value"]).strip().lower() in ("1", "true", "yes", "on")

    @app.get("/api/settings/video-submit-mode", tags=["Local Settings"])
    async def get_video_submit_mode():
        ensure_schema()
        return {"submit_mode": local_submit_mode()}

    @app.put("/api/settings/video-submit-mode", tags=["Local Settings"])
    async def update_video_submit_mode(payload: SubmitModeSetting):
        ensure_schema()
        with db() as c:
            c.execute(
                "INSERT INTO local_settings(key,value) VALUES('video_submit_mode',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (payload.submit_mode,),
            )
        return {"submit_mode": payload.submit_mode}

    @app.get("/api/settings/processing-limit", tags=["Local Settings"])
    async def get_processing_limit():
        ensure_schema()
        sync_processing_job_ids()
        return {"processing_limit": local_processing_limit(),
                "active_processing": active_processing}

    @app.put("/api/settings/processing-limit", tags=["Local Settings"])
    async def update_processing_limit(payload: ProcessingLimitSetting):
        ensure_schema()
        with db() as c:
            c.execute(
                "INSERT INTO local_settings(key,value) VALUES('processing_limit',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(payload.processing_limit),),
            )
        async with processing_condition:
            processing_condition.notify_all()
        sync_processing_job_ids()
        return {"processing_limit": payload.processing_limit,
                "active_processing": active_processing}

    @app.get("/api/settings/ignore-quota-check", tags=["Local Settings"])
    async def get_ignore_quota_check():
        ensure_schema()
        return {"ignore_quota_check": local_ignore_quota_check()}

    @app.put("/api/settings/ignore-quota-check", tags=["Local Settings"])
    async def update_ignore_quota_check(payload: IgnoreQuotaSetting):
        ensure_schema()
        with db() as c:
            c.execute(
                "INSERT INTO local_settings(key,value) VALUES('ignore_quota_check',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if payload.ignore_quota_check else "0",),
            )
        return {"ignore_quota_check": payload.ignore_quota_check}

    @app.get("/api/settings/claude-recovery", tags=["Local Settings"])
    async def get_claude_recovery_setting():
        ensure_schema()
        environment_enabled = AgentConfig.from_env().enabled
        local_enabled = local_claude_recovery_enabled()
        return {
            "claude_recovery_enabled": bool(environment_enabled and local_enabled),
            "environment_enabled": environment_enabled,
            "active_recoveries": int(getattr(recovery_service, "pending_count", 0) or 0),
        }

    @app.put("/api/settings/claude-recovery", tags=["Local Settings"])
    async def update_claude_recovery_setting(payload: ClaudeRecoverySetting):
        ensure_schema()
        with db() as c:
            c.execute(
                "INSERT INTO local_settings(key,value) VALUES('claude_recovery_enabled',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if payload.claude_recovery_enabled else "0",),
            )
        if not payload.claude_recovery_enabled:
            shutdown = getattr(recovery_service, "shutdown", None)
            if shutdown is not None:
                await shutdown()
        environment_enabled = AgentConfig.from_env().enabled
        return {
            "claude_recovery_enabled": bool(
                environment_enabled and payload.claude_recovery_enabled
            ),
            "environment_enabled": environment_enabled,
            "active_recoveries": int(getattr(recovery_service, "pending_count", 0) or 0),
        }

    def fingerprint(req):
        # Display naming is metadata, not generation input; changing it must not break
        # idempotent reuse of an already submitted H3 task.
        val = req.model_dump(exclude={"idempotency_key", "auto_start", "task_name", "result_push_url", "result_push_token"})
        return hashlib.sha256(json.dumps(val, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def resolve_assets(req):
        ids = [x for x in [req.first_frame_asset_id, req.last_frame_asset_id] if x] + req.reference_asset_ids
        out = {}
        with db() as c:
            for aid in ids:
                r = c.execute("SELECT stored_path FROM video_web_assets WHERE id=?", (aid,)).fetchone()
                if not r: raise HTTPException(422, f"素材不存在: {aid}")
                p = resolved_path(r["stored_path"], asset_dir)
                if not p.is_file(): raise HTTPException(422, f"素材文件不存在: {aid}")
                out[aid] = p
        return out

    def emit(jobid, status, msg="", code="", result=None, url="", result_source="web_automated"):
        now = ts()
        retryable = {
            "RECALL_EXCEPTION", "RESULT_NOT_READY", "WAIT_TIMEOUT",
            "WEB_AUTOMATION_ERROR", "WORKER_EXCEPTION",
        }
        with db() as c:
            previous = c.execute(
                "SELECT status,account_id FROM jobs WHERE id=?", (jobid,)
            ).fetchone()
            web_previous = c.execute(
                "SELECT phase,phase_started_at,attempt FROM video_web_jobs WHERE job_id=?",
                (jobid,),
            ).fetchone()
            prior_status = previous["status"] if previous else ""
            c.execute("UPDATE jobs SET status=?,updated_at=?,error_message=? WHERE id=?",
                      (status, now, msg if status in ("needs_review", "failed") else "", jobid))
            old_phase = web_previous["phase"] if web_previous else ""
            phase_started = now
            if (web_previous and old_phase == status
                    and (web_previous["phase_started_at"] or "")):
                phase_started = web_previous["phase_started_at"]
            c.execute(
                "UPDATE video_web_jobs SET phase=?,phase_started_at=?,"
                "last_heartbeat_at=? WHERE job_id=?",
                (status, phase_started, now, jobid),
            )
            if code:
                delay = 0
                if code in retryable:
                    attempt = int(web_previous["attempt"] or 0) if web_previous else 0
                    delay = min(300, 15 * (2 ** min(attempt, 4)))
                next_retry = (
                    (datetime.now(timezone.utc) + timedelta(seconds=delay))
                    .isoformat(timespec="seconds")
                    if delay else ""
                )
                c.execute(
                    "UPDATE video_web_jobs SET error_code=?,error_class=?,"
                    "next_retry_at=? WHERE job_id=?",
                    (code, code, next_retry, jobid),
                )
            elif status not in ("needs_review", "failed"):
                c.execute(
                    "UPDATE video_web_jobs SET error_code='',error_class='',"
                    "next_retry_at='' WHERE job_id=?",
                    (jobid,),
                )
            if url:
                session_url, session_id = session_binding(url)
                c.execute(
                    "UPDATE video_web_jobs SET web_page_url=?,session_url=?,session_id=? "
                    "WHERE job_id=?",
                    (url, session_url, session_id, jobid),
                )
            if result:
                relative = stored_path(Path(result["output"]), downloads)
                c.execute("UPDATE jobs SET video_path=? WHERE id=?", (relative, jobid))
                result_url = result["web_page_url"]
                session_url, session_id = session_binding(result_url)
                c.execute(
                    "UPDATE video_web_jobs SET video_url=?,web_page_url=?,session_url=?,"
                    "session_id=?,error_code='',result_source=? WHERE job_id=?",
                    (result["video_url"], result_url, session_url, session_id,
                     result_source, jobid),
                )
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (jobid,status,msg,now))
            settle_quota(c, jobid, status, prior_status=prior_status, error_code=code)
        if status in ("downloaded", "failed"):
            schedule_processing_release(jobid)
            schedule_account_release(previous["account_id"] if previous else "", jobid)
        elif status in ("needs_review", MANUAL_WAIT):
            # Hand-off to a human ends the machine phase. The account/page may
            # stay reserved for review, but the processing slot must be freed.
            schedule_processing_release(jobid)

    def detail(jobid):
        row = get_job(jobid)
        with db() as c:
            extra = c.execute("SELECT * FROM video_web_jobs WHERE job_id=?", (jobid,)).fetchone()
            quota = quota_summary(c, row["account_id"]) if row["account_id"] else None
        if not extra: raise HTTPException(404, "不是网页视频任务")
        val = serialize(row)
        val["web"] = dict(extra)
        val["web"]["assets"] = json.loads(val["web"].pop("assets_json") or "{}")
        val["web"].pop("request_fingerprint", None)
        val["web"].pop("quota_day", None)
        val["web"]["quota_state"] = val["web"].get("quota_state") or ""
        val["web"]["archived_at"] = val["web"].get("archived_at") or ""
        # 归档是用户手动动作：downloaded 只表示处理完，archived_at 非空才进入“已归档”。
        val["archived"] = bool(val["web"]["archived_at"])
        val["web"].pop("recall_token", None)
        val["web"].pop("result_push_token", None)
        val["web"].pop("extension_binding_token", None)
        val["task_name"] = val["web"].get("task_name") or ""
        val["video_file_url"] = f"/api/jobs/{jobid}/video" if row["video_path"] else None
        status_for_job = getattr(recovery_service, "status_for_job", None)
        if status_for_job is not None:
            val["recovery"] = status_for_job(jobid)
        if quota: val["quota"] = quota
        return val

    def load_recovery_context(jobid: str) -> dict:
        with db() as c:
            row = c.execute(
                "SELECT j.id,j.account_id,j.status,j.video_path,"
                "w.phase,w.error_code,w.web_page_url,w.quota_state,"
                "w.submit_mode,w.result_push_url,w.delivery_state,w.attempt,"
                "w.session_url,w.session_id,w.result_card_index,w.assets_json "
                "FROM jobs j LEFT JOIN video_web_jobs w ON j.id=w.job_id "
                "WHERE j.id=?",
                (jobid,),
            ).fetchone()
            events = c.execute(
                "SELECT status,message,timestamp FROM video_web_events "
                "WHERE job_id=? ORDER BY id DESC LIMIT 30",
                (jobid,),
            ).fetchall()
        job_state = dict(row) if row else {}
        if row:
            owners = account_owners.get(row["account_id"] or "", set())
            other_owner = next((owner for owner in owners if owner != jobid), "")
            job_state["account_owner_job_id"] = (
                jobid if jobid in owners else other_owner
            )
            job_state["account_owner_job_ids"] = sorted(owners)
            job_state["account_owned_by_job"] = not owners or jobid in owners
        return {
            "job_state": job_state,
            "recent_events": [dict(event) for event in reversed(events)],
        }

    async def notify_recovery(
        jobid: str,
        error_type: str,
        error_code: str,
        error_message: str,
        *,
        account_id: str = "",
        page=None,
        page_snapshot: dict | None = None,
        recent_network_errors: list | None = None,
    ) -> bool:
        """Best-effort Agent handoff. It never raises into business handling."""
        if recovery_service is None:
            return False
        try:
            snapshot = page_snapshot
            if snapshot is None and page is not None:
                snapshot = await capture_page_snapshot(page)
            scheduled = recovery_service.notify_exception(
                job_id=jobid,
                account_id=account_id,
                error_type=error_type,
                error_code=error_code,
                error_message=error_message,
                page_snapshot=snapshot,
                recent_network_errors=recent_network_errors,
            )
            if not scheduled:
                logger.debug(
                    "Agent recovery was not scheduled for job %s (%s)",
                    jobid,
                    error_code,
                )
            return bool(scheduled)
        except Exception:
            logger.exception(
                "Agent recovery dispatch failed for job %s (%s)",
                jobid,
                error_code,
            )
            return False

    async def record_recovery_failure(
        jobid: str,
        exc: BaseException,
        *,
        account_id: str,
        page=None,
        error_code: str = "",
        error_message: str = "",
        url: str = "",
    ) -> None:
        """Persist a failed manual action and hand it to the recovery agent."""
        code = error_code or error_code_from_exception(exc)
        detail = str(
            error_message
            or getattr(exc, "detail", "")
            or exc
            or type(exc).__name__
        )[:350]
        try:
            current = get_job(jobid)
        except Exception:
            current = None
        current_status = current["status"] if current else ""
        if current_status != "failed":
            try:
                emit(jobid, "needs_review", detail, code=code, url=url)
            except Exception:
                logger.exception(
                    "Could not persist recovery failure for job %s", jobid
                )
        await notify_recovery(
            jobid,
            type(exc).__name__,
            code,
            detail,
            account_id=account_id,
            page=page,
        )

    def assemble_req(jobid):
        with db() as c:
            r = c.execute("SELECT j.*, w.* FROM jobs j JOIN video_web_jobs w ON j.id=w.job_id WHERE j.id=?", (jobid,)).fetchone()
        if not r: raise HTTPException(404, "视频生成任务不存在")
        a = json.loads(r["assets_json"] or "{}")
        return VideoRequest(account_id=r["account_id"], project_id=r["project_id"], episode_id=r["episode_id"],
                            shot_id=r["shot_id"], prompt=r["prompt"], mode=r["mode"],
                            task_name=r["task_name"] or "", ratio=r["ratio"], duration=r["duration"],
                            model=(r["model"] or None), submit_mode=(r["submit_mode"] or "auto"),
                            result_push_url=(r["result_push_url"] or None),
                            result_push_token=(r["result_push_token"] or None), **a)

    def ensure_start_quota(jobid: str, account_id: str) -> None:
        """Ensure this queued job owns enough quota before its account Chrome opens."""
        with db() as c:
            w = c.execute(
                "SELECT duration,quota_day,quota_state FROM video_web_jobs WHERE job_id=?",
                (jobid,),
            ).fetchone()
            if not w:
                raise HTTPException(404, "视频生成任务不存在")
            quota = quota_summary(c, account_id)
            if quota_held_today(w):
                return
            duration = int(w["duration"] or 0)
            day = local_day()
            previous_day = w["quota_day"] or ""
            state = w["quota_state"] or ""
            remaining = quota["remaining_seconds"]
            if duration > remaining and not local_ignore_quota_check():
                raise HTTPException(
                    409, f"账号当日剩余额度不足（需 {duration} 秒，剩余 {remaining} 秒）"
                )
            if previous_day and previous_day != day:
                if state in ("", "reserved"):
                    c.execute(
                        "UPDATE account_quota SET reserved_seconds=MAX(0,reserved_seconds-?) "
                        "WHERE account_id=? AND day=?",
                        (duration, account_id, previous_day),
                    )
                elif state == "unknown":
                    # The previous attempt may already have spent quota. Keep the
                    # conservative charge and reserve a separate current-day hold.
                    c.execute(
                        "UPDATE account_quota SET reserved_seconds=MAX(0,reserved_seconds-?),"
                        "consumed_seconds=consumed_seconds+? WHERE account_id=? AND day=?",
                        (duration, duration, account_id, previous_day),
                    )
            reserve_quota(c, account_id, duration, day)
            c.execute(
                "UPDATE video_web_jobs SET quota_day=?,quota_state='reserved' WHERE job_id=?",
                (day, jobid),
            )

    def schedule_delivery(jobid: str) -> None:
        """Deliver to H3 after the worker is done, so a slow server cannot pin a job.

        The old code awaited push_result() inside the worker with a 600 s timeout:
        a hanging downstream held the account lane and the processing slot for up to
        ten minutes after the video was already archived.
        """

        async def deliver() -> None:
            try:
                await push_result(jobid)
            except HTTPException as exc:
                # Delivery failure never changes the generation's downloaded state.
                with db() as c:
                    c.execute(
                        "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                        "VALUES(?,?,?,?)",
                        (jobid, "downloaded",
                         "视频已归档，但自动推送服务器失败：" + str(exc.detail)[:300],
                         ts()),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Background result delivery failed for %s", jobid)

        task = asyncio.create_task(deliver())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    async def finish_downloaded(jobid: str, message: str, result: dict,
                                result_source: str = "web_automated",
                                close_account_after: bool = False) -> None:
        """Archive the result, then run server delivery as an independent outbox."""
        emit(jobid, "downloaded", message, result=result, result_source=result_source)
        auto_push = False
        account_id = ""
        quota = None
        try:
            with db() as c:
                push = c.execute(
                    "SELECT submit_mode,result_push_url,result_push_token,"
                    "result_pushed_at,delivery_state FROM video_web_jobs "
                    "WHERE job_id=?",
                    (jobid,),
                ).fetchone()
                job = c.execute(
                    "SELECT account_id FROM jobs WHERE id=?", (jobid,)
                ).fetchone()
                account_id = job["account_id"] if job and job["account_id"] else ""
                quota = quota_summary(c, account_id) if account_id else None
                if not push:
                    return
                configured = bool(push["result_push_url"] and push["result_push_token"])
                if push["result_pushed_at"]:
                    c.execute(
                        "UPDATE video_web_jobs SET delivery_state='delivered',"
                        "delivery_error='',delivery_next_retry_at='' WHERE job_id=?",
                        (jobid,),
                    )
                elif configured and (push["submit_mode"] or "auto") == "auto":
                    c.execute(
                        "UPDATE video_web_jobs SET delivery_state='pending',"
                        "delivery_error='',delivery_next_retry_at='' WHERE job_id=?",
                        (jobid,),
                    )
                    auto_push = True
                elif not configured:
                    c.execute(
                        "UPDATE video_web_jobs SET delivery_state='not_requested' "
                        "WHERE job_id=?",
                        (jobid,),
                    )

            if auto_push:
                schedule_delivery(jobid)
            if quota:
                quota_note = (
                    f"额度已核对：剩余 {quota['remaining_seconds']} 秒 / "
                    f"{quota['daily_limit_seconds']} 秒"
                )
                with db() as c:
                    current = c.execute(
                        "SELECT status FROM jobs WHERE id=?", (jobid,)
                    ).fetchone()
                    c.execute(
                        "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                        "VALUES(?,?,?,?)",
                        (jobid, current["status"] if current else "downloaded",
                         quota_note, ts()),
                    )
        finally:
            if close_account_after:
                schedule_idle_close(account_id, jobid)

    async def heartbeat_loop(jobid: str, stopped: asyncio.Event) -> None:
        try:
            interval = max(
                0.1, float(os.getenv("DOUBAO_HEARTBEAT_SECONDS", "15"))
            )
        except ValueError:
            interval = 15.0
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
            except asyncio.TimeoutError:
                with db() as c:
                    c.execute(
                        "UPDATE video_web_jobs SET last_heartbeat_at=? WHERE job_id=?",
                        (ts(), jobid),
                    )

    def retry_delay_seconds() -> float:
        try:
            return max(1.0, float(os.getenv("DOUBAO_RETRY_SECONDS", "60")))
        except ValueError:
            return 60.0

    def defer_job(jobid: str, message: str, extra_seconds: float = 0.0) -> str:
        """Put a job back into the auto-retry queue without needing a human click.

        ``dispatched_at`` is deliberately kept: the queue tick will pick the job up
        again once ``next_retry_at`` passes. Repeated identical reasons do not spam
        the event log.
        """
        next_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=retry_delay_seconds() + extra_seconds)
        ).isoformat(timespec="seconds")
        now = ts()
        with db() as c:
            c.execute(
                "UPDATE jobs SET status='queued',updated_at=? WHERE id=?",
                (now, jobid),
            )
            c.execute(
                "UPDATE video_web_jobs SET phase='queued',next_retry_at=? "
                "WHERE job_id=?",
                (next_at, jobid),
            )
            last = c.execute(
                "SELECT message FROM video_web_events WHERE job_id=? "
                "ORDER BY id DESC LIMIT 1",
                (jobid,),
            ).fetchone()
            if not last or last["message"] != message:
                c.execute(
                    "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                    "VALUES(?,?,?,?)",
                    (jobid, "queued", message, now),
                )
        return next_at

    async def worker(jobid, req, avoid_account_id: str = ""):
        heartbeat_stop = asyncio.Event()
        heartbeat_task = None
        account_claimed = False
        download_succeeded = False
        ctx = None
        recovery_error = None
        try:
            async with browser_slot(jobid):
                with db() as c:
                    row = c.execute(
                        "SELECT status,account_id FROM jobs WHERE id=?", (jobid,)
                    ).fetchone()
                if not row or row["status"] != "queued":
                    await release_processing_job(jobid)
                    return
                # The operator may choose or clear an account while this queued
                # worker waits for a processing slot. Always use the latest value.
                req.account_id = row["account_id"] or None
                # 已拿到处理名额：这是“进入处理前”分配账号的唯一位置。
                try:
                    if not req.account_id:
                        req.account_id = allocate_account(jobid, req, avoid_account_id)
                    else:
                        ensure_start_quota(jobid, req.account_id)
                except HTTPException as exc:
                    # 没有可用账号/额度：留在自动重试队列里，由 queue tick 稍后
                    # 重派，不再把 dispatched_at 清掉等人来点第二次。
                    defer_job(
                        jobid,
                        f"暂无可用账号或额度（{exc.detail}），后台自动重试",
                    )
                    await release_processing_job(jobid)
                    return
                if row["account_id"] is None:
                    with db() as c:
                        c.execute("UPDATE jobs SET account_id=?,updated_at=? WHERE id=?",
                                  (req.account_id, ts(), jobid))
                        c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                                  (jobid, "queued",
                                   f"进入处理前分配账号 {req.account_id}，并预占 {req.duration} 秒额度", ts()))
                account = get_account(req.account_id)
                idle_close = idle_close_tasks.pop(req.account_id, None)
                if idle_close is not None and not idle_close.done():
                    idle_close.cancel()
                enforce_account_model(req, account)
                with db() as c:
                    if bool(account_value(account, "is_member", 0)):
                        c.execute("UPDATE video_web_jobs SET model=? WHERE job_id=?", (MEMBER_MODEL, jobid))
                # Multiple jobs may share one account context. perform() creates a
                # dedicated page for each job and binds that page before any action.
                if not await acquire_account_slot_with_timeout(jobid, req.account_id):
                    defer_job(jobid, "登记账号任务失败，后台自动重试")
                    await release_processing_job(jobid)
                    return
                account_claimed = True
                # Claim the persisted state last. If another action changed the job
                # while we waited, back out without starting.
                with db() as c:
                    claimed = c.execute(
                        "UPDATE jobs SET status='opening',updated_at=? "
                        "WHERE id=? AND status='queued'", (ts(), jobid))
                    if not claimed.rowcount:
                        await release_processing_job(jobid)
                        return
                    now = ts()
                    c.execute(
                        "UPDATE video_web_jobs SET "
                        "attempt=COALESCE(attempt,0)+1,phase='opening',"
                        "phase_started_at=?,last_heartbeat_at=?,"
                        "error_code='',error_class='',next_retry_at='' "
                        "WHERE job_id=?",
                        (now, now, jobid),
                    )
                    c.execute(
                        "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                        "VALUES(?,?,?,?)",
                        (jobid, "opening",
                         "已登记账号任务，开始操作任务专属网页", now),
                    )
                heartbeat_task = asyncio.create_task(
                    heartbeat_loop(jobid, heartbeat_stop)
                )
                assets = resolve_assets(req)
                ctx = await browser(req.account_id)
                target = downloads / "library" / safe(req.project_id) / safe(req.episode_id) / safe(req.shot_id or jobid) / (jobid + ".mp4")

                async def status(s, msg):
                    emit(jobid, s, msg)

                async def page_url(url):
                    emit(jobid, "submitting", "任务页已记录", url=url)

                async def binding(card_index):
                    with db() as c:
                        c.execute(
                            "UPDATE video_web_jobs SET result_card_index=? "
                            "WHERE job_id=?",
                            (card_index, jobid),
                        )

                async def manual_ready(page, state):
                    await bind_task_page(jobid, page)
                    url, session_id = session_binding(state.get("url") or "")
                    with db() as c:
                        c.execute(
                            "UPDATE video_web_jobs SET web_page_url=?,session_url=?,"
                            "session_id=?,result_card_index=? WHERE job_id=?",
                            (url, url, session_id,
                             int(state.get("videoCards") or 0), jobid),
                        )
                    emit(
                        jobid, MANUAL_WAIT,
                        "已把提示词和素材填入豆包输入框（未发送）；请在豆包网页亲自发送，"
                        "生成完成后回到管理台点击“关联下载”。",
                        url=url,
                    )

                async def task_page(page):
                    await bind_task_page(jobid, page)

                result = await perform(
                    ctx, req, assets, target, status, page_url, binding,
                    on_manual_ready=manual_ready,
                    confirmation_text=default_confirmation_text(),
                    close_on_success=False, on_review_page=task_page,
                )
                if result.get("manual"):
                    return
                await automatic_step_delay(req.submit_mode or "auto")
                await finish_downloaded(
                    jobid, "已下载并校验视频", result,
                    close_account_after=False,
                )
                download_succeeded = True
        except ReviewNeeded as e:
            emit(jobid, "needs_review", e.message, code=e.code)
            recovery_error = {
                "error_type": type(e).__name__,
                "error_code": e.code,
                "error_message": e.message,
            }
        except Exception as e:
            emit(jobid, "needs_review", str(e)[:350], code="WORKER_EXCEPTION")
            recovery_error = {
                "error_type": type(e).__name__,
                "error_code": "WORKER_EXCEPTION",
                "error_message": str(e),
            }
        finally:
            heartbeat_stop.set()
            if heartbeat_task is not None:
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            pending.pop(jobid, None)
            bound_page = prepared_pages.get(jobid)
            if bound_page is not None:
                try:
                    if bound_page.is_closed():
                        prepared_pages.pop(jobid, None)
                except Exception:
                    prepared_pages.pop(jobid, None)
            if account_claimed:
                await release_account_slot_if_finished(jobid, req.account_id)
            if recovery_error is not None:
                agent_page = prepared_pages.get(jobid)
                if agent_page is None and ctx is not None:
                    pages = list(getattr(ctx, "pages", []) or [])
                    if pages:
                        agent_page = pages[-1]
                await notify_recovery(
                    jobid,
                    recovery_error["error_type"],
                    recovery_error["error_code"],
                    recovery_error["error_message"],
                    account_id=req.account_id or "",
                    page=agent_page,
                )
            if download_succeeded:
                await cleanup_downloaded_browser(jobid, req.account_id)
            else:
                schedule_idle_close(req.account_id, jobid)

    def store_image_asset(blob: bytes, filename: str) -> str:
        """Validate an uploaded image blob and persist it under data/web_assets."""
        if not blob or len(blob) > MAX_IMAGE:
            raise HTTPException(413, "图片为空或超过15MB")
        try:
            with Image.open(io.BytesIO(blob)) as im:
                im.verify()
            with Image.open(io.BytesIO(blob)) as im:
                fmt = im.format
                if im.width < 128 or im.height < 128 or im.width > 8192 or im.height > 8192:
                    raise HTTPException(422, "图片尺寸要求为128～8192像素")
        except (UnidentifiedImageError, OSError, ValueError) as e:
            raise HTTPException(422, "不是有效图片") from e
        if fmt not in ("PNG", "JPEG", "WEBP"):
            raise HTTPException(422, "只接受 PNG/JPEG/WEBP 图片")
        ext = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[fmt]
        sha = hashlib.sha256(blob).hexdigest()
        asset_id = "asset_" + uuid.uuid4().hex[:16]
        target = asset_dir / (asset_id + ext)
        target.write_bytes(blob)
        with db() as c:
            c.execute("INSERT INTO video_web_assets VALUES (?,?,?,?,?,?)",
                      (asset_id, sha, Path(filename or "upload").name,
                       stored_path(target, asset_dir), "image/" + fmt.lower(), ts()))
        return asset_id

    @app.post("/api/v1/videos/assets", status_code=201, tags=["Video Assets"])
    async def upload_asset(file: UploadFile = File(...), x_api_key: Optional[str] = Header(None)):
        auth(x_api_key); ensure_schema()
        blob = await file.read(MAX_IMAGE + 1)
        await file.close()
        asset_id = store_image_asset(blob, file.filename)
        return {"asset_id": asset_id, "filename": file.filename,
                "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}

    def confirmation_presets():
        with db() as c:
            return [dict(row) for row in c.execute(
                "SELECT id,name,text,created_at,updated_at FROM confirmation_presets ORDER BY created_at,id")]

    def default_confirmation_text() -> str:
        with db() as c:
            row = c.execute(
                "SELECT text FROM confirmation_presets WHERE id='preset_default'"
            ).fetchone()
        text = (row["text"] if row else "").strip()
        if not text:
            raise RuntimeError("默认确认语预设不存在或内容为空")
        return text

    @app.get("/api/v1/videos/confirmation-presets", tags=["Doubao Web Video"])
    async def list_confirmation_presets(x_api_key: Optional[str] = Header(None)):
        auth(x_api_key); ensure_schema()
        return confirmation_presets()

    @app.post("/api/v1/videos/confirmation-presets", status_code=201, tags=["Doubao Web Video"])
    async def create_confirmation_preset(payload: ConfirmationPresetInput,
                                         x_api_key: Optional[str] = Header(None)):
        auth(x_api_key); ensure_schema()
        preset_id = "preset_" + uuid.uuid4().hex[:16]
        stamp = ts()
        with db() as c:
            c.execute("INSERT INTO confirmation_presets(id,name,text,created_at,updated_at) VALUES(?,?,?,?,?)",
                      (preset_id, payload.name.strip(), payload.text.strip(), stamp, stamp))
        return next(row for row in confirmation_presets() if row["id"] == preset_id)

    @app.put("/api/v1/videos/confirmation-presets/{preset_id}", tags=["Doubao Web Video"])
    async def update_confirmation_preset(preset_id: str, payload: ConfirmationPresetInput,
                                         x_api_key: Optional[str] = Header(None)):
        auth(x_api_key); ensure_schema()
        with db() as c:
            result = c.execute("UPDATE confirmation_presets SET name=?,text=?,updated_at=? WHERE id=?",
                               (payload.name.strip(), payload.text.strip(), ts(), preset_id))
            if not result.rowcount:
                raise HTTPException(404, "确认语预设不存在")
        return next(row for row in confirmation_presets() if row["id"] == preset_id)

    @app.delete("/api/v1/videos/confirmation-presets/{preset_id}", tags=["Doubao Web Video"])
    async def delete_confirmation_preset(preset_id: str,
                                         x_api_key: Optional[str] = Header(None)):
        auth(x_api_key); ensure_schema()
        if preset_id == "preset_default":
            raise HTTPException(409, "默认确认语可以编辑，但不能删除")
        with db() as c:
            count = c.execute("SELECT COUNT(*) FROM confirmation_presets").fetchone()[0]
            if count <= 1:
                raise HTTPException(409, "至少保留一个确认语预设")
            result = c.execute("DELETE FROM confirmation_presets WHERE id=?", (preset_id,))
            if not result.rowcount:
                raise HTTPException(404, "确认语预设不存在")
        return {"ok": True, "id": preset_id}

    def auto_select_account(required_seconds: int, avoid_account_id: str = "") -> str:
        """Choose the account with the least capacity left after this task.

        Best-fit allocation reduces stranded seconds on accounts whose remaining
        quota is only slightly larger than the task duration. Avoided retry accounts
        stay last; idleness and configured priority break equal-fit ties.
        When the global quota override is enabled, prefer the greatest remainder.
        """
        ignore_quota = local_ignore_quota_check()
        with db() as c:
            rows = c.execute("SELECT * FROM accounts WHERE archived=0 ORDER BY created_at ASC").fetchall()
        active_rows = [row for row in rows if not bool(account_value(row, "paused", 0))]
        if not rows:
            raise HTTPException(409, "没有可用账号，请先创建账号档案并登录豆包")
        if not active_rows:
            raise HTTPException(409, "所有账号均已暂停使用，请先恢复至少一个账号")
        # account_owners survives a needs_review hand-off (the page stays available
        # for the operator), so auto-selection must treat it as busy. Otherwise a
        # new task is assigned to a locked account and waits forever in queued.
        busy = {get_job(jid)["account_id"] for jid, t in pending.items() if not t.done()}
        busy.update(account_owners)
        with db() as c:
            candidates = []
            for r in active_rows:
                quota = quota_summary(c, r["id"])
                remaining = quota["remaining_seconds"]
                if ignore_quota or remaining >= required_seconds:
                    candidates.append((r["id"], r["created_at"], remaining,
                                       int(account_value(r, "priority", 0) or 0)))
        if candidates:
            # Availability must outrank best-fit: an account that is already running
            # another job can only start this one after that job finishes, so picking
            # it solely because its leftover quota fits better makes the queue look
            # frozen while other accounts sit idle. Busy accounts stay selectable as a
            # last resort (all candidates busy) and are then waited on explicitly.
            if ignore_quota:
                candidates.sort(key=lambda item: (
                    item[0] == avoid_account_id,
                    item[0] in busy,
                    -item[2],
                    -item[3],
                    item[1],
                ))
            else:
                candidates.sort(key=lambda item: (
                    item[0] == avoid_account_id,
                    item[0] in busy,
                    item[2] - required_seconds,
                    -item[3],
                    item[2],
                    item[1],
                ))
            return candidates[0][0]
        raise HTTPException(409, f"所有账号当日视频额度不足（本次需 {required_seconds} 秒，每日上限 {DAILY_VIDEO_SECONDS} 秒）")

    # 供普通（非网页生成）任务在进入处理前分配账号：只需要时长即可挑账号并预占额度。
    def assign_account_for_job(jobid: str) -> Optional[str]:
        with db() as c:
            row = c.execute("SELECT account_id,status FROM jobs WHERE id=?", (jobid,)).fetchone()
            if not row:
                raise HTTPException(404, "分镜任务不存在")
            if row["account_id"]:
                return row["account_id"]
            if row["status"] not in ("queued", "ready"):
                return None
        account_id = auto_select_account(0)
        with db() as c:
            c.execute("UPDATE jobs SET account_id=?,updated_at=? WHERE id=?", (account_id, ts(), jobid))
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (jobid, "queued", f"进入处理前已分配账号 {account_id}", ts()))
        return account_id

    app.state.video_assign_account_for = assign_account_for_job

    def account_label(account_id: str) -> str:
        try:
            row = get_account(account_id)
        except HTTPException:
            return account_id
        return account_value(row, "name", "") or account_id

    def release_job_quota_hold(jobid: str) -> None:
        """Give back a reservation held by a job that never started executing."""
        with db() as c:
            row = c.execute(
                "SELECT j.account_id,w.duration,w.quota_day,w.quota_state "
                "FROM jobs j JOIN video_web_jobs w ON w.job_id=j.id WHERE j.id=?",
                (jobid,),
            ).fetchone()
            if not row or not row["account_id"]:
                return
            state = row["quota_state"] or ""
            duration = int(row["duration"] or 0)
            if duration <= 0 or state not in ("", "reserved"):
                return
            c.execute(
                "UPDATE account_quota SET reserved_seconds=MAX(0,reserved_seconds-?) "
                "WHERE account_id=? AND day=?",
                (duration, row["account_id"], row["quota_day"] or local_day()),
            )
            c.execute("UPDATE video_web_jobs SET quota_state='' WHERE job_id=?", (jobid,))

    def avoid_account_for(jobid: str) -> str:
        """For retry/copied jobs, the account used by the source job (to be avoided)."""
        with db() as c:
            web_row = c.execute(
                "SELECT avoid_account_id,retry_of_job_id FROM video_web_jobs WHERE job_id=?",
                (jobid,),
            ).fetchone()
            if not web_row:
                return ""
            if web_row["avoid_account_id"]:
                return web_row["avoid_account_id"]
            if not web_row["retry_of_job_id"]:
                return ""
            previous = c.execute(
                "SELECT account_id FROM jobs WHERE id=?", (web_row["retry_of_job_id"],)
            ).fetchone()
        return (previous["account_id"] or "") if previous else ""

    def allocate_account(jobid: str, req: VideoRequest, avoid_account_id: str = "") -> str:
        """Pick the account for a queued job and reserve its quota right before dispatch."""
        avoid = avoid_account_id or avoid_account_for(jobid)
        account_id = auto_select_account(req.duration, avoid)
        ensure_start_quota(jobid, account_id)
        return account_id

    # ---- 待处理任务的账号：建单不分配，进入处理前才分配 ----

    @app.get("/api/v1/videos/queue/status", tags=["Doubao Web Video"])
    async def queue_status(x_api_key: Optional[str] = Header(None)):
        """Queue overview: unassigned pending jobs wait for allocation; processing jobs hold accounts."""
        auth(x_api_key); ensure_schema()
        with db() as c:
            counts = {
                row["status"]: row["count"]
                for row in c.execute("SELECT status,COUNT(*) AS count FROM jobs GROUP BY status")
            }
            unassigned = c.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','ready') AND account_id IS NULL"
            ).fetchone()[0]
            # 同为 queued，但含义不同：已放行的是在等名额/账号，未放行的在等人工点“进入调度”。
            dispatched = c.execute(
                "SELECT COUNT(*) FROM jobs j JOIN video_web_jobs w ON j.id=w.job_id "
                "WHERE j.status IN ('queued','ready') AND COALESCE(w.dispatched_at,'')!=''"
            ).fetchone()[0]
        processing = sum(counts.get(s, 0) for s in ACTIVE)
        pending = counts.get("queued", 0) + counts.get("ready", 0)
        return {"pending": pending,
                "pending_unassigned": unassigned,
                "pending_dispatched": dispatched,
                "pending_undispatched": pending - dispatched,
                "processing": processing,
                "processing_limit": local_processing_limit()}

    @app.post("/api/jobs/{jobid}/account", tags=["Doubao Web Video"])
    async def assign_queued_account(jobid: str, payload: QueueAccountRequest,
                                    x_api_key: Optional[str] = Header(None)):
        """Assign or clear the account of a job that has not started executing yet.

        Clearing the selection restores automatic allocation. A selected account is
        re-read and its quota is reserved only when the worker actually starts.
        """
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        if item["status"] not in ("queued", "ready"):
            raise HTTPException(409, "只有待处理任务可以更换账号；任务已开始执行")
        new_account = (payload.account_id or "").strip()
        changed_account = new_account != (item.get("account_id") or "")
        if new_account:
            account = get_account(new_account)
            if account["archived"]: raise HTTPException(409, "账号已归档")
            if bool(account_value(account, "paused", 0)):
                raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        if changed_account:
            # 任务可能带着上一个账号的预占（显式指定账号建单的任务），换人时先归还。
            release_job_quota_hold(jobid)
        with db() as c:
            c.execute("UPDATE jobs SET account_id=?,updated_at=? WHERE id=?",
                      (new_account or None, ts(), jobid))
            if changed_account:
                c.execute("UPDATE video_web_jobs SET model='' WHERE job_id=?", (jobid,))
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (jobid, item["status"],
                       f"操作者把账号改为 {account_label(new_account)}" if new_account
                       else "操作者取消账号，任务将在进入处理前自动分配", ts()))
        return detail(jobid)

    def job_video_path(jobid: str) -> Path | None:
        """Resolve a job's archived video on disk.

        ``video_path`` 历史上既可能相对 base 目录（自动下载写入），也可能相对
        downloads 目录（老数据/手工导入）。绝对路径直接使用；相对路径依次在
        base、downloads 下解析，都找不到再退回「当前工作目录」，避免因启动目录
        与挂载目录不一致而误判文件不存在。
        """
        row = get_job(jobid)
        raw = (row["video_path"] or "") if row else ""
        if not raw:
            return None
        path = Path(raw)
        if path.is_absolute():
            return path if path.is_file() else None
        for root in (base, downloads, Path.cwd()):
            candidate = root / raw
            try:
                candidate = candidate.resolve()
            except OSError:
                continue
            if candidate.is_file():
                return candidate
        return None

    @app.post("/api/jobs/{jobid}/archive", tags=["Doubao Web Video"])
    async def archive_finished_job(jobid: str, x_api_key: Optional[str] = Header(None)):
        """User action: move a finished job from 处理完 to 已归档."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        if item["web"].get("archived_at"):
            return item
        if item["status"] != "downloaded":
            raise HTTPException(409, "只有处理完（视频已下载校验）的任务可以归档")
        if jobid in pending and not pending[jobid].done():
            raise HTTPException(409, "任务仍在收尾（可能正在回推服务器），请稍后再归档")
        row = get_job(jobid)
        if not (row["video_path"] or ""):
            raise HTTPException(409, "任务还没有本地视频，不能归档")
        path = job_video_path(jobid)
        if path is None or path.stat().st_size <= 0:
            raise HTTPException(409, "本地视频文件不存在，请重新下载后再归档")
        with db() as c:
            now = ts()
            c.execute("UPDATE video_web_jobs SET archived_at=? WHERE job_id=?", (now, jobid))
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (jobid, "downloaded", "操作者手动归档", now))
        return detail(jobid)

    @app.delete("/api/jobs/{jobid}/archive", tags=["Doubao Web Video"])
    async def unarchive_job(jobid: str, x_api_key: Optional[str] = Header(None)):
        """User action: move a job back from 已归档 to 处理完 (files are kept)."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        if not item["web"].get("archived_at"):
            return item
        with db() as c:
            now = ts()
            c.execute("UPDATE video_web_jobs SET archived_at='' WHERE job_id=?", (jobid,))
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (jobid, item["status"], "操作者取消归档，任务回到处理完", now))
        return detail(jobid)

    def create_generation(req: VideoRequest, *, retry_of_job_id: str = "", avoid_account_id: str = ""):
        """Persist a generation request and optionally start it. Shared by the JSON and multipart entry points."""
        req.submit_mode = req.submit_mode or local_submit_mode()
        # 人工发送必须由本机操作者显式启动：即使调用方传 auto_start=true 也只建单排队，
        # 防止 H3/外部调度器在用户不在电脑前时替人完成网页发送。
        if req.submit_mode == "manual":
            req.auto_start = False
        # 建单阶段不分配账号：调用方显式给了账号就沿用，否则任务以“待分配账号”进入待处理，
        # 真正开始执行前（start_worker）才挑选账号并预占额度，避免账号和额度被排队任务提前占住。
        account = None
        if req.account_id:
            account = get_account(req.account_id)
            if account["archived"]: raise HTTPException(409, "账号已归档")
            if bool(account_value(account, "paused", 0)):
                raise HTTPException(409, "账号已暂停使用，请先恢复账号")
            enforce_account_model(req, account)
        resolve_assets(req)
        fp = fingerprint(req)
        if req.idempotency_key:
            with db() as c:
                previous = c.execute("SELECT job_id,request_fingerprint FROM video_web_jobs WHERE idempotency_key=?",
                                     (req.idempotency_key,)).fetchone()
            if previous:
                if previous["request_fingerprint"] and previous["request_fingerprint"] != fp:
                    raise HTTPException(409, "幂等键已用于不同请求")
                if req.task_name.strip() or req.result_push_url:
                    with db() as c:
                        c.execute("""UPDATE video_web_jobs
                                     SET task_name=CASE WHEN task_name='' THEN ? ELSE task_name END,
                                         result_push_url=CASE WHEN ?!='' THEN ? ELSE result_push_url END,
                                         result_push_token=CASE WHEN ?!='' THEN ? ELSE result_push_token END
                                     WHERE job_id=?""",
                                  (req.task_name.strip(), req.result_push_url or "", req.result_push_url or "",
                                   req.result_push_token or "", req.result_push_token or "", previous["job_id"]))
                return detail(previous["job_id"])
        jobid = "job_" + uuid.uuid4().hex[:12]
        stamp = ts()
        payload = req.model_dump(include={"first_frame_asset_id", "last_frame_asset_id", "reference_asset_ids"}, exclude_none=True)
        try:
            with db() as c:
                if req.account_id and not local_ignore_quota_check():
                    remaining = quota_summary(c, req.account_id)["remaining_seconds"]
                    if req.duration > remaining:
                        raise HTTPException(
                            409,
                            f"账号当日剩余额度不足（需 {req.duration} 秒，剩余 {remaining} 秒）",
                        )
                c.execute("INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)"
                          " VALUES(?,?,?,?,?,?,'queued',?,?)",
                          (jobid, req.project_id, req.episode_id, req.shot_id, req.prompt, req.account_id, stamp, stamp))
                c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,idempotency_key,mode,assets_json,request_fingerprint,model,quota_day,quota_state,task_name,retry_of_job_id,submit_mode,result_push_url,result_push_token,avoid_account_id)"
                          " VALUES(?,?,?,?,?,?,?,?,?,'',?,?,?,?,?,?)",
                          (jobid, req.ratio, req.duration, req.idempotency_key, req.mode, json.dumps(payload), fp,
                           req.model or "", "", req.task_name.strip(), retry_of_job_id, req.submit_mode,
                           req.result_push_url or "", req.result_push_token or "", avoid_account_id or ""))
                # 只有调用方显式指定账号时才在建单阶段预占额度；待分配任务等分配账号时再预占。
                if req.account_id:
                    reserve_quota(c, req.account_id, req.duration)
                    c.execute("UPDATE video_web_jobs SET quota_day=?, quota_state='reserved' WHERE job_id=?",
                              (local_day(), jobid))
        except sqlite3.IntegrityError:
            if req.idempotency_key:
                with db() as c:
                    existing = c.execute("SELECT job_id,request_fingerprint FROM video_web_jobs WHERE idempotency_key=?",
                                         (req.idempotency_key,)).fetchone()
                if existing:
                    if existing["request_fingerprint"] != fp: raise HTTPException(409, "幂等键冲突")
                    if req.task_name.strip() or req.result_push_url:
                        with db() as c:
                            c.execute("""UPDATE video_web_jobs
                                         SET task_name=CASE WHEN task_name='' THEN ? ELSE task_name END,
                                             result_push_url=CASE WHEN ?!='' THEN ? ELSE result_push_url END,
                                             result_push_token=CASE WHEN ?!='' THEN ? ELSE result_push_token END
                                         WHERE job_id=?""",
                                      (req.task_name.strip(), req.result_push_url or "", req.result_push_url or "",
                                       req.result_push_token or "", req.result_push_token or "", existing["job_id"]))
                    return detail(existing["job_id"])
            raise HTTPException(409, "任务冲突")
        emit(jobid, "queued",
             "手动任务已排队：点击“进入调度”后将自动准备并自动发送"
             if req.submit_mode == "manual" else "任务已写入持久化队列")
        # avoid_account_id 只在本次自动启动时生效：账号真正分配到那一刻才用得上。
        if req.auto_start: start_worker(jobid, req, avoid_account_id)
        return detail(jobid)

    @app.post("/api/v1/videos/generations", status_code=202, tags=["Doubao Web Video"])
    async def create(req: VideoRequest, x_api_key: Optional[str] = Header(None)):
        auth(x_api_key); ensure_schema()
        return create_generation(req)

    def start_worker(jobid, req, avoid_account_id: str = ""):
        """Admit a queued job to the processing queue.

        账号不在这里分配：任务先排队等处理名额，真正拿到名额、进入处理前那一刻
        才在 worker 里挑选账号并预占额度（见 worker 中的 allocate_account）。
        """
        # Keep persisted status queued until account and global processing slots are free.
        # 先挡住重复放行：额度检查会写库（跨天重置），账号检查会取消空闲关窗任务，
        # 这些副作用必须发生在 409 判定之后，否则被拒绝的请求会带着“额度不足”这类
        # 误导性原因落地，而真实原因是“已在执行”。
        task = pending.get(jobid)
        if task is not None and not task.done():
            raise HTTPException(409,"任务已经在等待或执行，拒绝重复提交")
        # 操作者在进入处理前手动指定过账号的，沿用那个账号；否则保持未分配。
        if not req.account_id:
            with db() as c:
                row = c.execute("SELECT account_id,status FROM jobs WHERE id=?", (jobid,)).fetchone()
            if not row:
                raise HTTPException(404, "分镜任务不存在")
            if row["status"] not in ("queued", "ready"):
                raise HTTPException(409, "任务当前状态不允许开始执行")
            if row["account_id"]:
                req.account_id = row["account_id"]
        if req.account_id:
            account = get_account(req.account_id)
            idle_close = idle_close_tasks.pop(req.account_id, None)
            if idle_close is not None and not idle_close.done():
                idle_close.cancel()
            if bool(account_value(account, "paused", 0)):
                raise HTTPException(409, "账号已暂停使用，请先恢复账号")
            enforce_account_model(req, account)
        with db() as c:
            if req.account_id and bool(account_value(get_account(req.account_id), "is_member", 0)):
                c.execute("UPDATE video_web_jobs SET model=? WHERE job_id=?", (MEMBER_MODEL, jobid))
            row = c.execute("SELECT status FROM jobs WHERE id=?", (jobid,)).fetchone()
            if not row or row["status"] != "queued":
                raise HTTPException(409,"任务可能已提交或处理中，拒绝重复提交")
        # 所有 409 都已发生，才落下“已交给调度器”的标记：额度不足这类拒绝
        # 不该留下一个没有 worker 的已放行假象。
        if req.account_id:
            ensure_start_quota(jobid, req.account_id)
        with db() as c:
            c.execute("UPDATE video_web_jobs SET dispatched_at=? WHERE job_id=?", (ts(), jobid))
        pending[jobid] = asyncio.create_task(worker(jobid, req, avoid_account_id))

    @app.get("/api/v1/videos/generations/{jobid}",tags=["Doubao Web Video"])
    async def query(jobid:str,x_api_key:Optional[str]=Header(None)):
        auth(x_api_key); ensure_schema(); return detail(jobid)

    @app.get("/api/v1/videos/generations/{jobid}/events",tags=["Doubao Web Video"])
    async def events(jobid:str,x_api_key:Optional[str]=Header(None)):
        auth(x_api_key); ensure_schema(); detail(jobid)
        with db() as c:
            return [dict(r) for r in c.execute("SELECT status,message,timestamp FROM video_web_events WHERE job_id=? ORDER BY id",(jobid,))]

    def extension_task_actions(item: dict) -> list[dict]:
        status = item.get("status") or ""
        web = item["web"]
        recovering = bool((item.get("recovery") or {}).get("active"))
        live_page = prepared_pages.get(item["id"])
        try:
            has_live_page = live_page is not None and not live_page.is_closed()
        except Exception:
            has_live_page = False
        has_page = bool(
            has_live_page
            or web.get("session_url")
            or web.get("web_page_url")
        )
        pending_status = status in ("queued", "ready")
        dispatched = pending_status and bool(web.get("dispatched_at"))
        can_prepare = status in (
            "queued", MANUAL_WAIT, "confirming", "generating", "needs_review", "failed"
        )
        first_key = "dispatch" if pending_status else "prepare"
        first_label = (
            "已放行 · 排队中" if dispatched
            else "进入调度" if pending_status
            else "提交素材"
        )
        busy_reason = "Claude 正在处理该任务，请等待恢复结束"
        return [
            {
                "key": first_key,
                "label": first_label,
                "enabled": not recovering and can_prepare and not dispatched,
                "reason": busy_reason if recovering else (
                    "任务已进入调度，无需重复点击" if dispatched else "当前状态不能提交素材"
                ),
                "confirm": "确认把当前任务交给调度器自动执行？" if pending_status
                else "确认按当前任务进度重新准备素材与提示词？",
            },
            {
                "key": "send",
                "label": "确认发送",
                "enabled": not recovering and status == MANUAL_WAIT and has_page,
                "reason": busy_reason if recovering else "仅已准备完成的任务可以确认发送",
                "confirm": "确认发送当前任务已经准备好的素材与提示词？",
            },
            {
                "key": "confirm",
                "label": "发送确认语",
                "enabled": not recovering and status in (MANUAL_WAIT, "confirming", "needs_review") and has_page,
                "reason": busy_reason if recovering else "当前状态不能发送确认语",
                "confirm": "确认向当前任务网页发送所选确认语？此操作可能继续视频生成。",
            },
            {
                "key": "download",
                "label": "下载视频",
                "enabled": not recovering and status in (
                    "downloaded", "needs_review", "failed", MANUAL_WAIT, "confirming", "generating"
                ) and has_page,
                "reason": busy_reason if recovering else "当前任务还没有可关联的任务网页",
                "confirm": "确认从当前任务绑定的网页核验并下载视频？系统不会重新提交生成。",
            },
            {
                "key": "push",
                "label": "再次推送服务器" if web.get("result_pushed_at") else "推送服务器",
                "enabled": not recovering and bool(item.get("video_path")) and bool(web.get("result_push_url")),
                "reason": busy_reason if recovering else "需要已归档视频和服务器推送配置",
                "confirm": "确认把当前任务已归档的视频推送到服务器？",
            },
        ]

    @app.get("/api/v1/extension/tasks/resolve", tags=["Chrome Extension"])
    async def resolve_extension_task(
        binding_token: str, x_api_key: Optional[str] = Header(None)
    ):
        """Resolve only an explicit tab binding; never infer a task from URL or title."""
        auth(x_api_key); ensure_schema()
        token = binding_token.strip()
        if len(token) < 32 or len(token) > 200:
            raise HTTPException(404, "当前网页没有有效的任务绑定")
        with db() as c:
            row = c.execute(
                "SELECT job_id FROM video_web_jobs WHERE extension_binding_token=?",
                (token,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "当前网页没有绑定分镜任务")
            recent = c.execute(
                "SELECT status,message,timestamp FROM video_web_events "
                "WHERE job_id=? ORDER BY id DESC LIMIT 10",
                (row["job_id"],),
            ).fetchall()
        item = detail(row["job_id"])
        account_name = ""
        if item.get("account_id"):
            try:
                account_name = account_label(item["account_id"])
            except Exception:
                account_name = item["account_id"]
        events_out = [dict(event) for event in reversed(recent)]
        last_event = events_out[-1] if events_out else {}
        return {
            "job_id": item["id"],
            "account_id": item.get("account_id") or "",
            "account_name": account_name,
            "project_id": item.get("project_id") or "",
            "episode_id": item.get("episode_id") or "",
            "shot_id": item.get("shot_id") or "",
            "task_name": item.get("task_name") or "",
            "status": item.get("status") or "",
            "submit_mode": item["web"].get("submit_mode") or "auto",
            "error_code": item["web"].get("error_code") or "",
            "error_message": item.get("error_message") or "",
            "updated_at": item.get("updated_at") or "",
            "last_action_at": last_event.get("timestamp") or item.get("updated_at") or "",
            "last_action": last_event.get("message") or "",
            "recovery": item.get("recovery") or {},
            "events": events_out,
            "actions": extension_task_actions(item),
            "confirmation_presets": confirmation_presets(),
        }

    @app.post("/api/v1/videos/generations/{jobid}/start",tags=["Doubao Web Video"])
    async def start(jobid:str,x_api_key:Optional[str]=Header(None)):
        auth(x_api_key); ensure_schema()
        req = assemble_req(jobid)
        resolve_assets(req)
        if req.account_id and get_account(req.account_id)["archived"]: raise HTTPException(409,"账号已归档")
        start_worker(jobid, req, avoid_account_for(jobid))
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/dispatch", tags=["Doubao Web Video"])
    async def dispatch_job(jobid: str, x_api_key: Optional[str] = Header(None)):
        """把待处理任务放行进入调度队列，之后全程自动执行。

        手动模式下建单只入“待处理”，不占用调度名额；操作者点“进入调度”即视为
        授权自动执行：任务的处理方式随之转为 auto，从准备素材到点击发送全部交给
        机器（仍遵守正在处理队列上限）。想保留人工点击发送的半自动流程，请走
        prepare-manual / start 准备页面后再 send。
        """
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        if item["status"] not in ("queued", "ready"):
            raise HTTPException(409, "只有待处理任务可以进入调度")
        task = pending.get(jobid)
        if task is not None and not task.done():
            raise HTTPException(409, "任务已经在等待或执行，无需重复进入调度")
        req = assemble_req(jobid)
        resolve_assets(req)
        if req.account_id and get_account(req.account_id)["archived"]:
            raise HTTPException(409, "账号已归档")
        # 点击“进入调度”即授权自动执行：把处理方式落库为 auto，准备、发送、确认、
        # 下载全程交给机器，结果推送等下游逻辑也能按 auto 一致地工作。
        if (req.submit_mode or "auto") != "auto":
            with db() as c:
                c.execute("UPDATE video_web_jobs SET submit_mode='auto' WHERE job_id=?", (jobid,))
            req.submit_mode = "auto"
        start_worker(jobid, req, avoid_account_for(jobid))
        return detail(jobid)

    async def reset_generation(jobid: str, *, rematch_account: bool = False):
        """Reset an existing generation to queued without opening or submitting a page."""
        item = detail(jobid)
        if item["status"] == "downloaded":
            raise HTTPException(409,"任务已归档，无需重新开始")
        if item["status"] not in ("queued", MANUAL_WAIT, "confirming", "generating", "needs_review", "failed"):
            raise HTTPException(409,"任务正在执行；请等待当前浏览器操作结束后再重新开始")
        task = pending.get(jobid)
        if task is not None and not task.done():
            raise HTTPException(409,"该任务正在执行，请稍后再重新开始")
        req = assemble_req(jobid)
        resolve_assets(req)
        previous_account_id = req.account_id or ""
        if rematch_account:
            req.account_id = None
        if req.account_id and get_account(req.account_id)["archived"]:
            raise HTTPException(409,"账号已归档")
        with db() as c:
            w = c.execute("SELECT duration, quota_day, quota_state FROM video_web_jobs WHERE job_id=?",
                          (jobid,)).fetchone()
            if not w:
                raise HTTPException(404,"视频生成任务不存在")
            dur = int(w["duration"] or 0)
            day = local_day()
            state = w["quota_state"] or ""
            if state == "consumed":
                raise HTTPException(409,"任务额度已结算为已消耗，不能原任务重发")
            new_attempt = item["status"] != "queued"
            if new_attempt:
                # A reset after an attempted send is a new generation intent. Settle
                # the previous attempt before creating a separate current reservation;
                # unknown means it may already have spent quota, so keep it charged.
                if state == "unknown":
                    settle_quota(c, jobid, "downloaded", prior_status=item["status"])
                elif state in ("", "reserved"):
                    terminal_status = "failed" if item["status"] == "failed" else "needs_review"
                    settle_quota(
                        c,
                        jobid,
                        terminal_status,
                        prior_status=item["status"],
                        error_code=item["web"].get("error_code") or "",
                    )
                    w = c.execute(
                        "SELECT duration, quota_day, quota_state "
                        "FROM video_web_jobs WHERE job_id=?",
                        (jobid,),
                    ).fetchone()
                    if (w["quota_state"] or "") == "unknown":
                        settle_quota(c, jobid, "downloaded", prior_status=item["status"])

            w = c.execute(
                "SELECT duration, quota_day, quota_state "
                "FROM video_web_jobs WHERE job_id=?",
                (jobid,),
            ).fetchone()
            state = w["quota_state"] or ""
            held_today = quota_held_today(w)
            # 未分配账号的待处理任务没有额度占用，等 start_worker 分配账号时再预占。
            if req.account_id and (new_attempt or not held_today):
                if (state in ("", "reserved") and w["quota_day"]
                        and w["quota_day"] != day):
                    c.execute(
                        "UPDATE account_quota SET reserved_seconds=MAX(0,reserved_seconds-?) "
                        "WHERE account_id=? AND day=?",
                        (dur, req.account_id, w["quota_day"]),
                    )
                remaining = quota_summary(c, req.account_id)["remaining_seconds"]
                if dur > remaining and not local_ignore_quota_check():
                    raise HTTPException(409,f"账号当日剩余额度不足（需 {dur} 秒，剩余 {remaining} 秒）")
                reserve_quota(c, req.account_id, dur, day)
                c.execute("UPDATE video_web_jobs SET quota_day=?, quota_state='reserved' WHERE job_id=?",
                          (day, jobid))
            now = ts()
            claimed = c.execute(
                "UPDATE jobs SET status='queued', error_message='', account_id=?, updated_at=? "
                "WHERE id=? AND status=?",
                (req.account_id, now, jobid, item["status"]))
            if not claimed.rowcount:
                raise HTTPException(409,"任务状态已变化，请刷新后再试")
            quota_reset = ",quota_day='',quota_state=''" if rematch_account else ""
            c.execute(f"""UPDATE video_web_jobs
                         SET web_page_url='', session_url='', session_id='',
                             result_card_index=NULL, video_url='', result_source='',
                             error_code='', error_class='', next_retry_at='',
                             recall_state='', recall_token='', recall_started_at='',
                             dispatched_at='',
                             phase='queued', phase_started_at=?, last_heartbeat_at=?{quota_reset}
                         WHERE job_id=?""", (now, now, jobid))
            message = (
                f"任务已重置为待执行，解除原账号 {previous_account_id}，将重新匹配可用账号"
                if rematch_account
                else "任务已重置为待执行，旧会话与旧结果不再关联"
            )
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (jobid,"queued",message,ts()))
        await release_processing_job(jobid)
        old = prepared_pages.pop(jobid, None)
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass
        return req

    @app.post("/api/v1/videos/generations/{jobid}/prepare-manual", tags=["Doubao Web Video"])
    async def prepare_manual(jobid: str, x_api_key: Optional[str] = Header(None)):
        """Prepare the task page without sending, regardless of its saved submit mode."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        if item.get("account_id") and bool(account_value(get_account(item["account_id"]), "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        if item["status"] == "queued":
            req = assemble_req(jobid)
            resolve_assets(req)
            if req.account_id and get_account(req.account_id)["archived"]:
                raise HTTPException(409, "账号已归档")
        else:
            req = await reset_generation(jobid)
        req.submit_mode = "manual"
        start_worker(jobid, req, avoid_account_for(jobid))
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/restart-auto", tags=["Doubao Web Video"])
    async def restart_auto(jobid: str, x_api_key: Optional[str] = Header(None)):
        """Restart an idle unfinished task and match a fresh available account."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        previous_account_id = item.get("account_id") or ""
        if item["status"] == "queued":
            release_job_quota_hold(jobid)
        req = await reset_generation(jobid, rematch_account=True)
        req.submit_mode = "auto"
        start_worker(jobid, req, previous_account_id or avoid_account_for(jobid))
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/reset",tags=["Doubao Web Video"])
    async def reset(jobid:str,confirm_reset:bool=False,x_api_key:Optional[str]=Header(None)):
        """Reset status and old result binding only; never opens a page or submits."""
        auth(x_api_key); ensure_schema()
        if not confirm_reset:
            raise HTTPException(422,"必须明确确认任务重新开始")
        await reset_generation(jobid)
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/resend",tags=["Doubao Web Video"])
    async def resend(jobid:str,confirm_resend:bool=False,x_api_key:Optional[str]=Header(None)):
        """Backward-compatible reset-and-start operation, keeping the original job ID."""
        auth(x_api_key); ensure_schema()
        if not confirm_resend:
            raise HTTPException(422,"必须明确确认重新执行（可能再次消耗视频额度）")
        item = detail(jobid)
        if item.get("account_id") and bool(account_value(get_account(item["account_id"]), "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        req = await reset_generation(jobid)
        start_worker(jobid, req, avoid_account_for(jobid))
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/mark-failed",tags=["Doubao Web Video"])
    async def mark_failed(jobid:str,confirm_failed:bool=False,x_api_key:Optional[str]=Header(None)):
        """操作者人工判定失败：H3/外部调度会收到 failed，停止继续等待该任务。"""
        auth(x_api_key); ensure_schema()
        if not confirm_failed:
            raise HTTPException(422,"必须明确确认人工判定失败")
        item = detail(jobid)
        if (item["web"].get("submit_mode") or "auto") != "manual":
            raise HTTPException(409,"仅人工发送任务支持人工判定失败")
        if item["status"] == "downloaded":
            raise HTTPException(409,"任务已归档，不能判定失败")
        if item["status"] not in ("queued", MANUAL_WAIT, "needs_review", "failed"):
            raise HTTPException(409,"任务正在执行，请等待当前步骤结束再判定失败")
        task = pending.get(jobid)
        if task is not None and not task.done():
            raise HTTPException(409,"该任务正在执行，请稍后再判定")
        if item["status"] == "failed" and item["web"].get("error_code") == MANUAL_FAILED:
            return item
        emit(jobid,"failed","人工判定失败：H3/外部调度将收到失败状态，不再等待该任务",code=MANUAL_FAILED)
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/copy", status_code=202,
              tags=["Doubao Web Video"])
    async def copy_generation(jobid: str, confirm_copy: bool = False,
                              x_api_key: Optional[str] = Header(None)):
        """Create a queued job from the original server-submitted request parameters."""
        auth(x_api_key); ensure_schema()
        if not confirm_copy:
            raise HTTPException(422, "必须明确确认复制为新任务（可能再次消耗视频额度）")
        req = assemble_req(jobid)
        original_account_id = req.account_id or ""
        # 复制的任务同样先进入待处理、不绑定账号；分配时优先避开原账号。
        req.account_id = None
        req.idempotency_key = None
        req.auto_start = False
        return create_generation(req, avoid_account_id=original_account_id)

    @app.post("/api/v1/videos/generations/{jobid}/retry", status_code=202,
              tags=["Doubao Web Video"])
    async def retry_generation(jobid: str, confirm_retry: bool = False,
                               x_api_key: Optional[str] = Header(None)):
        """Clone a terminal unfinished task and enqueue it with a newly selected account."""
        auth(x_api_key); ensure_schema()
        if not confirm_retry:
            raise HTTPException(422, "必须明确确认重新生成（可能再次消耗视频额度）")
        original = detail(jobid)
        if original["status"] not in ("needs_review", "failed"):
            raise HTTPException(409, "仅需人工处理或失败的未完成任务可以重新生成")
        with db() as c:
            existing = c.execute(
                """SELECT j.id,j.status FROM jobs j JOIN video_web_jobs w ON w.job_id=j.id
                   WHERE w.retry_of_job_id=? AND j.status NOT IN ('needs_review','failed')
                   ORDER BY j.created_at DESC LIMIT 1""", (jobid,)).fetchone()
        if existing:
            raise HTTPException(409, f"该任务已有重试任务 {existing['id']}（{existing['status']}），不会重复提交")
        req = assemble_req(jobid)
        previous_account_id = req.account_id or ""
        req.account_id = None
        req.idempotency_key = None
        req.auto_start = True
        base_name = (req.task_name or original.get("task_name") or jobid).strip()
        req.task_name = (base_name + " · 重试")[:160]
        return create_generation(req, retry_of_job_id=jobid,
                                 avoid_account_id=previous_account_id)

    @app.post("/api/v1/videos/generations/from-job/{jobid}",status_code=202,tags=["Doubao Web Video"])
    async def from_job(jobid:str, submit_mode: str = "auto", x_api_key:Optional[str]=Header(None)):
        auth(x_api_key); ensure_schema()
        if submit_mode not in ("auto", "manual"):
            raise HTTPException(422, "submit_mode 必须是 auto 或 manual")
        row = get_job(jobid)
        if row["status"] != "queued":
            raise HTTPException(409, "分镜必须是 queued（待处理）状态")
        if row["account_id"] and bool(account_value(get_account(row["account_id"]), "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        req = VideoRequest(account_id=row["account_id"],project_id=row["project_id"],
                           episode_id=row["episode_id"],shot_id=row["shot_id"],prompt=row["prompt"],
                           submit_mode=submit_mode)
        with db() as c:
            if c.execute("SELECT 1 FROM video_web_jobs WHERE job_id=?",(jobid,)).fetchone():
                raise HTTPException(409,"分镜已经是网页视频任务")
            # 转网页任务也不分配账号：额度在进入处理前分配账号时再预占。
            c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,mode,assets_json,quota_day,quota_state,submit_mode) VALUES(?,?,?,?,?,?,'',?)",
                      (jobid,req.ratio,req.duration,req.mode,"{}","",submit_mode))
            if req.account_id:
                remaining = quota_summary(c, req.account_id)["remaining_seconds"]
                if req.duration > remaining and not local_ignore_quota_check():
                    raise HTTPException(
                        409, f"账号当日剩余额度不足（需 {req.duration} 秒，剩余 {remaining} 秒）"
                    )
                reserve_quota(c, req.account_id, req.duration)
                c.execute("UPDATE video_web_jobs SET quota_day=?, quota_state='reserved' WHERE job_id=?",
                          (local_day(), jobid))
        if submit_mode == "manual":
            emit(jobid, "queued", "人工发送任务已排队；请在任务列表点击“发送到豆包”")
        else:
            start_worker(jobid, req, avoid_account_for(jobid))
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/reconcile",tags=["Doubao Web Video"])
    async def reconcile(jobid:str,x_api_key:Optional[str]=Header(None)):
        """Re-read a saved result URL without re-submitting anything."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        if item["status"]!="needs_review": raise HTTPException(409,"仅允许核对需人工复查的任务")
        url = item["web"]["web_page_url"]
        if not url: raise HTTPException(409,"无已保存的任务页，请人工检查豆包历史记录")
        # Generic creation pages can show somebody else's completed video. In
        # that case we cannot attribute the result to this job after a crash.
        verified_video_url = item["web"]["video_url"]
        if not verified_video_url:
            raise HTTPException(409,"缺少与当前任务绑定的结果视频URL，禁止把历史视频当成当前结果；请人工核实并使用人工归档接口")
        p=urlparse(url)
        if p.scheme!="https" or p.hostname not in ("doubao.com","www.doubao.com"):
            if not (os.getenv("DOUBAO_TEST_MODE")=="1" and p.scheme=="http" and p.hostname in ("127.0.0.1","localhost")):
                raise HTTPException(422,"页面 URL 非受信任域")
        if jobid in pending: raise HTTPException(409,"任务正在执行")
        ctx = await browser(item["account_id"])
        page = await ctx.new_page()
        await bind_task_page(jobid, page)
        await page.goto(url,wait_until="domcontentloaded",timeout=60000)
        await page.wait_for_timeout(1500)
        state = await read_state(page)
        if not (state["done"] and not state["running"]):
            return {"ok":False,"status":"needs_review","message":"未确认完成，未重复发送生成请求"}
        # 卡片序号就是本任务结果的绑定关系；不凭“页面上最后一个视频”猜测。
        cards = int(state.get("videoCards") or 0)
        raw_index = item["web"].get("result_card_index")
        card_index = int(raw_index) if raw_index is not None else (0 if cards == 1 else None)
        if card_index is None or card_index >= cards:
            return {"ok":False,"status":"needs_review",
                    "message":"无法确认任务绑定的视频卡片，未下载（禁止把历史视频当成当前结果）"}
        target = downloads / "library" / safe(item["project_id"]) / safe(item["episode_id"]) / safe(item["shot_id"] or jobid) / (jobid+".mp4")
        result = await download_result_video(page, set(state.get("videoUrls") or []), target,
                                             card_index=card_index, allow_known=True)
        await finish_downloaded(
            jobid, "人工复查成功：从详情面板下载无水印成片，无重复生成",
            {"video_url": result["video_url"], "web_page_url": url, "output": result["output"]},
            close_account_after=False)
        await cleanup_downloaded_browser(jobid, item["account_id"], page=page)
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/manual-result",tags=["Doubao Web Video"])
    async def manual_result(jobid: str, file: UploadFile = File(...),
                            confirm_review: bool = Form(False), x_api_key: Optional[str] = Header(None)):
        """Explicit human-reviewed result attachment. Never resends a generation request."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        if item["status"] != "needs_review":
            raise HTTPException(409, "仅允许复查状态下上传经人工核对的结果")
        if not confirm_review:
            raise HTTPException(422, "必须明确确认已在豆包网页核对视频与分镜归属")
        if jobid in pending:
            raise HTTPException(409, "任务正在运行，不能导入结果")
        name = (file.filename or "").lower()
        if not name.endswith((".mp4", ".webm")):
            raise HTTPException(415, "仅接受 MP4 或 WEBM 视频")
        folder = downloads / "library" / safe(item["project_id"]) / safe(item["episode_id"]) / safe(item["shot_id"] or jobid)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (jobid + ".uploading")
        digest = hashlib.sha256()
        total = 0
        try:
            with tmp.open("wb") as dest:
                while True:
                    piece = await file.read(1024 * 1024)
                    if not piece: break
                    total += len(piece)
                    if total > MAX_VIDEO: raise HTTPException(413, "视频不得超过600MB")
                    digest.update(piece)
                    dest.write(piece)
            with tmp.open("rb") as source: magic = source.read(16)
            is_mp4 = magic[4:8] == b"ftyp"
            is_webm = magic[:4] == b"\x1aE\xdf\xa3"
            if total < 256 or not (is_mp4 or is_webm):
                raise HTTPException(422,"视频文件头验证失败")
            target = folder / (jobid + (".mp4" if is_mp4 else ".webm"))
            tmp.replace(target)
            await finish_downloaded(
                jobid, "人工复查归档 sha256=" + digest.hexdigest(),
                {"video_url": "", "web_page_url": item["web"]["web_page_url"], "output": str(target)},
                result_source="human_verified_upload")
            return detail(jobid)
        finally:
            await file.close()
            tmp.unlink(missing_ok=True)

    @app.post("/api/v1/videos/generations/{jobid}/open-page", tags=["Doubao Web Video"])
    async def open_page(jobid: str, x_api_key: Optional[str] = Header(None)):
        """Open the saved Doubao conversation in the account's managed Chrome profile.

        Opening the URL in the user's default browser would miss the managed login
        profile, so the persistent context is reused (or launched) here instead.
        """
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        account_id = item.get("account_id")
        if not account_id:
            raise HTTPException(409, "任务所属账号已删除，无法用登录态打开网页")
        page_title = concise_job_title(
            item.get("project_id", ""),
            item.get("episode_id", ""),
            item.get("shot_id", ""),
            fallback=item.get("task_name") or jobid,
        )
        # 人工发送任务的标签页可能还没有真实会话 URL，但它本身就是任务页面。
        prepared = prepared_pages.get(jobid)
        if prepared is not None and not prepared.is_closed():
            await bind_task_page(jobid, prepared)
            await set_job_page_title(prepared, page_title)
            await prepared.bring_to_front()
            return {"ok": True, "job_id": jobid, "url": prepared.url,
                    "reused": True, "message": "已打开该任务绑定的网页"}
        url = (item["web"].get("web_page_url") or "").strip()
        if not url:
            raise HTTPException(409, "该任务还没有记录网页地址")
        p = urlparse(url)
        is_test_page = (os.getenv("DOUBAO_TEST_MODE") == "1" and p.scheme == "http"
                        and p.hostname in ("127.0.0.1", "localhost"))
        if not is_test_page:
            if p.scheme != "https" or p.hostname not in ("doubao.com", "www.doubao.com"):
                raise HTTPException(422, "任务网页不是受信任的豆包地址")
            if not CHAT_PAGE_URL.search(url):
                raise HTTPException(409, "该任务只记录了通用对话页，请从豆包历史记录中人工查找具体会话")
        ctx = await browser(account_id)
        key = url.split("?")[0]
        for existing in ctx.pages:
            try:
                if (existing.url or "").split("?")[0] == key:
                    await bind_task_page(jobid, existing)
                    await set_job_page_title(existing, page_title)
                    await existing.bring_to_front()
                    return {"ok": True, "job_id": jobid, "url": url,
                            "reused": True, "message": "已打开该任务绑定的网页"}
            except Exception:
                continue
        page = await ctx.new_page()
        await bind_task_page(jobid, page)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await set_job_page_title(page, page_title)
        await page.bring_to_front()
        return {"ok": True, "job_id": jobid, "url": url,
                "reused": False, "message": "已打开该任务绑定的网页"}

    @app.post("/api/v1/videos/generations/{jobid}/open-window", tags=["Doubao Web Video"])
    async def open_window(jobid: str, x_api_key: Optional[str] = Header(None)):
        """Open the task in its account's managed browser window.

        The browser context is the window boundary. Within that window this endpoint
        reuses or creates only the task's conversation tab and clicks the persisted
        video-card index; it never falls back to another task or the latest video.
        """
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        account_id = item.get("account_id")
        if not account_id:
            raise HTTPException(409, "任务所属账号已删除，无法打开该账号的独立浏览器窗口")

        previous_context = existing_browser(account_id) if existing_browser is not None else None
        ctx = await browser(account_id)
        prepared = prepared_pages.get(jobid)
        if prepared is not None and not prepared.is_closed():
            page = prepared
            page_reused = True
        else:
            prepared_pages.pop(jobid, None)
            url = (item["web"].get("session_url")
                   or item["web"].get("web_page_url") or "").strip()
            if not url:
                raise HTTPException(
                    409,
                    "已打开该账号的浏览器窗口，但任务尚未记录具体豆包会话页；"
                    "请从豆包历史记录中人工查找，系统不会重新生成",
                )
            p = urlparse(url)
            is_test_page = (os.getenv("DOUBAO_TEST_MODE") == "1"
                            and p.scheme == "http"
                            and p.hostname in ("127.0.0.1", "localhost"))
            if not is_test_page:
                if p.scheme != "https" or p.hostname not in ("doubao.com", "www.doubao.com"):
                    raise HTTPException(422, "任务会话地址不是受信任的豆包地址")
                if not CHAT_PAGE_URL.search(url):
                    raise HTTPException(
                        409,
                        "任务只记录了通用对话页，无法定位具体分镜；"
                        "请从豆包历史记录中人工查找，系统不会重新生成",
                    )
            key = url.split("?")[0]
            page = None
            page_reused = False
            for existing in ctx.pages:
                try:
                    if (existing.url or "").split("?")[0] == key:
                        page = existing
                        page_reused = True
                        break
                except Exception:
                    continue
            if page is None:
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(1200)

        await bind_task_page(jobid, page)

        page_title = concise_job_title(
            item.get("project_id", ""),
            item.get("episode_id", ""),
            item.get("shot_id", ""),
            fallback=item.get("task_name") or jobid,
        )
        await set_job_page_title(page, page_title)
        await page.bring_to_front()

        state = await read_state(page)
        live_url = (state.get("url") or getattr(page, "url", "") or "").strip()
        if live_url and CHAT_PAGE_URL.search(live_url):
            session_url, session_id = session_binding(live_url)
            with db() as c:
                c.execute(
                    "UPDATE video_web_jobs SET web_page_url=?,session_url=?,session_id=? "
                    "WHERE job_id=?",
                    (session_url, session_url, session_id, jobid),
                )
        else:
            session_url = item["web"].get("session_url") or ""
            session_id = item["web"].get("session_id") or ""

        raw_index = item["web"].get("result_card_index")
        card_index = int(raw_index) if raw_index is not None else None
        try:
            locate_timeout = max(
                0.0, float(os.getenv("DOUBAO_TASK_LOCATE_TIMEOUT", "15"))
            )
        except ValueError:
            locate_timeout = 15.0
        deadline = asyncio.get_running_loop().time() + locate_timeout
        while True:
            cards = int(state.get("videoCards") or 0)
            if card_index is None and cards == 1:
                card_index = 0
                with db() as c:
                    c.execute(
                        "UPDATE video_web_jobs SET result_card_index=0 WHERE job_id=?",
                        (jobid,),
                    )
            if card_index is not None and 0 <= card_index < cards:
                break
            if asyncio.get_running_loop().time() >= deadline:
                if card_index is None and cards > 1:
                    raise HTTPException(
                        409,
                        f"会话中有 {cards} 个视频，但任务缺少视频卡片绑定；"
                        "为避免打开其他分镜的视频，系统没有猜测，也不会重新生成",
                    )
                if card_index is not None and card_index >= cards:
                    raise HTTPException(
                        409,
                        f"任务绑定的第 {card_index + 1} 个视频卡片未在当前会话出现"
                        f"（当前 {cards} 个）；系统不会改选其他视频，也不会重新生成",
                    )
                raise HTTPException(
                    409,
                    "任务会话已打开，但尚未找到可定位的视频卡片；"
                    "请确认登录状态和生成结果，系统不会重新生成",
                )
            await page.wait_for_timeout(400)
            state = await read_state(page)

        card_clicked = await click_result_card(page, card_index)
        message = (
            "已激活该账号的浏览器窗口，并定位到任务绑定的视频卡片"
            if card_clicked
            else "已激活该账号的浏览器窗口并找到任务视频卡片，但未能打开视频详情"
        )
        return {
            "ok": True,
            "job_id": jobid,
            "account_id": account_id,
            "browser_open": True,
            "window_reused": previous_context is not None,
            "page_reused": page_reused,
            "url": live_url,
            "session_url": session_url,
            "session_id": session_id,
            "video_card_index": card_index,
            "card_clicked": card_clicked,
            "message": message,
        }

    @app.post("/api/v1/videos/generations/{jobid}/close-window", tags=["Doubao Web Video"])
    async def close_window(jobid: str, x_api_key: Optional[str] = Header(None)):
        """Close only this task's account browser window without touching the job."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        account_id = item.get("account_id")
        if not account_id:
            raise HTTPException(409, "任务所属账号已删除，无法定位要关闭的浏览器窗口")
        if close_browser is None:
            raise HTTPException(503, "浏览器窗口管理服务尚未配置")

        for other_jobid, task in list(pending.items()):
            if task.done():
                continue
            try:
                if get_job(other_jobid)["account_id"] == account_id:
                    raise HTTPException(
                        409,
                        "该账号仍有生成任务在执行。关闭窗口不会取消任务，"
                        "请等待当前步骤结束后再关闭",
                    )
            except HTTPException:
                raise
            except Exception:
                continue
        account_pending = getattr(recovery_service, "account_pending", None)
        if account_pending is not None and account_pending(account_id):
            raise HTTPException(
                409,
                "该账号仍有恢复任务在执行。关闭窗口不会取消任务，请稍后再关闭",
            )

        was_open = (existing_browser(account_id) if existing_browser is not None else None) is not None
        await close_browser(account_id)
        for other_jobid, page in list(prepared_pages.items()):
            try:
                if get_job(other_jobid)["account_id"] != account_id:
                    continue
            except Exception:
                continue
            prepared_pages.pop(other_jobid, None)
        with db() as c:
            current = c.execute("SELECT status FROM jobs WHERE id=?", (jobid,)).fetchone()
        return {
            "ok": True,
            "job_id": jobid,
            "account_id": account_id,
            "browser_open": False,
            "closed": was_open,
            "already_closed": not was_open,
            "status": current["status"] if current else item.get("status"),
            "message": (
                "已关闭该账号的浏览器窗口；其他账号不受影响，登录状态和任务记录均已保留"
                if was_open
                else "该账号的浏览器窗口已经关闭；登录状态和任务记录均已保留"
            ),
        }

    @app.post("/api/v1/videos/generations/{jobid}/close-page", tags=["Doubao Web Video"])
    async def close_page(jobid: str, x_api_key: Optional[str] = Header(None)):
        """Close only this job's tab without closing the account's Chrome context."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        account_id = item.get("account_id")
        if not account_id:
            raise HTTPException(409, "任务所属账号已删除，无法定位任务网页")
        task = pending.get(jobid)
        if task is not None and not task.done():
            raise HTTPException(409, "任务正在执行，暂不能关闭任务网页")

        prepared = prepared_pages.get(jobid)
        if prepared is not None and not prepared.is_closed():
            try:
                await prepared.close()
            except Exception as exc:
                raise HTTPException(500, f"关闭任务网页失败：{str(exc)[:300]}") from exc
            if not prepared.is_closed():
                raise HTTPException(500, "浏览器未能关闭任务网页，请稍后重试")
            prepared_pages.pop(jobid, None)
            return {"ok": True, "job_id": jobid, "closed": True, "already_closed": False}
        if prepared is not None:
            prepared_pages.pop(jobid, None)

        url = (item["web"].get("web_page_url") or "").strip()
        if not url:
            if prepared is not None:
                return {"ok": True, "job_id": jobid, "closed": False, "already_closed": True}
            raise HTTPException(409, "该任务还没有记录网页地址")
        parsed = urlparse(url)
        is_test_page = (
            os.getenv("DOUBAO_TEST_MODE") == "1"
            and parsed.scheme == "http"
            and parsed.hostname in ("127.0.0.1", "localhost")
        )
        if not is_test_page:
            if parsed.scheme != "https" or parsed.hostname not in (
                "doubao.com", "www.doubao.com"
            ):
                raise HTTPException(422, "任务网页不是受信任的豆包地址")
            if not CHAT_PAGE_URL.search(url):
                raise HTTPException(
                    409,
                    "任务没有绑定具体会话网页；为避免关闭同账号其他任务网页，系统未执行关闭",
                )

        ctx = existing_browser(account_id) if existing_browser is not None else None
        if ctx is None:
            return {"ok": True, "job_id": jobid, "closed": False, "already_closed": True}
        key = url.split("?")[0]
        for existing in list(getattr(ctx, "pages", []) or []):
            try:
                if (existing.url or "").split("?")[0] != key:
                    continue
                if existing.is_closed():
                    continue
                await existing.close()
                if not existing.is_closed():
                    raise HTTPException(500, "浏览器未能关闭任务网页，请稍后重试")
                return {"ok": True, "job_id": jobid, "closed": True, "already_closed": False}
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(500, f"关闭任务网页失败：{str(exc)[:300]}") from exc
        return {"ok": True, "job_id": jobid, "closed": False, "already_closed": True}

    @app.post("/api/v1/videos/generations/{jobid}/send", tags=["Doubao Web Video"])
    async def send_prepared_job(jobid: str, confirm_send: bool = False,
                                x_api_key: Optional[str] = Header(None)):
        """Click send for a task prepared through the manual workflow."""
        auth(x_api_key); ensure_schema()
        if not confirm_send:
            raise HTTPException(422, "必须明确确认发送已准备的任务")
        item = detail(jobid)
        task = pending.get(jobid)
        if task is not None and not task.done():
            raise HTTPException(409, "任务正在执行，请稍后再发送")
        page = prepared_pages.get(jobid)
        if page is None or page.is_closed():
            prepared_pages.pop(jobid, None)
            raise HTTPException(409, "已准备的任务页面已关闭，请重新准备豆包网页")
        account_id = item.get("account_id")
        if not account_id:
            raise HTTPException(409, "任务所属账号已删除")
        if bool(account_value(get_account(account_id), "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        recovery_recorded = False
        try:
            async with locks.setdefault(account_id, asyncio.Lock()):
                async with browser_slot(jobid):
                    await page.bring_to_front()
                    await dismiss_blocking_dialogs(page)
                    before = await read_state(page)
                    composer_before = await read_composer_state(page)
                    expected = normalize_prompt_newlines(translate_mention_markers(item.get("prompt") or ""))
                    if not prompts_equivalent(composer_before["prompt"], expected):
                        raise HTTPException(409, "任务提示词不在输入框中，请重新准备豆包网页")
                    submit = await first_visible(page, SUBMIT_SELECTORS)
                    if submit is None or not await submit.is_enabled():
                        raise HTTPException(409, "豆包发送按钮不可用，请打开任务页面人工检查")
                    await submit.click()
                    after = before
                    sent = False
                    for _ in range(20):
                        await page.wait_for_timeout(250)
                        after = await read_state(page)
                        composer_after = await read_composer_state(page)
                        prompt_cleared = bool(composer_before["prompt"]) and not composer_after["prompt"]
                        attachments_consumed = (composer_before["attachmentCount"] == 0 or
                                                composer_after["attachmentCount"] < composer_before["attachmentCount"])
                        assistant_replied = int(after.get("messageCount") or 0) > int(before.get("messageCount") or 0)
                        sent = (after.get("url") != before.get("url") or after.get("running") or after.get("done") or
                                after.get("awaitingConfirm") or after.get("generationStarted") or assistant_replied or
                                (prompt_cleared and attachments_consumed))
                        if sent:
                            break
                    if not sent:
                        emit(jobid, "needs_review", "点击发送后无法证明任务已发出，请人工检查页面",
                             code="SUBMISSION_UNCONFIRMED", url=after.get("url") or "")
                        await notify_recovery(
                            jobid,
                            "SubmissionUnconfirmed",
                            "SUBMISSION_UNCONFIRMED",
                            "点击后无法证明提交，禁止自动重复发送",
                            account_id=account_id,
                            page=page,
                        )
                        recovery_recorded = True
                        raise HTTPException(409, "点击后无法确认是否发送成功；任务已转为需人工处理，禁止重复发送")
                    live = await persisted_url(page, after.get("url") or "")
                    emit(jobid, "confirming", "已发送任务提示词，等待发送确认语", url=live)
                    return detail(jobid)
        except Exception as exc:
            if not recovery_recorded:
                await record_recovery_failure(
                    jobid,
                    exc,
                    account_id=account_id,
                    page=page,
                )
            raise

    @app.post("/api/v1/videos/generations/{jobid}/send-confirmation", tags=["Doubao Web Video"])
    async def send_confirmation(jobid: str, payload: Optional[ConfirmationSendRequest] = None,
                                confirm_send: bool = False,
                                x_api_key: Optional[str] = Header(None)):
        """Send a selected confirmation preset on the job's saved conversation page."""
        auth(x_api_key); ensure_schema()
        if not confirm_send:
            raise HTTPException(422, "必须明确确认发送确认消息（会消耗视频额度）")
        item = detail(jobid)
        preset_id = payload.preset_id if payload else "preset_default"
        with db() as c:
            preset = c.execute("SELECT id,name,text FROM confirmation_presets WHERE id=?",
                               (preset_id,)).fetchone()
        if not preset:
            raise HTTPException(404, "确认语预设不存在")
        confirmation_text = preset["text"]
        account_id = item.get("account_id")
        if not account_id:
            raise HTTPException(409, "任务所属账号已删除")
        if item["status"] not in (MANUAL_WAIT, "confirming", "needs_review"):
            raise HTTPException(409, "当前任务状态不允许补发确认语，禁止重复确认或重复生成")
        if bool(account_value(get_account(account_id), "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        # 人工发送任务常停留在还没有真实会话 URL 的标签页上；有活动准备页就直接复用。
        prepared = prepared_pages.get(jobid)
        reusable = prepared is not None and not prepared.is_closed()
        if prepared is not None and not reusable:
            prepared_pages.pop(jobid, None)
        url = (item["web"].get("web_page_url") or "").strip()
        if not reusable:
            if not url or not CHAT_PAGE_URL.search(url):
                raise HTTPException(409, "该任务没有记录具体会话页，无法补发确认")
            p = urlparse(url)
            is_test_page = (os.getenv("DOUBAO_TEST_MODE") == "1" and p.scheme == "http"
                            and p.hostname in ("127.0.0.1", "localhost"))
            if not is_test_page and (p.scheme != "https" or p.hostname not in ("doubao.com", "www.doubao.com")):
                raise HTTPException(422, "任务网页不是受信任的豆包地址")
        with db() as c:
            w = c.execute("SELECT duration, quota_day, quota_state FROM video_web_jobs WHERE job_id=?",
                          (jobid,)).fetchone()
            needs_new_attempt = item["status"] == "needs_review"
            needs_reservation = needs_new_attempt or (
                w is not None and not quota_held_today(w)
            )
            if w and needs_reservation:
                dur = int(w["duration"] or 0)
                remaining = quota_summary(c, account_id)["remaining_seconds"]
                if dur > remaining and not local_ignore_quota_check():
                    raise HTTPException(409, f"账号当日剩余额度不足（需 {dur} 秒，剩余 {remaining} 秒）")
        page = None
        created_page = False
        try:
            ctx = await browser(account_id)
            if reusable:
                page = prepared
            key = url.split("?")[0] if url else ""
            if page is None:
                for existing in getattr(ctx, "pages", []):
                    try:
                        if key and (existing.url or "").split("?")[0] == key:
                            page = existing
                            break
                    except Exception:
                        continue
            if page is None:
                page = await ctx.new_page()
                created_page = True
            if created_page:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.bring_to_front()
            await page.wait_for_timeout(1500)
            state = await read_state(page)
            text = state.get("text") or ""
            if re.search(r"登录后才能|请先登录|扫码登录", text):
                raise HTTPException(409, "需要先在该账号浏览器登录豆包")
            if re.search(r"人机验证|安全验证|验证码|captcha", text, re.I):
                raise HTTPException(409, "页面要求人工验证，未发送")
            if re.search(r"额度不足|次数已用完|已达上限|请开通会员", text):
                raise HTTPException(409, "账号额度不足，未发送")
            if await dismiss_blocking_dialogs(page):
                state = await read_state(page)
            await send_composer_text(page, confirmation_text)
            how = f"composer:{confirmation_text}"
            verdict, _ = await wait_confirm_send(
                page, state, int(state.get("videoCards") or 0),
                float(os.getenv("DOUBAO_CONFIRM_SEND_GRACE", "10")))
            if verdict == "not_sent":
                raise HTTPException(409, "确认语没有发出去，请在豆包页面核对")
            live = (await read_state(page)).get("url") or url
            with db() as c:
                w = c.execute(
                    "SELECT duration, quota_day, quota_state "
                    "FROM video_web_jobs WHERE job_id=?",
                    (jobid,),
                ).fetchone()
                dur = int(w["duration"] or 0) if w else 0
                day = local_day()
                state = (w["quota_state"] or "") if w else ""
                if needs_new_attempt:
                    if state == "unknown":
                        settle_quota(c, jobid, "downloaded", prior_status=item["status"])
                    elif state in ("", "reserved"):
                        settle_quota(
                            c,
                            jobid,
                            "needs_review",
                            prior_status=item["status"],
                            error_code=item["web"].get("error_code") or "",
                        )
                        w = c.execute(
                            "SELECT duration, quota_day, quota_state "
                            "FROM video_web_jobs WHERE job_id=?",
                            (jobid,),
                        ).fetchone()
                        if (w["quota_state"] or "") == "unknown":
                            settle_quota(
                                c, jobid, "downloaded", prior_status=item["status"]
                            )
                w = c.execute(
                    "SELECT duration, quota_day, quota_state "
                    "FROM video_web_jobs WHERE job_id=?",
                    (jobid,),
                ).fetchone()
                state = (w["quota_state"] or "") if w else ""
                next_status = "generating" if item["status"] in (MANUAL_WAIT, "confirming", "needs_review") else item["status"]
                now = ts()
                c.execute("UPDATE jobs SET status=?, updated_at=?, error_message='' WHERE id=?",
                          (next_status, now, jobid))
                c.execute(
                    "UPDATE video_web_jobs SET phase=?,phase_started_at=?,"
                    "last_heartbeat_at=? WHERE job_id=?",
                    (next_status, now, now, jobid),
                )
                if live and CHAT_PAGE_URL.search(live):
                    session_url, session_id = session_binding(live)
                    c.execute(
                        "UPDATE video_web_jobs SET web_page_url=?,session_url=?,session_id=? "
                        "WHERE job_id=?",
                        (live, session_url, session_id, jobid),
                    )
                c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                          (jobid, next_status, f"人工发送确认语预设“{preset['name']}”：{how}", ts()))
                if w and (needs_new_attempt or not quota_held_today(w)):
                    if (state in ("", "reserved") and w["quota_day"]
                            and w["quota_day"] != day):
                        c.execute(
                            "UPDATE account_quota SET reserved_seconds=MAX(0,reserved_seconds-?) "
                            "WHERE account_id=? AND day=?",
                            (dur, account_id, w["quota_day"]),
                        )
                    c.execute("UPDATE video_web_jobs SET quota_day=?, quota_state='reserved', error_code='' WHERE job_id=?",
                              (day, jobid))
                    reserve_quota(c, account_id, dur, day)
                else:
                    c.execute("UPDATE video_web_jobs SET error_code='' WHERE job_id=?", (jobid,))
            return {"ok": True, "job_id": jobid, "preset_id": preset["id"],
                    "preset_name": preset["name"], "how": how,
                    "message": "确认语已发送"}
        except Exception as exc:
            await record_recovery_failure(
                jobid,
                exc,
                account_id=account_id,
                page=page,
            )
            if created_page and page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            raise

    def claim_recall(jobid: str, preserve_downloaded: bool = False) -> str:
        """Atomically claim a persisted recall lease; stale leases may be reclaimed.

        A forced re-download of an archived task is an artifact maintenance
        operation: it must not move the task out of the archived bucket.
        """
        token = uuid.uuid4().hex
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(timespec="seconds")
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            claimed = c.execute(
                """UPDATE video_web_jobs
                   SET recall_state='recalling', recall_token=?, recall_started_at=?
                   WHERE job_id=? AND (recall_state!='recalling' OR recall_started_at='' OR recall_started_at<?)""",
                (token, ts(), jobid, cutoff))
            if not claimed.rowcount:
                raise HTTPException(409, "该任务已有召回操作正在执行")
            now = ts()
            if preserve_downloaded:
                c.execute(
                    "UPDATE video_web_jobs SET error_code='',error_class='' WHERE job_id=?",
                    (jobid,),
                )
                c.execute(
                    "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                    "VALUES(?,?,?,?)",
                    (jobid, "downloaded",
                     "已取得数据库召回锁，重新下载并校验原归档结果（任务保持已归档）", now),
                )
            else:
                c.execute(
                    "UPDATE jobs SET status='generating', error_message='', updated_at=? "
                    "WHERE id=?",
                    (now, jobid),
                )
                c.execute(
                    "UPDATE video_web_jobs SET phase='generating',phase_started_at=?,"
                    "last_heartbeat_at=?,error_code='',error_class='' WHERE job_id=?",
                    (now, now, jobid),
                )
                c.execute(
                    "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                    "VALUES(?,?,?,?)",
                    (jobid, "generating",
                     "已取得数据库召回锁，使用原会话等待结果（不会重新提交）", now),
                )
        return token

    def release_recall(jobid: str, token: str) -> None:
        with db() as c:
            c.execute("""UPDATE video_web_jobs SET recall_state='', recall_token='', recall_started_at=''
                         WHERE job_id=? AND recall_token=?""", (jobid, token))

    async def recall_worker(jobid: str, token: str, preserve_downloaded: bool = False):
        page = None
        reused = False
        download_succeeded = False
        account_id = ""
        account_claimed = False
        recovery_error = None
        recovery_snapshot = None
        try:
            item = detail(jobid)
            account_id = item.get("account_id")
            await acquire_account_slot(jobid, account_id)
            account_claimed = True
            url = (item["web"].get("web_page_url") or "").strip()
            prepared = prepared_pages.get(jobid)
            is_manual = ((item["web"].get("submit_mode") or "auto") == "manual"
                         or (prepared is not None and not prepared.is_closed()))
            async with locks.setdefault(account_id, asyncio.Lock()):
                async with browser_slot(jobid):
                    ctx = await browser(account_id)
                    # 人工发送任务的页面可能还停在还没发消息的临时会话上；优先复用它，
                    # 这样用户在网页发送后 URL 已转成真实 /chat/<id> 时能一并记录下来。
                    if prepared is not None and not prepared.is_closed():
                        page = prepared
                        reused = True
                        await bind_task_page(jobid, page)
                        try:
                            await page.bring_to_front()
                        except Exception:
                            pass
                        await page.wait_for_timeout(800)
                        try:
                            live = (await read_state(page)).get("url") or ""
                        except Exception:
                            live = ""
                        p = urlparse(live)
                        trusted = (p.hostname in ("doubao.com", "www.doubao.com")
                                   or (os.getenv("DOUBAO_TEST_MODE") == "1"
                                       and p.hostname in ("127.0.0.1", "localhost")))
                        if live and trusted and CHAT_PAGE_URL.search(live):
                            url = live
                            session_url, session_id = session_binding(url)
                            with db() as c:
                                c.execute(
                                    "UPDATE video_web_jobs SET web_page_url=?,session_url=?,"
                                    "session_id=? WHERE job_id=?",
                                    (url, session_url, session_id, jobid),
                                )
                    else:
                        prepared_pages.pop(jobid, None)
                        page = await ctx.new_page()
                        await bind_task_page(jobid, page)
                        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        await page.wait_for_timeout(1500)
                    opened_at = asyncio.get_running_loop().time()
                    load_grace = float(os.getenv("DOUBAO_RECALL_LOAD_GRACE", "20"))
                    deadline = (asyncio.get_running_loop().time()
                                + float(os.getenv("DOUBAO_RECALL_TIMEOUT", "900")))
                    while asyncio.get_running_loop().time() < deadline:
                        with db() as c:
                            c.execute(
                                "UPDATE video_web_jobs SET last_heartbeat_at=? "
                                "WHERE job_id=?",
                                (ts(), jobid),
                            )
                        state = await read_state(page)
                        text = state.get("text") or ""
                        if re.search(r"登录后才能|请先登录|扫码登录", text):
                            raise ReviewNeeded("LOGIN_REQUIRED", "召回时发现账号需要重新登录")
                        if re.search(r"人机验证|安全验证|验证码|captcha", text, re.I):
                            raise ReviewNeeded("VERIFICATION_REQUIRED", "召回页面要求人工验证")
                        if await dismiss_blocking_dialogs(page):
                            state = await read_state(page)
                        if state["failed"] and preserve_downloaded:
                            raise ReviewNeeded("GENERATION_FAILED", "豆包页面明确显示生成失败")
                        if state["failed"]:
                            emit(jobid, "failed", "豆包页面明确显示生成失败", code="GENERATION_FAILED")
                            return
                        if state["awaitingConfirm"]:
                            # 刚打开持久化会话时消息列表尚未水合，最后一条仍是历史的
                            # “确认后开始生成”文案；不能在加载宽限期内据此判死。
                            if asyncio.get_running_loop().time() - opened_at < load_grace:
                                await adaptive_poll(page, opened_at)
                                continue
                            raise ReviewNeeded("CONFIRM_REQUIRED", "页面仍等待确认；召回不会自动补发确认消息")

                        card_count = int(state.get("videoCards") or 0)
                        raw_index = item["web"].get("result_card_index")
                        card_index = int(raw_index) if raw_index is not None else None
                        if card_index is None and card_count == 1:
                            card_index = 0
                            with db() as c:
                                c.execute("UPDATE video_web_jobs SET result_card_index=0 WHERE job_id=?", (jobid,))
                        if card_index is None and card_count > 1:
                            raise ReviewNeeded("RESULT_BINDING_MISSING",
                                               "旧任务缺少提交前卡片序号，且会话有多个视频，禁止猜测最后一个结果")
                        if card_index is not None and card_count > card_index and not state["runningLast"]:
                            target = (downloads / "library" / safe(item["project_id"]) / safe(item["episode_id"])
                                      / safe(item["shot_id"] or jobid) / (jobid + ".mp4"))
                            result = await download_result_video(
                                page, set(state.get("videoUrls") or []), target,
                                card_index=card_index, allow_known=True)
                            path = result["output"]
                            await finish_downloaded(
                                jobid,
                                "已关联人工发送后生成的视频并归档（无水印成片）" if is_manual
                                else "按任务绑定的视频卡片召回并归档（无水印成片）",
                                {"video_url": result["video_url"],
                                 "web_page_url": state.get("url") or url,
                                 "output": path},
                                result_source="manual_web_link" if is_manual else "bound_web_recall",
                                close_account_after=False)
                            download_succeeded = True
                            return
                        if not state["runningLast"] and card_count == 0:
                            # 人工发送时操作者可能刚点过发送：最后一条助手消息会先说
                            # “本次使用…生成”，此时应继续等卡片，而不是立即判失败。
                            if is_manual and state.get("generationStarted"):
                                await adaptive_poll(page, opened_at)
                                continue
                            # 刚打开会话时消息列表还在水合，卡片尚未挂载不能当成“没有结果”。
                            if asyncio.get_running_loop().time() - opened_at < load_grace:
                                await adaptive_poll(page, opened_at)
                                continue
                            hint = "；请先在豆包网页点击发送并等待生成" if is_manual else ""
                            raise ReviewNeeded("RESULT_NOT_READY", "原会话尚未出现与任务绑定的视频结果" + hint)
                        await adaptive_poll(page, opened_at)
                    raise ReviewNeeded("WAIT_TIMEOUT", "召回等待超时；保留额度未知状态，可稍后再次召回")
        except ReviewNeeded as e:
            if preserve_downloaded:
                emit(jobid, "downloaded", "重新下载失败，已保留原归档视频：" + e.message, code=e.code)
            else:
                emit(jobid, "needs_review", e.message, code=e.code)
            recovery_error = {
                "error_type": type(e).__name__,
                "error_code": e.code,
                "error_message": e.message,
            }
        except Exception as e:
            if preserve_downloaded:
                emit(jobid, "downloaded", "重新下载失败，已保留原归档视频：" + str(e)[:300], code="RECALL_EXCEPTION")
            else:
                emit(jobid, "needs_review", str(e)[:350], code="RECALL_EXCEPTION")
            recovery_error = {
                "error_type": type(e).__name__,
                "error_code": "RECALL_EXCEPTION",
                "error_message": str(e),
            }
        finally:
            if recovery_error is not None:
                recovery_snapshot = await capture_page_snapshot(page)
            # 下载失败时保留复用页面供人工检查；成功后的关闭策略在释放任务占用后统一判断。
            if page is not None and not download_succeeded and not reused:
                try:
                    await page.close()
                except Exception:
                    pass
            release_recall(jobid, token)
            pending.pop(jobid, None)
            if account_claimed:
                await release_account_slot_if_finished(jobid, account_id)
            if download_succeeded:
                await cleanup_downloaded_browser(jobid, account_id, page=page)
            if recovery_error is not None:
                await notify_recovery(
                    jobid,
                    recovery_error["error_type"],
                    recovery_error["error_code"],
                    recovery_error["error_message"],
                    account_id=account_id or "",
                    page_snapshot=recovery_snapshot,
                )
            if not download_succeeded:
                schedule_idle_close(account_id, jobid)

    async def begin_recall(jobid: str, wait: bool = False, force_redownload: bool = False):
        item = detail(jobid)
        preserve_downloaded = item["status"] == "downloaded"
        if preserve_downloaded and not force_redownload:
            return item
        allowed = ("downloaded", "needs_review", "failed", MANUAL_WAIT, "confirming", "generating")
        if item["status"] not in allowed:
            raise HTTPException(409, "仅需复查、失败或待人工发送的任务可以关联/召回")
        prepared = prepared_pages.get(jobid)
        reusable = prepared is not None and not prepared.is_closed()
        if prepared is not None and not reusable:
            prepared_pages.pop(jobid, None)
        url = (item["web"].get("web_page_url") or "").strip()
        if not reusable:
            if not url or not CHAT_PAGE_URL.search(url):
                if item["status"] == MANUAL_WAIT:
                    raise HTTPException(409, "准备页面已关闭，且未记录具体会话；请在豆包历史记录中人工核对，"
                                             "并用“复查归档”导入视频")
                raise HTTPException(409, "该任务没有记录具体会话页，无法召回")
            p = urlparse(url)
            is_test_page = (os.getenv("DOUBAO_TEST_MODE") == "1" and p.scheme == "http"
                            and p.hostname in ("127.0.0.1", "localhost"))
            if not is_test_page and (p.scheme != "https" or p.hostname not in ("doubao.com", "www.doubao.com")):
                raise HTTPException(422, "任务网页不是受信任的豆包地址")
        account_id = item.get("account_id")
        if not account_id:
            raise HTTPException(409, "任务所属账号已删除")
        task = pending.get(jobid)
        if task is not None and not task.done():
            # A submitted generation may remain "generating" while its automatic
            # result watcher is stalled. An explicit download may replace that
            # watcher only after a card has been bound; recall itself never submits.
            if (item["status"] != "generating"
                    or item["web"].get("result_card_index") is None):
                return item
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            item = detail(jobid)
        token = claim_recall(jobid, preserve_downloaded=preserve_downloaded)
        if wait:
            await recall_worker(jobid, token, preserve_downloaded=preserve_downloaded)
            return detail(jobid)
        pending[jobid] = asyncio.create_task(
            recall_worker(jobid, token, preserve_downloaded=preserve_downloaded))
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/recall", tags=["Doubao Web Video"])
    async def recall(jobid: str, force_redownload: bool = False,
                     x_api_key: Optional[str] = Header(None)):
        """Idempotently resume/download an existing result; never submits generation."""
        auth(x_api_key); ensure_schema()
        return await begin_recall(jobid, force_redownload=force_redownload)

    @app.post("/api/v1/videos/generations/{jobid}/download-result", tags=["Doubao Web Video"])
    async def download_result(jobid: str, confirm_review: bool = False,
                              x_api_key: Optional[str] = Header(None)):
        """Backward-compatible alias for the unified recall endpoint."""
        auth(x_api_key); ensure_schema()
        if not confirm_review:
            raise HTTPException(422, "必须明确确认已在豆包网页核对视频与分镜归属")
        return await begin_recall(jobid, wait=True)

    @app.post("/api/v1/videos/generations/{jobid}/push-result", tags=["Doubao Web Video"])
    async def push_result(jobid: str):
        """Deliver an archived video through the outbox, independently of generation."""
        ensure_schema()
        row = get_job(jobid)
        if not row["video_path"]:
            raise HTTPException(409, "请先下载并归档视频，再推送服务器")
        with db() as c:
            push = c.execute(
                "SELECT result_push_url,result_push_token,duration,error_code,"
                "delivery_attempts FROM video_web_jobs WHERE job_id=?",
                (jobid,),
            ).fetchone()
        if not push or not push["result_push_url"] or not push["result_push_token"]:
            raise HTTPException(409, "该任务不是由支持回推的 H3 服务提交，无法推送")

        parsed = urlparse(push["result_push_url"])
        if parsed.scheme not in ("http", "https") or parsed.hostname not in (
            "127.0.0.1", "localhost", "::1"
        ):
            raise HTTPException(422, "H3 回推地址必须通过本机 SSH 转发端口访问")
        video_path = resolved_path(row["video_path"], base)
        if not video_path.is_file():
            raise HTTPException(404, "本地归档视频不存在，请重新下载")

        attempt = int(push["delivery_attempts"] or 0) + 1
        with db() as c:
            c.execute(
                "UPDATE video_web_jobs SET delivery_state='pending',"
                "delivery_attempts=?,delivery_error='',delivery_next_retry_at='' "
                "WHERE job_id=?",
                (attempt, jobid),
            )
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(600.0, connect=15.0)
            ) as client:
                with video_path.open("rb") as video:
                    response = await client.post(
                        push["result_push_url"],
                        headers={"X-Doubao-Push-Token": push["result_push_token"]},
                        data={"duration": str(push["duration"] or "")},
                        files={"file": (video_path.name, video, "video/mp4")},
                    )
            if response.is_error:
                try:
                    payload = response.json()
                    detail_msg = (
                        payload.get("message")
                        or payload.get("detail")
                        or response.text
                    )
                except Exception:
                    detail_msg = response.text
                raise RuntimeError(
                    f"H3 返回 HTTP {response.status_code}：{str(detail_msg)[:300]}"
                )
        except Exception as exc:
            message = str(exc)[:500]
            retry_after = min(900, 30 * (2 ** min(attempt - 1, 5)))
            retry_at = (
                datetime.now(timezone.utc) + timedelta(seconds=retry_after)
            ).isoformat(timespec="seconds")
            with db() as c:
                c.execute(
                    "UPDATE video_web_jobs SET delivery_state='failed',"
                    "delivery_error=?,delivery_next_retry_at=?,"
                    "result_push_error=?,error_code='',error_class='' "
                    "WHERE job_id=?",
                    (message, retry_at, message, jobid),
                )
                current = c.execute(
                    "SELECT status FROM jobs WHERE id=?", (jobid,)
                ).fetchone()
                c.execute(
                    "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                    "VALUES(?,?,?,?)",
                    (jobid, current["status"] if current else "downloaded",
                     f"服务器推送失败（第 {attempt} 次）：{message}", ts()),
                )
            await notify_recovery(
                jobid,
                type(exc).__name__,
                "RESULT_PUSH_FAILED",
                message,
                account_id=row["account_id"] or "",
            )
            raise HTTPException(502, f"推送 H3 失败：{message}") from exc

        pushed_at = ts()
        with db() as c:
            recovered = (
                row["status"] == "needs_review"
                and push["error_code"] == "RESULT_PUSH_FAILED"
            )
            c.execute(
                "UPDATE video_web_jobs SET result_pushed_at=?,result_push_error='',"
                "delivery_state='delivered',delivery_error='',"
                "delivery_next_retry_at='',error_code='',error_class='' "
                "WHERE job_id=?",
                (pushed_at, jobid),
            )
            if recovered:
                c.execute(
                    "UPDATE jobs SET status='downloaded',error_message='',"
                    "updated_at=? WHERE id=?",
                    (pushed_at, jobid),
                )
                c.execute(
                    "UPDATE video_web_jobs SET phase='downloaded',"
                    "phase_started_at=?,last_heartbeat_at=? WHERE job_id=?",
                    (pushed_at, pushed_at, jobid),
                )
            c.execute(
                "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                "VALUES(?,?,?,?)",
                (jobid, "downloaded" if recovered else row["status"],
                 f"已将归档视频推送到 H3（第 {attempt} 次）", pushed_at),
            )
        if recovered:
            # A push-only recovery turns a human-review hand-off into an archived
            # task. Drop any stale slot reservation and release the account lock.
            await release_processing_job(jobid)
            if row["account_id"]:
                await release_account_slot_if_finished(jobid, row["account_id"])
        return {
            "ok": True,
            "job_id": jobid,
            "pushed_at": pushed_at,
            "delivery_state": "delivered",
            "delivery_attempts": attempt,
        }

    def recovery_bound_url(item: Mapping) -> str:
        """Return only a persisted, recognizable Doubao conversation URL."""
        url = (
            item.get("web", {}).get("session_url")
            or item.get("web", {}).get("web_page_url")
            or ""
        ).strip()
        if not url:
            return ""
        parsed = urlparse(url)
        trusted = (
            parsed.scheme == "https"
            and parsed.hostname in ("doubao.com", "www.doubao.com")
        ) or (
            os.getenv("DOUBAO_TEST_MODE") == "1"
            and parsed.scheme == "http"
            and parsed.hostname in ("127.0.0.1", "localhost")
        )
        if not trusted or not CHAT_PAGE_URL.search(url):
            return ""
        return url

    async def recovery_task_page(
        jobid: str, *, requested_url: str = ""
    ) -> tuple[object, dict, dict, str]:
        """Reuse/open only the task's exact persisted conversation tab."""
        item = detail(jobid)
        account_id = item.get("account_id") or ""
        if not account_id:
            raise HTTPException(409, "任务所属账号已删除，无法定位任务页")
        bound_url = recovery_bound_url(item)
        if not bound_url:
            raise HTTPException(
                409,
                "任务未持久化可识别的豆包会话页；禁止猜测历史标签或其他任务",
            )
        if requested_url and requested_url.strip().split("?")[0] != (
            bound_url.split("?")[0]
        ):
            raise HTTPException(
                409, "请求的页面不是该任务持久化绑定的会话页，已拒绝切换"
            )

        ctx = await browser(account_id)
        page = prepared_pages.get(jobid)
        if page is not None and page.is_closed():
            prepared_pages.pop(jobid, None)
            page = None
        if page is None:
            key = bound_url.split("?")[0]
            page = next(
                (
                    candidate
                    for candidate in list(getattr(ctx, "pages", []) or [])
                    if not candidate.is_closed()
                    and (getattr(candidate, "url", "") or "").split("?")[0]
                    == key
                ),
                None,
            )
        if page is None:
            page = await ctx.new_page()
            await page.goto(
                bound_url, wait_until="domcontentloaded", timeout=60000
            )
            await page.wait_for_timeout(800)

        page_title = concise_job_title(
            item.get("project_id", ""),
            item.get("episode_id", ""),
            item.get("shot_id", ""),
            fallback=item.get("task_name") or jobid,
        )
        await set_job_page_title(page, page_title)
        await page.bring_to_front()
        try:
            state = await read_state(page)
        except Exception:
            state = {}
        live_url = (
            state.get("url") or getattr(page, "url", "") or bound_url
        ).strip()
        persisted_url = recovery_bound_url(
            {"web": {"session_url": live_url}}
        )
        if persisted_url:
            session_url, session_id = session_binding(persisted_url)
            with db() as c:
                c.execute(
                    "UPDATE video_web_jobs SET web_page_url=?,session_url=?,"
                    "session_id=? WHERE job_id=?",
                    (session_url, session_url, session_id, jobid),
                )
            bound_url = session_url
        return page, detail(jobid), state, bound_url

    async def recovery_restart_browser(jobid: str, _args=None) -> dict:
        item = detail(jobid)
        account_id = item.get("account_id") or ""
        if not account_id:
            return {"success": False, "verified": False, "detail": "任务所属账号已删除"}
        if close_browser is None:
            return {"success": False, "verified": False, "detail": "未配置浏览器关闭功能"}
        async with account_condition:
            owners = account_owners.get(account_id, set())
            if owners - {jobid}:
                return {
                    "success": False,
                    "verified": False,
                    "detail": "该账号仍有其他任务在执行，未重启浏览器",
                }
        await close_browser(account_id)
        ctx = await browser(account_id)
        pages = list(getattr(ctx, "pages", []) or [])
        return {
            "success": bool(pages),
            "verified": bool(pages),
            "detail": "已通过现有账号浏览器功能重新启动" if pages else "浏览器已启动但没有可用页面",
        }

    async def recovery_refresh_page(jobid: str, _args=None) -> dict:
        item = detail(jobid)
        account_id = item.get("account_id") or ""
        if not account_id:
            return {"success": False, "verified": False, "detail": "任务所属账号已删除"}
        ctx = await browser(account_id)
        page = prepared_pages.get(jobid)
        if page is None or page.is_closed():
            expected = (item["web"].get("web_page_url") or "").split("?")[0]
            candidates = list(getattr(ctx, "pages", []) or [])
            page = next(
                (
                    candidate
                    for candidate in candidates
                    if expected
                    and (getattr(candidate, "url", "") or "").split("?")[0] == expected
                ),
                None,
            )
        if page is None or page.is_closed():
            return {"success": False, "verified": False, "detail": "没有可刷新的原有任务页面"}
        await page.reload(wait_until="domcontentloaded", timeout=60000)
        return {
            "success": True,
            "verified": not page.is_closed(),
            "detail": "已刷新原有任务页面",
            "status": (getattr(page, "url", "") or "").split("?")[0],
        }

    async def recovery_switch_page(jobid: str, args=None) -> dict:
        page, item, state, url = await recovery_task_page(
            jobid, requested_url=str((args or {}).get("url") or "")
        )
        return {
            "success": True,
            "verified": not page.is_closed(),
            "recovery_complete": False,
            "status": item.get("status") or "",
            "url": state.get("url") or url,
            "video_card_count": int(state.get("videoCards") or 0),
            "detail": "已切换并置于前台该任务唯一绑定的豆包会话页",
        }

    async def recovery_inspect_dom(jobid: str, _args=None) -> dict:
        page, item, _state, _url = await recovery_task_page(jobid)
        snapshot = await capture_page_snapshot(page)
        available = bool(snapshot.get("available"))
        return {
            "success": available,
            "verified": available,
            "recovery_complete": False,
            "status": item.get("status") or "",
            "observation": snapshot,
            "detail": (
                "已读取最新任务页 DOM、控件、上传标记和网络错误"
                if available
                else f"DOM 读取失败：{snapshot.get('reason') or 'unknown'}"
            ),
        }

    async def recovery_dismiss_popup(jobid: str, _args=None) -> dict:
        page, item, state, _url = await recovery_task_page(jobid)
        closed = await dismiss_blocking_dialogs(page)
        return {
            "success": True,
            "verified": not page.is_closed(),
            "recovery_complete": False,
            "status": item.get("status") or "",
            "closed_popups": int(closed or 0),
            "video_card_count": int(state.get("videoCards") or 0),
            "detail": f"已关闭 {int(closed or 0)} 个带明确关闭控件的干扰弹窗",
        }

    async def recovery_locate_task_page(jobid: str, _args=None) -> dict:
        page, item, state, url = await recovery_task_page(jobid)
        return {
            "success": True,
            "verified": not page.is_closed(),
            "recovery_complete": False,
            "status": item.get("status") or "",
            "session_url": url,
            "session_id": item["web"].get("session_id") or "",
            "video_card_count": int(state.get("videoCards") or 0),
            "detail": "已恢复任务与当前豆包会话标签页的绑定",
        }

    async def recovery_verify_task_status(jobid: str, _args=None) -> dict:
        item = detail(jobid)
        page_error = ""
        state = {}
        try:
            _page, item, state, _url = await recovery_task_page(jobid)
        except Exception as exc:
            page_error = str(getattr(exc, "detail", "") or exc)[:300]
        status = item.get("status") or ""
        downloaded = status == "downloaded"
        return {
            "success": True,
            "verified": bool(status),
            "recovery_complete": downloaded,
            "status": status,
            "phase": item.get("web", {}).get("phase") or "",
            "video_card_count": int(state.get("videoCards") or 0),
            "running": bool(state.get("running") or state.get("runningLast")),
            "page_error": page_error,
            "detail": (
                "已核对任务已归档，原业务流程恢复"
                if downloaded
                else f"任务当前状态为 {status or 'unknown'}"
                + (f"；页面读取失败：{page_error}" if page_error else "")
            ),
        }

    async def recovery_check_assets(jobid: str, _args=None) -> dict:
        item = detail(jobid)
        assets = item.get("web", {}).get("assets") or {}
        asset_ids = [
            value
            for value in (
                [assets.get("first_frame_asset_id")]
                + [assets.get("last_frame_asset_id")]
                + list(assets.get("reference_asset_ids") or [])
            )
            if value
        ]
        checked = []
        with db() as c:
            for asset_id in asset_ids:
                row = c.execute(
                    "SELECT stored_path,sha256 FROM video_web_assets WHERE id=?",
                    (asset_id,),
                ).fetchone()
                exists = False
                if row:
                    try:
                        exists = resolved_path(
                            row["stored_path"], asset_dir
                        ).is_file()
                    except Exception:
                        exists = False
                checked.append(
                    {
                        "asset_id": asset_id,
                        "exists": exists,
                        "sha256_present": bool(row and row["sha256"]),
                    }
                )
        missing = [entry["asset_id"] for entry in checked if not entry["exists"]]
        return {
            "success": not missing,
            "verified": True,
            "recovery_complete": False,
            "status": item.get("status") or "",
            "checked_assets": checked,
            "detail": (
                "全部任务素材均存在"
                if not missing
                else f"缺少 {len(missing)} 个任务素材：{', '.join(missing)}"
            ),
        }

    async def recovery_download_video(jobid: str, _args=None) -> dict:
        return await recovery_recall_existing_result(jobid)

    async def recovery_release_account(jobid: str, _args=None) -> dict:
        item = detail(jobid)
        account_id = item.get("account_id") or ""
        if not account_id:
            return {
                "success": False,
                "verified": False,
                "recovery_complete": False,
                "detail": "任务所属账号已删除，无法释放占用",
            }
        async with account_condition:
            owned_before = jobid in account_owners.get(account_id, set())
        await release_account_slot(jobid, account_id)
        async with account_condition:
            released = jobid not in account_owners.get(account_id, set())
        return {
            "success": released and owned_before,
            "verified": released,
            "recovery_complete": False,
            "status": item.get("status") or "",
            "detail": (
                "已释放该任务的账号使用记录；同账号其他任务不受影响"
                if released and owned_before
                else "该任务没有残留的账号占用"
            ),
        }

    async def recovery_recall_existing_result(jobid: str, _args=None) -> dict:
        before = detail(jobid)
        if before.get("status") == "downloaded":
            return {
                "success": False,
                "verified": False,
                "detail": "已有归档视频，禁止召回覆盖",
                "status": "downloaded",
            }
        await begin_recall(jobid, wait=False)
        task = pending.get(jobid)
        try:
            observe_seconds = max(
                0.0,
                float(os.getenv("DOUBAO_AGENT_RECOVERY_OBSERVE_SECONDS", "20")),
            )
        except ValueError:
            observe_seconds = 20.0
        if task is not None and observe_seconds:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=observe_seconds)
            except asyncio.TimeoutError:
                pass
        current = detail(jobid)
        success = current.get("status") == "downloaded"
        return {
            "success": success,
            "verified": success,
            "accepted": current.get("status") in ACTIVE,
            "status": current.get("status") or "",
            "detail": (
                "原任务召回成功并已归档，未重新生成"
                if success
                else "原任务召回已启动或结束，结果仍需人工核对"
            ),
        }

    async def recovery_retry_h3_push(jobid: str, _args=None) -> dict:
        result = await push_result(jobid)
        return {
            "success": bool(result.get("ok")),
            "verified": result.get("delivery_state") == "delivered",
            "detail": "已通过现有 H3 回推接口重试，未重新生成视频",
            "status": result.get("delivery_state") or "",
        }

    async def recovery_restart_auto(jobid: str, _args=None) -> dict:
        """Agent action: restart the current job in automatic mode (same job id)."""
        before = detail(jobid)
        if before.get("status") == "downloaded":
            return {
                "success": False,
                "verified": False,
                "detail": "任务已下载完成，不能就地重跑（请改用 regenerate 或召回）",
                "status": "downloaded",
            }
        if jobid in pending and not pending[jobid].done():
            return {
                "success": False,
                "verified": False,
                "detail": "任务仍在执行，暂不能重新自动执行",
                "status": before.get("status") or "",
            }
        previous_account_id = before.get("account_id") or ""
        if before.get("status") == "queued":
            release_job_quota_hold(jobid)
        req = await reset_generation(jobid, rematch_account=True)
        req.submit_mode = "auto"
        start_worker(jobid, req, previous_account_id or avoid_account_for(jobid))
        await _await_agent_observation(jobid)
        current = detail(jobid)
        success = current.get("status") in ACTIVE or current.get("status") == "downloaded"
        return {
            "success": bool(success),
            "verified": bool(success),
            "accepted": current.get("status") in ACTIVE,
            "status": current.get("status") or "",
            "detail": (
                "已重新自动执行并进入处理，未产生新任务 ID"
                if success
                else "已请求重新自动执行，但状态未推进，仍需人工核对"
            ),
        }

    async def recovery_regenerate(jobid: str, _args=None) -> dict:
        """Agent action: clone the failed job into a new queued job and start it."""
        original = detail(jobid)
        if original["status"] not in ("needs_review", "failed", "queued"):
            return {
                "success": False,
                "verified": False,
                "detail": f"当前状态 {original['status']} 不适合重新生成",
                "status": original.get("status") or "",
            }
        with db() as c:
            existing = c.execute(
                """SELECT j.id,j.status FROM jobs j JOIN video_web_jobs w ON w.job_id=j.id
                   WHERE w.retry_of_job_id=? AND j.status NOT IN ('needs_review','failed')
                   ORDER BY j.created_at DESC LIMIT 1""",
                (jobid,),
            ).fetchone()
        if existing:
            return {
                "success": True,
                "verified": existing["status"] in ACTIVE or existing["status"] == "downloaded",
                "detail": f"已存在重试任务 {existing['id']}，未重复创建",
                "status": existing["status"],
                "new_job_id": existing["id"],
            }
        req = assemble_req(jobid)
        previous_account_id = req.account_id or ""
        req.account_id = None
        req.idempotency_key = None
        req.auto_start = True
        base_name = (req.task_name or original.get("task_name") or jobid).strip()
        req.task_name = (base_name + " · Agent重试")[:160]
        created = create_generation(
            req, retry_of_job_id=jobid, avoid_account_id=previous_account_id
        )
        await _await_agent_observation(created["id"])
        current = detail(created["id"])
        success = current.get("status") in ACTIVE or current.get("status") == "downloaded"
        return {
            "success": bool(success),
            "verified": bool(success),
            "accepted": current.get("status") in ACTIVE,
            "status": current.get("status") or "",
            "new_job_id": created["id"],
            "detail": (
                f"已创建新任务 {created['id']} 并重新生成，原任务保留"
                if success
                else f"已创建新任务 {created['id']}，但状态未推进，仍需人工核对"
            ),
        }

    async def _await_agent_observation(jobid: str) -> None:
        """短暂等待任务推进，让代理拿到可验证的状态而不是立刻的 queued。"""
        try:
            observe_seconds = max(
                0.0,
                float(os.getenv("DOUBAO_AGENT_RECOVERY_OBSERVE_SECONDS", "20")),
            )
        except ValueError:
            observe_seconds = 20.0
        deadline = asyncio.get_running_loop().time() + observe_seconds
        while observe_seconds and asyncio.get_running_loop().time() < deadline:
            if detail(jobid).get("status") not in ("queued", "ready"):
                return
            await asyncio.sleep(0.2)

    if recovery_service is None:
        recovery_service = ExceptionRecoveryService(
            backend=RecoveryBackend(
                restart_browser=recovery_restart_browser,
                refresh_page=recovery_refresh_page,
                switch_page=recovery_switch_page,
                inspect_dom=recovery_inspect_dom,
                dismiss_popup=recovery_dismiss_popup,
                locate_task_page=recovery_locate_task_page,
                verify_task_status=recovery_verify_task_status,
                check_assets=recovery_check_assets,
                recall_existing_result=recovery_recall_existing_result,
                download_video=recovery_download_video,
                retry_h3_push=recovery_retry_h3_push,
                restart_auto=recovery_restart_auto,
                regenerate=recovery_regenerate,
                release_account=recovery_release_account,
            ),
            context_loader=load_recovery_context,
            log_dir=base / "data" / "agent_recovery",
            enabled_provider=local_claude_recovery_enabled,
        )
    app.state.video_recovery_service = recovery_service

    @app.post(
        "/api/internal/agent-recovery/tool",
        include_in_schema=False,
    )
    async def agent_recovery_tool_bridge(
        payload: dict = Body(...),
        x_doubao_agent_token: Optional[str] = Header(None),
    ):
        """Token-scoped bridge used only by the recovery MCP subprocess."""
        execute_tool = getattr(recovery_service, "execute_tool", None)
        if execute_tool is None:
            raise HTTPException(503, "恢复工具桥未启用")
        try:
            return await execute_tool(
                job_id=str(payload.get("job_id") or ""),
                token=str(x_doubao_agent_token or ""),
                tool=str(payload.get("tool") or ""),
                arguments=payload.get("arguments") or {},
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    recoverable_existing_codes = {
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
        "RESULT_PUSH_FAILED",
        "SETTING_NOT_FOUND",
        "SETTING_UNSUPPORTED",
        "SETTING_UNVERIFIED",
        "MODEL_NOT_FOUND",
        "MODEL_UNVERIFIED",
        "MODEL_TRIGGER_NOT_FOUND",
        "UPLOAD_SLOT_NOT_FOUND",
        "UPLOAD_UNVERIFIED",
        "UPLOAD_COUNT_MISMATCH",
        "EDITOR_NOT_FOUND",
        "PROMPT_UNVERIFIED",
        "SUBMIT_NOT_FOUND",
        "SUBMISSION_UNCONFIRMED",
        "CONFIRM_NOT_SENT",
        "CONFIRM_REQUIRED",
        "GENERATION_FAILED",
        "DURATION_MISMATCH",
        "RESULT_BINDING_MISSING",
        "MANUAL_SEND_INTERRUPTED",
        "INTERRUPTED_REVIEW_REQUIRED",
    }

    async def resume_recoverable_jobs() -> int:
        """Hand pre-existing safe failures to the Agent once after startup."""
        with db() as c:
            rows = c.execute(
                "SELECT j.id,j.account_id,j.error_message,j.video_path,"
                "w.phase,w.error_code,w.web_page_url,w.quota_state "
                "FROM jobs j JOIN video_web_jobs w ON j.id=w.job_id "
                "WHERE j.status='needs_review' "
                "ORDER BY j.updated_at ASC"
            ).fetchall()

        scheduled = 0
        for row in rows:
            code = row["error_code"] or ""
            if code not in recoverable_existing_codes:
                continue
            if row["quota_state"] == "unknown":
                continue
            if code == "RESULT_PUSH_FAILED" and not row["video_path"]:
                continue
            handed_off = await notify_recovery(
                row["id"],
                "RecoverableException",
                code,
                row["error_message"] or row["phase"] or code,
                account_id=row["account_id"] or "",
            )
            scheduled += int(handed_off)
        if scheduled:
            logger.info("Handed %s pre-existing failures to Agent recovery", scheduled)
        return scheduled

    app.state.video_resume_recovery = resume_recoverable_jobs

    def redispatch_queued_jobs() -> int:
        """Re-admit auto jobs that are queued but have no live worker.

        A worker can disappear after a quota/allocation failure, after an account
        wait timeout, or after an unexpected cancel. Without this tick those jobs
        sit in ``queued`` until a human clicks again or the service restarts.
        """
        now = ts()
        with db() as c:
            rows = c.execute(
                "SELECT j.id FROM jobs j JOIN video_web_jobs w ON j.id=w.job_id "
                "WHERE j.status='queued' "
                "AND COALESCE(w.submit_mode,'auto')='auto' "
                "AND COALESCE(w.dispatched_at,'')!='' "
                "AND COALESCE(w.next_retry_at,'') <= ? "
                "ORDER BY j.created_at ASC, j.id ASC LIMIT 50",
                (now,),
            ).fetchall()
        started = 0
        for row in rows:
            jobid = row["id"]
            task = pending.get(jobid)
            if task is not None and not task.done():
                continue
            try:
                req = assemble_req(jobid)
                start_worker(jobid, req, avoid_account_for(jobid))
                started += 1
            except HTTPException as exc:
                # Keep the job from being rescanned every tick when the dispatch is
                # not viable yet (quota short, account archived/paused, ...).
                with db() as c:
                    state = c.execute(
                        "SELECT status FROM jobs WHERE id=?", (jobid,)
                    ).fetchone()
                if state is not None and state["status"] == "queued":
                    defer_job(
                        jobid,
                        f"自动重派暂不可执行（{exc.detail}），稍后重试",
                        extra_seconds=240.0,
                    )
                continue
            except Exception:
                logger.exception("Could not redispatch queued job %s", jobid)
        return started

    async def queue_tick_loop() -> None:
        try:
            interval = max(1.0, float(os.getenv("DOUBAO_QUEUE_TICK_SECONDS", "5")))
        except ValueError:
            interval = 5.0
        while True:
            await asyncio.sleep(interval)
            try:
                redispatch_queued_jobs()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Automatic queue tick failed")

    def _phase_age_seconds(value: str) -> Optional[float]:
        if not value:
            return None
        try:
            started = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - started).total_seconds()

    def _stall_limit(status: str) -> float:
        try:
            if status == "generating":
                return float(os.getenv("DOUBAO_STALL_GENERATING_SECONDS", "1080"))
            if status == "downloading":
                return float(os.getenv("DOUBAO_STALL_DOWNLOADING_SECONDS", "600"))
            return float(os.getenv("DOUBAO_STALL_PREPARE_SECONDS", "300"))
        except ValueError:
            return 300.0

    async def stall_watchdog_once() -> int:
        """Kill workers that stopped making progress and release their resources.

        This is the last line of defence for “绝对不能卡”: even an unforeseen
        Playwright/browser hang only costs one phase timeout, never the queue.
        """
        with db() as c:
            rows = c.execute(
                "SELECT j.id,j.status,j.updated_at,w.phase_started_at "
                "FROM jobs j JOIN video_web_jobs w ON j.id=w.job_id "
                f"WHERE j.status IN ({','.join('?' * len(ACTIVE))})",
                tuple(ACTIVE),
            ).fetchall()
        interrupted = 0
        for row in rows:
            jobid = row["id"]
            task = pending.get(jobid)
            if task is None or task.done():
                # Orphaned running row (worker vanished without a terminal emit).
                age = _phase_age_seconds(row["phase_started_at"] or row["updated_at"])
                if age is None or age < 120:
                    continue
                emit(
                    jobid,
                    "needs_review",
                    "执行器已退出但任务未收尾，已自动释放资源",
                    code="STALL_TIMEOUT",
                )
                interrupted += 1
                continue
            limit = _stall_limit(row["status"])
            age = _phase_age_seconds(row["phase_started_at"])
            if age is None or age <= limit:
                continue
            emit(
                jobid,
                "needs_review",
                f"阶段 {row['status']} 超过 {int(limit)} 秒无进展，已自动中断并释放资源",
                code="STALL_TIMEOUT",
            )
            task.cancel()
            interrupted += 1
        return interrupted

    async def stall_watchdog_loop() -> None:
        try:
            interval = max(1.0, float(os.getenv("DOUBAO_STALL_CHECK_SECONDS", "15")))
        except ValueError:
            interval = 15.0
        while True:
            await asyncio.sleep(interval)
            try:
                await stall_watchdog_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Stall watchdog failed")

    app.state.video_queue_tick = queue_tick_loop
    app.state.video_stall_watchdog = stall_watchdog_loop
    app.state.video_redispatch = redispatch_queued_jobs
    app.state.video_stall_watchdog_once = stall_watchdog_once

    def resume_queued_jobs() -> int:
        """Restart persisted automatic jobs; manual jobs still require a person."""
        with db() as c:
            rows = c.execute(
                "SELECT j.id,j.account_id FROM jobs j "
                "JOIN video_web_jobs w ON j.id=w.job_id "
                "WHERE j.status='queued' AND COALESCE(w.submit_mode,'auto')='auto' "
                "ORDER BY j.created_at ASC"
            ).fetchall()

        started = 0
        for row in rows:
            # 未绑账号是自动任务的常态（账号在进入处理前才分配），不能因此跳过：
            # 否则重启后这类排队任务再无人认领，自动队列静默停摆。worker 会在拿到
            # 名额后自行分配账号，分配不到就退回待处理并记录原因。
            try:
                if row["account_id"]:
                    # 已绑定账号的要先确认可用；未绑定的交给 worker 分配。
                    account = get_account(row["account_id"])
                    if bool(account["archived"]) or bool(
                        account_value(account, "paused", 0)
                    ):
                        continue
                req = assemble_req(row["id"])
                start_worker(row["id"], req, avoid_account_for(row["id"]))
                started += 1
            except Exception:
                logger.exception(
                    "Could not resume queued job %s after startup",
                    row["id"],
                )
        if started:
            logger.info("Resumed %s persisted automatic jobs", started)
        return started

    app.state.video_resume_queue = resume_queued_jobs

    @app.get("/api/v1/videos/capabilities",tags=["Doubao Web Video"])
    async def capabilities():
        return {"provider":"doubao_web","modes":list(MODES),
                "ratios":"doubao_ui_defined","durations":"doubao_ui_defined",
                "state":"implemented_live_verified","precise_ui_controls_required":True,
                "assets":"local_upload_with_explicit_slots","automatic_account_rotation":False,
                "submit_modes":["auto","manual"],
                "manual_flow":"manual 任务创建后先排队；在本机点击开始后只填提示词和素材，不点发送；"
                              "操作者在豆包网页发送，出片后点关联下载（recall）归档；"
                              "需要重发时用 resend，job_id 不变，关联下载只认新页面",
                "manual_resend":True,"manual_resend_keeps_job_id":True,
                "manual_fail_verdict":True,
                "manual_review_upload":True,"reconcile_requires_exact_video_url":True,
                "safe_restart":"no_resubmit_if_interrupted",
                "daily_video_seconds":DAILY_VIDEO_SECONDS,
                "account_auto_select":"quota_aware",
                "note":"duration/ratio 以豆包网页实际提供的选项为准（常见 16:9/9:16/1:1、4/10/15 秒），适配器不再硬编码限制；账号每天额度上限可经 DOUBAO_DAILY_VIDEO_SECONDS 覆盖"}

    @app.get("/api/v1/videos/confirm-answer", tags=["Doubao Web Video"])
    async def confirm_answer(x_api_key: Optional[str] = Header(None)):
        """The exact text the adapter sends when Doubao asks to confirm generation."""
        auth(x_api_key); ensure_schema()
        return {"text": default_confirmation_text()}

    # ---- 单入口接口（供外部系统集成，拉取式）----
    # 一个 multipart 请求带上所有图片与参数即可提交；之后轮询状态、下载视频。

    @app.post("/api/v1/videos", status_code=202, tags=["Doubao Web Video"])
    async def create_video_v1(
        account_id: Optional[str] = Form(default=None),
        prompt: str = Form(...),
        mode: str = Form("text2video"),
        ratio: str = Form("16:9"),
        duration: int = Form(4),
        model: Optional[str] = Form(default=None),
        project_name: str = Form(""),
        episode_name: str = Form(""),
        shot_name: str = Form(""),
        task_name: str = Form(""),
        # Compatibility for existing callers. New integrations should send the *_name fields above.
        project_id: str = Form(""),
        episode_id: str = Form(""),
        shot_id: str = Form(""),
        idempotency_key: Optional[str] = Form(default=None),
        result_push_url: Optional[str] = Form(default=None),
        result_push_token: Optional[str] = Form(default=None),
        auto_start: bool = Form(default=True),
        submit_mode: Optional[str] = Form(default=None),
        reference: Optional[List[UploadFile]] = File(default=None),
        first_frame: Optional[UploadFile] = File(default=None),
        last_frame: Optional[UploadFile] = File(default=None),
        x_api_key: Optional[str] = Header(default=None),
    ):
        """单入口提交：图片作为文件字段直接上传，无需先调用 /assets。"""
        auth(x_api_key); ensure_schema()
        if mode not in MODES:
            raise HTTPException(422, f"mode 必须是 {list(MODES)} 之一")
        if submit_mode is not None and submit_mode not in ("auto", "manual"):
            raise HTTPException(422, "submit_mode 必须是 auto（自动提交）或 manual（人工发送）")

        async def read_blob(f: UploadFile):
            b = await f.read(MAX_IMAGE + 1)
            await f.close()
            return b

        ref_ids = [store_image_asset(await read_blob(f), f.filename) for f in (reference or [])]
        first_id = store_image_asset(await read_blob(first_frame), first_frame.filename) if first_frame else None
        last_id = store_image_asset(await read_blob(last_frame), last_frame.filename) if last_frame else None

        has_ref, has_first, has_last = bool(ref_ids), bool(first_id), bool(last_id)
        if mode == "text2video" and (has_first or has_last or has_ref):
            raise HTTPException(422, "文生视频不能附带图片")
        if mode == "image2video" and (not has_first or has_last or has_ref):
            raise HTTPException(422, "首帧图生视频需要且只允许 first_frame")
        if mode == "first_last_frame" and (not has_first or not has_last or has_ref):
            raise HTTPException(422, "首尾帧模式需要 first_frame 和 last_frame")
        if mode == "reference_images" and (not has_ref or has_first or has_last or not (1 <= len(ref_ids) <= 9)):
            raise HTTPException(422, "参考图模式需要 1～9 个 reference 文件")

        try:
            req = VideoRequest(account_id=account_id, prompt=prompt, mode=mode, ratio=ratio, duration=duration,
                               model=model, project_id=project_name.strip() or project_id,
                               episode_id=episode_name.strip() or episode_id,
                               shot_id=shot_name.strip() or shot_id, task_name=task_name,
                               first_frame_asset_id=first_id, last_frame_asset_id=last_id,
                               reference_asset_ids=ref_ids, idempotency_key=idempotency_key, auto_start=auto_start,
                               submit_mode=submit_mode, result_push_url=result_push_url,
                               result_push_token=result_push_token)
        except ValidationError as e:
            raise HTTPException(422, f"参数校验失败：{e.errors()[0].get('msg', '')}")
        item = create_generation(req)
        return {"job_id": item["id"], "status": item["status"],
                "account_id": item["account_id"], "submit_mode": item["web"].get("submit_mode", "auto"),
                "status_url": f"/api/v1/videos/{item['id']}",
                "video_url": f"/api/v1/videos/{item['id']}/video"}

    @app.get("/api/v1/videos/{job_id}", tags=["Doubao Web Video"])
    async def video_status_v1(job_id: str, x_api_key: Optional[str] = Header(default=None)):
        auth(x_api_key); ensure_schema()
        item = detail(job_id)
        return {"job_id": job_id, "status": item["status"],
                "mode": item["web"].get("mode"), "ratio": item["web"].get("ratio"),
                "duration": item["web"].get("duration"),
                "submit_mode": item["web"].get("submit_mode", "auto"),
                "error_code": item["web"].get("error_code"), "error_message": item.get("error_message"),
                "result_source": item["web"].get("result_source"),
                "quota": item.get("quota"),
                "video_url": f"/api/v1/videos/{job_id}/video" if item.get("video_path") else None,
                "web_video_url": item["web"].get("video_url")}

    @app.get("/api/v1/videos/{job_id}/video", tags=["Doubao Web Video"])
    async def video_file_v1(job_id: str, x_api_key: Optional[str] = Header(default=None)):
        auth(x_api_key); ensure_schema()
        row = get_job(job_id)
        if not row["video_path"]:
            raise HTTPException(404, "该任务还没有归档视频")
        path = resolved_path(row["video_path"], downloads)
        if not path.is_file():
            raise HTTPException(404, "视频文件不存在")
        return FileResponse(path, filename=path.name)

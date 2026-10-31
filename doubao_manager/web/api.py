from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import httpx
from fastapi import File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from .automation import *
from .config import (
    ACTIVE,
    CHAT_PAGE_URL,
    DAILY_VIDEO_SECONDS,
    MANUAL_FAILED,
    MANUAL_WAIT,
    MAX_IMAGE,
    MAX_VIDEO,
    MODES,
    REAL_CHAT_URL,
    TERMINAL,
)
from .models import (
    BindPageRequest,
    ConfirmationPresetInput,
    ConfirmationSendRequest,
    ProcessingLimitSetting,
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
from .utils import adaptive_poll, automatic_step_delay, safe, ts

def mount_video_api(app, *, db, get_account, get_job, browser, base_dir, downloads_dir, serialize,
                    close_browser=None):
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
    # Serialize browser use per account. Different account profiles may run in parallel.
    # No account rotation or retry after submission.
    locks: dict[str, asyncio.Lock] = {}
    pending: dict[str, asyncio.Task] = {}
    # 人工发送任务停留期间保留它准备页面的句柄：操作者点“关联下载”时优先复用这个
    # 已经打开、且可能已经从 /chat/local_* 转成真实会话 id 的标签页。
    prepared_pages: dict[str, object] = {}
    app.state.video_pending = pending
    app.state.video_prepared_pages = prepared_pages
    # The processing limit controls how many unfinished tasks may occupy the queue.
    # A separate execution lane serializes browser work; manual and recall operations
    # still use per-account locks around their own browser actions.
    processing_condition = asyncio.Condition()
    active_processing = 0
    processing_job_ids: set[str] = set()
    processing_statuses = frozenset(ACTIVE) | {MANUAL_WAIT, "needs_review"}
    # Browser work is serialized independently from the processing queue. A task
    # releases this lane when it starts waiting for the generated video and reacquires
    # it for confirmation or download.
    execution_condition = asyncio.Condition()
    execution_job_id: str | None = None
    execution_resume_statuses = frozenset(ACTIVE) - {"generating"}
    idle_close_tasks: dict[str, asyncio.Task] = {}
    app.state.video_idle_close_tasks = idle_close_tasks

    def account_is_busy(account_id: str, ignore_jobid: str = "") -> bool:
        for other_jobid, task in list(pending.items()):
            if other_jobid == ignore_jobid or task.done():
                continue
            try:
                if get_job(other_jobid)["account_id"] == account_id:
                    return True
            except Exception:
                continue
        for other_jobid, page in list(prepared_pages.items()):
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
        nonlocal active_processing
        processing_job_ids.update(persisted_processing_job_ids())
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
            # Recover jobs left in a processing state by a previous process. The slot
            # belongs to the task until it reaches downloaded/failed, not to one call.
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
            # The slot is deliberately retained while the task waits for generation,
            # manual sending, review, or download. Terminal status changes release it.
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

    async def acquire_execution_slot(jobid: str) -> None:
        nonlocal execution_job_id
        async with execution_condition:
            await execution_condition.wait_for(
                lambda: execution_job_id in (None, jobid)
            )
            execution_job_id = jobid

    async def release_execution_slot(jobid: str) -> None:
        nonlocal execution_job_id
        async with execution_condition:
            if execution_job_id != jobid:
                return
            execution_job_id = None
            execution_condition.notify_all()

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
        value = (row["value"] if row else "auto").strip().lower()
        return value if value in ("auto", "manual") else "auto"

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
            previous = c.execute("SELECT status FROM jobs WHERE id=?", (jobid,)).fetchone()
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
            if url: c.execute("UPDATE video_web_jobs SET web_page_url=? WHERE job_id=?", (url, jobid))
            if result:
                relative = stored_path(Path(result["output"]), downloads)
                c.execute("UPDATE jobs SET video_path=? WHERE id=?", (relative, jobid))
                c.execute("UPDATE video_web_jobs SET video_url=?, web_page_url=?, error_code='', result_source=? WHERE job_id=?",
                          (result["video_url"], result["web_page_url"], result_source, jobid))
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (jobid,status,msg,now))
            settle_quota(c, jobid, status, prior_status=prior_status, error_code=code)
        if status in ("downloaded", "failed"):
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
        val["web"].pop("recall_token", None)
        val["web"].pop("result_push_token", None)
        val["task_name"] = val["web"].get("task_name") or ""
        val["video_file_url"] = f"/api/jobs/{jobid}/video" if row["video_path"] else None
        if quota: val["quota"] = quota
        return val

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
            if quota_held_today(w):
                return
            duration = int(w["duration"] or 0)
            remaining = quota_summary(c, account_id)["remaining_seconds"]
            if duration > remaining:
                raise HTTPException(
                    409, f"账号当日剩余额度不足（需 {duration} 秒，剩余 {remaining} 秒）"
                )
            reserve_quota(c, account_id, duration)
            c.execute(
                "UPDATE video_web_jobs SET quota_day=?,quota_state='reserved' WHERE job_id=?",
                (local_day(), jobid),
            )

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

    async def worker(jobid, req):
        heartbeat_stop = asyncio.Event()
        heartbeat_task = None
        execution_held = False
        try:
            async with browser_slot(jobid):
                await acquire_execution_slot(jobid)
                execution_held = True
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
                         "已取得处理名额，开始浏览器操作", now),
                    )
                heartbeat_task = asyncio.create_task(
                    heartbeat_loop(jobid, heartbeat_stop)
                )
                assets = resolve_assets(req)
                ctx = await browser(req.account_id)
                target = downloads / "library" / safe(req.project_id) / safe(req.episode_id) / safe(req.shot_id or jobid) / (jobid + ".mp4")

                async def status(s, msg):
                    nonlocal execution_held
                    if s in execution_resume_statuses and not execution_held:
                        await acquire_execution_slot(jobid)
                        execution_held = True
                    emit(jobid, s, msg)
                    if s == "generating" and execution_held:
                        await release_execution_slot(jobid)
                        execution_held = False

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
                    prepared_pages[jobid] = page
                    url = state.get("url") or ""
                    with db() as c:
                        c.execute(
                            "UPDATE video_web_jobs SET web_page_url=?,"
                            "result_card_index=? WHERE job_id=?",
                            (url, int(state.get("videoCards") or 0), jobid),
                        )
                    emit(
                        jobid, MANUAL_WAIT,
                        "已把提示词和素材填入豆包输入框（未发送）；请在豆包网页亲自发送，"
                        "生成完成后回到管理台点击“关联下载”。",
                        url=url,
                    )

                async def review_page(page):
                    prepared_pages[jobid] = page

                result = await perform(
                    ctx, req, assets, target, status, page_url, binding,
                    on_manual_ready=manual_ready,
                    confirmation_text=default_confirmation_text(),
                    close_on_success=True, on_review_page=review_page,
                )
                if result.get("manual"):
                    return
                await automatic_step_delay(req.submit_mode or "auto")
                await finish_downloaded(
                    jobid, "已下载并校验视频", result,
                    close_account_after=False,
                )
        except ReviewNeeded as e:
            emit(jobid, "needs_review", e.message, code=e.code)
        except Exception as e:
            emit(jobid, "needs_review", str(e)[:350], code="WORKER_EXCEPTION")
        finally:
            if execution_held:
                await release_execution_slot(jobid)
            heartbeat_stop.set()
            if heartbeat_task is not None:
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            pending.pop(jobid, None)
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
        """
        with db() as c:
            rows = c.execute("SELECT * FROM accounts WHERE archived=0 ORDER BY created_at ASC").fetchall()
        active_rows = [row for row in rows if not bool(account_value(row, "paused", 0))]
        if not rows:
            raise HTTPException(409, "没有可用账号，请先创建账号档案并登录豆包")
        if not active_rows:
            raise HTTPException(409, "所有账号均已暂停使用，请先恢复至少一个账号")
        busy = {get_job(jid)["account_id"] for jid, t in pending.items() if not t.done()}
        with db() as c:
            candidates = []
            for r in active_rows:
                remaining = quota_summary(c, r["id"])["remaining_seconds"]
                if remaining >= required_seconds:
                    candidates.append((r["id"], r["created_at"], remaining,
                                       int(account_value(r, "priority", 0) or 0)))
        if candidates:
            candidates.sort(key=lambda item: (
                item[0] == avoid_account_id,
                item[2] - required_seconds,
                item[0] in busy,
                -item[3],
                item[2],
                item[1],
            ))
            return candidates[0][0]
        raise HTTPException(409, f"所有账号当日视频额度不足（本次需 {required_seconds} 秒，每日上限 {DAILY_VIDEO_SECONDS} 秒）")

    def create_generation(req: VideoRequest, *, retry_of_job_id: str = "", avoid_account_id: str = ""):
        """Persist a generation request and optionally start it. Shared by the JSON and multipart entry points."""
        req.submit_mode = req.submit_mode or local_submit_mode()
        # 人工发送必须由本机操作者显式启动：即使调用方传 auto_start=true 也只建单排队，
        # 防止 H3/外部调度器在用户不在电脑前时替人完成网页发送。
        if req.submit_mode == "manual":
            req.auto_start = False
        if not req.account_id:
            req.account_id = auto_select_account(req.duration, avoid_account_id)
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
                remaining = quota_summary(c, req.account_id)["remaining_seconds"]
                if req.duration > remaining:
                    raise HTTPException(
                        409,
                        f"账号当日剩余额度不足（需 {req.duration} 秒，剩余 {remaining} 秒）",
                    )
                c.execute("INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)"
                          " VALUES(?,?,?,?,?,?,'queued',?,?)",
                          (jobid, req.project_id, req.episode_id, req.shot_id, req.prompt, req.account_id, stamp, stamp))
                c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,idempotency_key,mode,assets_json,request_fingerprint,model,quota_day,quota_state,task_name,retry_of_job_id,submit_mode,result_push_url,result_push_token)"
                          " VALUES(?,?,?,?,?,?,?,?,?,'reserved',?,?,?,?,?)",
                          (jobid, req.ratio, req.duration, req.idempotency_key, req.mode, json.dumps(payload), fp,
                           req.model or "", local_day(), req.task_name.strip(), retry_of_job_id, req.submit_mode,
                           req.result_push_url or "", req.result_push_token or ""))
                reserve_quota(c, req.account_id, req.duration)
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
             "人工发送任务已排队：请在本机任务列表点击“发送到豆包”"
             if req.submit_mode == "manual" else "任务已写入持久化队列")
        if req.auto_start: start_worker(jobid, req)
        return detail(jobid)

    @app.post("/api/v1/videos/generations", status_code=202, tags=["Doubao Web Video"])
    async def create(req: VideoRequest, x_api_key: Optional[str] = Header(None)):
        auth(x_api_key); ensure_schema()
        return create_generation(req)

    def start_worker(jobid,req):
        # Keep persisted status queued until account and global processing slots are free.
        account = get_account(req.account_id)
        idle_close = idle_close_tasks.pop(req.account_id, None)
        if idle_close is not None and not idle_close.done():
            idle_close.cancel()
        if bool(account_value(account, "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        enforce_account_model(req, account)
        ensure_start_quota(jobid, req.account_id)
        task = pending.get(jobid)
        if task is not None and not task.done():
            raise HTTPException(409,"任务已经在等待或执行，拒绝重复提交")
        with db() as c:
            if bool(account_value(account, "is_member", 0)):
                c.execute("UPDATE video_web_jobs SET model=? WHERE job_id=?", (MEMBER_MODEL, jobid))
            row = c.execute("SELECT status FROM jobs WHERE id=?", (jobid,)).fetchone()
            if not row or row["status"] != "queued":
                raise HTTPException(409,"任务可能已提交或处理中，拒绝重复提交")
        pending[jobid] = asyncio.create_task(worker(jobid,req))

    @app.get("/api/v1/videos/generations/{jobid}",tags=["Doubao Web Video"])
    async def query(jobid:str,x_api_key:Optional[str]=Header(None)):
        auth(x_api_key); ensure_schema(); return detail(jobid)

    @app.get("/api/v1/videos/generations/{jobid}/events",tags=["Doubao Web Video"])
    async def events(jobid:str,x_api_key:Optional[str]=Header(None)):
        auth(x_api_key); ensure_schema(); detail(jobid)
        with db() as c:
            return [dict(r) for r in c.execute("SELECT status,message,timestamp FROM video_web_events WHERE job_id=? ORDER BY id",(jobid,))]

    @app.post("/api/v1/videos/generations/{jobid}/start",tags=["Doubao Web Video"])
    async def start(jobid:str,x_api_key:Optional[str]=Header(None)):
        auth(x_api_key); ensure_schema()
        req = assemble_req(jobid)
        resolve_assets(req)
        if get_account(req.account_id)["archived"]: raise HTTPException(409,"账号已归档")
        start_worker(jobid,req)
        return detail(jobid)

    async def reset_generation(jobid: str):
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
        if get_account(req.account_id)["archived"]:
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
            # 同一天已经占用的额度直接沿用，避免同一次生成意图被重复占用；
            # 只有额度已释放/跨天/旧任务才需要重新占用当天额度。
            held_today = quota_held_today(w)
            if not held_today:
                if state in ("reserved","unknown") and w["quota_day"]:
                    c.execute("UPDATE account_quota SET reserved_seconds=MAX(0,reserved_seconds-?) "
                              "WHERE account_id=? AND day=?",(dur, req.account_id, w["quota_day"]))
                remaining = quota_summary(c, req.account_id)["remaining_seconds"]
                if dur > remaining:
                    raise HTTPException(409,f"账号当日剩余额度不足（需 {dur} 秒，剩余 {remaining} 秒）")
                reserve_quota(c, req.account_id, dur, day)
                c.execute("UPDATE video_web_jobs SET quota_day=?, quota_state='reserved' WHERE job_id=?",
                          (day, jobid))
            now = ts()
            claimed = c.execute(
                "UPDATE jobs SET status='queued', error_message='', updated_at=? "
                "WHERE id=? AND status=?",
                (now, jobid, item["status"]))
            if not claimed.rowcount:
                raise HTTPException(409,"任务状态已变化，请刷新后再试")
            c.execute("""UPDATE video_web_jobs
                         SET web_page_url='', result_card_index=NULL, video_url='', result_source='',
                             error_code='', error_class='', next_retry_at='',
                             recall_state='', recall_token='', recall_started_at='',
                             phase='queued', phase_started_at=?, last_heartbeat_at=?
                         WHERE job_id=?""", (now, now, jobid))
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (jobid,"queued","任务已重置为待执行，旧会话与旧结果不再关联",ts()))
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
            if get_account(req.account_id)["archived"]:
                raise HTTPException(409, "账号已归档")
        else:
            req = await reset_generation(jobid)
        req.submit_mode = "manual"
        start_worker(jobid, req)
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/restart-auto", tags=["Doubao Web Video"])
    async def restart_auto(jobid: str, x_api_key: Optional[str] = Header(None)):
        """Restart an idle unfinished task from the beginning in automatic mode."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        if item.get("account_id") and bool(account_value(get_account(item["account_id"]), "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        if item["status"] == "queued":
            req = assemble_req(jobid)
            resolve_assets(req)
            if get_account(req.account_id)["archived"]:
                raise HTTPException(409, "账号已归档")
        else:
            req = await reset_generation(jobid)
        req.submit_mode = "auto"
        start_worker(jobid, req)
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
        start_worker(jobid, req)
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
        if row["status"]!="queued" or not row["account_id"]:
            raise HTTPException(409,"分镜必须是 queued 状态且已选择账号")
        if bool(account_value(get_account(row["account_id"]), "paused", 0)):
            raise HTTPException(409, "账号已暂停使用，请先恢复账号")
        req = VideoRequest(account_id=row["account_id"],project_id=row["project_id"],
                           episode_id=row["episode_id"],shot_id=row["shot_id"],prompt=row["prompt"],
                           submit_mode=submit_mode)
        with db() as c:
            if c.execute("SELECT 1 FROM video_web_jobs WHERE job_id=?",(jobid,)).fetchone():
                raise HTTPException(409,"分镜已经是网页视频任务")
            remaining = quota_summary(c, req.account_id)["remaining_seconds"]
            if req.duration > remaining:
                raise HTTPException(
                    409, f"账号当日剩余额度不足（需 {req.duration} 秒，剩余 {remaining} 秒）"
                )
            c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,mode,assets_json,quota_day,quota_state,submit_mode) VALUES(?,?,?,?,?,?,'reserved',?)",
                      (jobid,req.ratio,req.duration,req.mode,"{}",local_day(),submit_mode))
            reserve_quota(c, req.account_id, req.duration)
        if submit_mode == "manual":
            emit(jobid, "queued", "人工发送任务已排队；请在任务列表点击“发送到豆包”")
        else:
            start_worker(jobid,req)
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
            close_account_after=True)
        try:
            await page.close()
        except Exception:
            pass
        return detail(jobid)

    @app.post("/api/v1/videos/generations/{jobid}/bind-page", tags=["Doubao Web Video"])
    async def bind_page(jobid: str, payload: BindPageRequest,
                        x_api_key: Optional[str] = Header(None)):
        """Bind a manually identified Doubao conversation without starting recall."""
        auth(x_api_key); ensure_schema()
        item = detail(jobid)
        if item["status"] not in (MANUAL_WAIT, "needs_review", "failed"):
            raise HTTPException(409, "仅待人工发送、需人工处理或失败的任务可以绑定网页")
        task = pending.get(jobid)
        if task is not None and not task.done():
            raise HTTPException(409, "任务正在执行，请等待当前步骤结束后再绑定网页")
        page_url = payload.page_url.strip()
        parsed = urlparse(page_url)
        try:
            trusted_port = parsed.port in (None, 443)
        except ValueError:
            trusted_port = False
        if (parsed.scheme != "https"
                or (parsed.hostname or "").lower() not in ("doubao.com", "www.doubao.com")
                or parsed.username is not None or parsed.password is not None
                or not trusted_port or not REAL_CHAT_URL.fullmatch(parsed.path)):
            raise HTTPException(422, "请提供 https://www.doubao.com/chat/<会话ID> 格式的具体会话地址")
        current_url = (item["web"].get("web_page_url") or "").strip()
        changed_conversation = current_url.split("?")[0] != page_url.split("?")[0]
        with db() as c:
            if changed_conversation:
                c.execute("""UPDATE video_web_jobs
                             SET web_page_url=?, result_card_index=NULL, video_url='', result_source=''
                             WHERE job_id=?""", (page_url, jobid))
            else:
                c.execute("UPDATE video_web_jobs SET web_page_url=? WHERE job_id=?",
                          (page_url, jobid))
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (jobid, item["status"], "已人工绑定具体豆包会话，等待关联下载", ts()))
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
            # Enforce stream validation when ffprobe is installed.
            import shutil
            if shutil.which("ffprobe"):
                proc = await asyncio.create_subprocess_exec(
                    "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                    "stream=codec_type", "-of", "default=noprint_wrappers=1", str(tmp),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, _ = await proc.communicate()
                if proc.returncode or b"codec_type=video" not in out:
                    raise HTTPException(422, "视频流验证失败")
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
        # 人工发送任务的标签页可能还没有真实会话 URL，但它本身就是任务页面。
        prepared = prepared_pages.get(jobid)
        if prepared is not None and not prepared.is_closed():
            await prepared.bring_to_front()
            return {"ok": True, "job_id": jobid, "url": prepared.url, "reused": True}
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
        busy = jobid in pending and not pending[jobid].done()
        ctx = await browser(account_id)
        key = url.split("?")[0]
        for existing in ctx.pages:
            try:
                if (existing.url or "").split("?")[0] == key:
                    await existing.bring_to_front()
                    return {"ok": True, "job_id": jobid, "url": url, "reused": True}
            except Exception:
                continue
        if busy:
            raise HTTPException(409, "任务正在执行且页面已切换，暂不能跳转；请等任务结束后再打开")
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.bring_to_front()
        return {"ok": True, "job_id": jobid, "url": url, "reused": False}

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
                    raise HTTPException(409, "点击后无法确认是否发送成功；任务已转为需人工处理，禁止重复发送")
                live = await persisted_url(page, after.get("url") or "")
                emit(jobid, "confirming", "已发送任务提示词，等待发送确认语", url=live)
                return detail(jobid)

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
            if w and not quota_held_today(w):
                dur = int(w["duration"] or 0)
                remaining = quota_summary(c, account_id)["remaining_seconds"]
                if dur > remaining:
                    raise HTTPException(409, f"账号当日剩余额度不足（需 {dur} 秒，剩余 {remaining} 秒）")
        ctx = await browser(account_id)
        page = None
        created_page = False
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
        try:
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
                dur = int(w["duration"] or 0) if w else 0
                day = local_day()
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
                    c.execute("UPDATE video_web_jobs SET web_page_url=? WHERE job_id=?", (live, jobid))
                c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                          (jobid, next_status, f"人工发送确认语预设“{preset['name']}”：{how}", ts()))
                if w and not quota_held_today(w):
                    c.execute("UPDATE video_web_jobs SET quota_day=?, quota_state='reserved', error_code='' WHERE job_id=?",
                              (day, jobid))
                    reserve_quota(c, account_id, dur, day)
                else:
                    c.execute("UPDATE video_web_jobs SET error_code='' WHERE job_id=?", (jobid,))
            return {"ok": True, "job_id": jobid, "preset_id": preset["id"],
                    "preset_name": preset["name"], "how": how,
                    "message": "确认语已发送"}
        except Exception:
            if created_page:
                try:
                    await page.close()
                except Exception:
                    pass
            raise

    def claim_recall(jobid: str) -> str:
        """Atomically claim a persisted recall lease; stale leases may be reclaimed."""
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
            c.execute("UPDATE jobs SET status='generating', error_message='', updated_at=? WHERE id=?",
                      (now, jobid))
            c.execute(
                "UPDATE video_web_jobs SET phase='generating',phase_started_at=?,"
                "last_heartbeat_at=?,error_code='',error_class='' WHERE job_id=?",
                (now, now, jobid),
            )
            c.execute("INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                      (jobid, "generating", "已取得数据库召回锁，使用原会话等待结果（不会重新提交）", ts()))
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
        try:
            item = detail(jobid)
            account_id = item.get("account_id")
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
                            with db() as c:
                                c.execute("UPDATE video_web_jobs SET web_page_url=? WHERE job_id=?", (url, jobid))
                    else:
                        prepared_pages.pop(jobid, None)
                        page = await ctx.new_page()
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
                            got = media_duration_seconds(path)
                            want = int(item["web"].get("duration") or 0)
                            if got is not None and want and abs(got - want) > 1.5:
                                Path(path).unlink(missing_ok=True)
                                raise ReviewNeeded("DURATION_MISMATCH",
                                                   f"下载视频时长 {got:.1f}s 与任务 {want}s 不符，未归档")
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
        except Exception as e:
            if preserve_downloaded:
                emit(jobid, "downloaded", "重新下载失败，已保留原归档视频：" + str(e)[:300], code="RECALL_EXCEPTION")
            else:
                emit(jobid, "needs_review", str(e)[:350], code="RECALL_EXCEPTION")
        finally:
            # 下载成功后，无论页面来自人工准备还是本次召回都关闭；失败时保留复用页面供人工检查。
            if download_succeeded:
                prepared_pages.pop(jobid, None)
            if page is not None and (download_succeeded or not reused):
                try:
                    await page.close()
                except Exception:
                    pass
            release_recall(jobid, token)
            pending.pop(jobid, None)
            schedule_idle_close(account_id, jobid)

    async def begin_recall(jobid: str, wait: bool = False, force_redownload: bool = False):
        item = detail(jobid)
        preserve_downloaded = item["status"] == "downloaded"
        if preserve_downloaded and not force_redownload:
            return item
        if jobid in pending and not pending[jobid].done():
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
        token = claim_recall(jobid)
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
        return {
            "ok": True,
            "job_id": jobid,
            "pushed_at": pushed_at,
            "delivery_state": "delivered",
            "delivery_attempts": attempt,
        }

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

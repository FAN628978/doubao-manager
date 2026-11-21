from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sqlite3
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, BrowserContext, Playwright

from doubao_manager.chrome_runtime import extension_launch_args, resolve_project_chrome

from doubao_manager.windows_taskbar import (
    account_title_prefix,
    apply_account_taskbar_identity,
)

SOURCE_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SOURCE_DIR.parent
STATIC_DIR = SOURCE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
PROFILES_DIR = DATA_DIR / "profiles"
DOWNLOADS_DIR = DATA_DIR / "videos"
TRASH_DIR = DATA_DIR / "trash"
ACCEPTANCE_REPORTS_DIR = DATA_DIR / "acceptance_reports"
TASKBAR_ICONS_DIR = DATA_DIR / "taskbar-icons"
DB_PATH = DATA_DIR / "manager.sqlite3"
DOUBAO_URL = "https://www.doubao.com/"
ACCOUNT_NAME_PREFIX = "doubao"
DATA_SCHEMA_VERSION = 2
CACHE_RELATIVE_DIRS = (
    Path("Default") / "Cache",
    Path("Default") / "Code Cache",
    Path("Default") / "GPUCache",
    Path("Default") / "Shared Dictionary" / "cache",
)

for directory in (DATA_DIR, PROFILES_DIR, DOWNLOADS_DIR, TRASH_DIR, TASKBAR_ICONS_DIR):
    directory.mkdir(parents=True, exist_ok=True)

playwright_runtime: Optional[Playwright] = None
open_contexts: dict[str, BrowserContext] = {}
context_locks: dict[str, asyncio.Lock] = {}
logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def portable_path(value: str | Path) -> Path:
    """Resolve a persisted relative path and keep it inside the EXE's data directory."""
    path = Path(value)
    if path.is_absolute():
        raise ValueError(f"持久化路径必须是相对路径：{value}")
    resolved = (BASE_DIR / path).resolve()
    data_root = DATA_DIR.resolve()
    if resolved != data_root and data_root not in resolved.parents:
        raise ValueError(f"持久化路径必须位于 data 目录内：{value}")
    return resolved


def stored_data_path(path: str | Path) -> str:
    """Convert a runtime path under data into the portable value stored in SQLite."""
    resolved = Path(path).resolve()
    data_root = DATA_DIR.resolve()
    if resolved != data_root and data_root not in resolved.parents:
        raise ValueError(f"只能保存 data 目录内的文件路径：{path}")
    return str(resolved.relative_to(BASE_DIR.resolve()))


def account_profile_path(account_id: str) -> tuple[str, Path]:
    stored = str(Path("data") / "profiles" / account_id)
    return stored, portable_path(stored)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with db() as conn:
        # Several accounts may now write job/event rows in parallel. WAL keeps
        # readers from blocking writers and sharply reduces "database is locked"
        # retries when a batch of jobs is admitted at once.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current_version > DATA_SCHEMA_VERSION:
            raise RuntimeError(f"data 数据版本 {current_version} 高于当前软件支持的 {DATA_SCHEMA_VERSION}")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                profile_dir TEXT NOT NULL,
                is_member INTEGER NOT NULL DEFAULT 0,
                priority INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_opened_at TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL DEFAULT '',
                episode_id TEXT NOT NULL DEFAULT '',
                shot_id TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL,
                account_id TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                video_path TEXT,
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS account_quota (
                account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                day TEXT NOT NULL,
                reserved_seconds INTEGER NOT NULL DEFAULT 0,
                consumed_seconds INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (account_id, day)
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_account_quota_day ON account_quota(day);
            """
        )
        account_columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
        if "is_member" not in account_columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN is_member INTEGER NOT NULL DEFAULT 0")
        if "priority" not in account_columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        if "paused" not in account_columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN paused INTEGER NOT NULL DEFAULT 0")
        # Persist only paths relative to the executable. This makes a copied data folder
        # portable even when the new release is extracted to a different absolute path.
        for row in conn.execute("SELECT id, profile_dir FROM accounts").fetchall():
            stored, _ = account_profile_path(row["id"])
            if row["profile_dir"] != stored:
                conn.execute("UPDATE accounts SET profile_dir = ? WHERE id = ?", (stored, row["id"]))
        conn.execute(f"PRAGMA user_version = {DATA_SCHEMA_VERSION}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global playwright_runtime
    init_db()
    from web_api import recover_interrupted_jobs
    recover_interrupted_jobs(db, base_dir=BASE_DIR)
    playwright_runtime = await async_playwright().start()
    resume_queue = getattr(app.state, "video_resume_queue", None)
    if resume_queue is not None:
        try:
            resume_queue()
        except Exception:
            logger.exception("Could not resume persisted automatic jobs")
    # Always-on maintenance: re-admit auto jobs that lost their worker, and cut
    # off workers that stopped progressing so their locks cannot freeze the queue.
    maintenance_tasks = []
    for hook in (
        "video_queue_tick",
        "video_scheduled_dispatch",
        "video_stall_watchdog",
    ):
        factory = getattr(app.state, hook, None)
        if factory is not None:
            maintenance_tasks.append(asyncio.create_task(factory()))
    yield
    for task in maintenance_tasks:
        task.cancel()
    if maintenance_tasks:
        await asyncio.gather(*maintenance_tasks, return_exceptions=True)
    # Cancel unfinished workers before closing their browser contexts. On shutdown,
    # interrupted jobs are conservatively marked for human review, never re-sent.
    running = getattr(app.state, "video_pending", {})
    tasks = list(running.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        recover_interrupted_jobs(db, base_dir=BASE_DIR)
    idle_closes = list(getattr(app.state, "video_idle_close_tasks", {}).values())
    for task in idle_closes:
        task.cancel()
    if idle_closes:
        await asyncio.gather(*idle_closes, return_exceptions=True)
    for account_id, context in list(open_contexts.items()):
        try:
            await context.close()
        except Exception:
            pass
    open_contexts.clear()
    if playwright_runtime:
        await playwright_runtime.stop()
        playwright_runtime = None


app = FastAPI(
    title="Doubao Account Desk API",
    description="本机运行的账号档案、浏览器会话、分镜任务与视频归档 API。豆包操作需由用户在浏览器内确认。",
    version="0.3.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_for_frontend(request, call_next):
    """页面与前端资源禁用缓存。

    这个管理台是本地工具，前端文件会随升级频繁变化；如果浏览器缓存了旧的
    app.js，界面看起来"刷新没生效"，实际是加载了旧脚本。API 响应本来就不缓存，
    这里只对页面和 /static 资源加 no-cache（仍允许用 ETag 做校验）。
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/") or path.endswith(".js") or path.endswith(".css"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


class AccountCreate(BaseModel):
    # Optional: an omitted, null or blank name falls back to the doubao0N default.
    name: Optional[str] = Field(default=None, max_length=80)
    note: str = Field(default="", max_length=500)
    is_member: bool = False
    priority: int = Field(default=0, ge=0, le=1000)


class AccountSettings(BaseModel):
    is_member: bool
    priority: int = Field(ge=0, le=1000)


class JobCreate(BaseModel):
    project_id: str = Field(default="", max_length=120)
    episode_id: str = Field(default="", max_length=120)
    shot_id: str = Field(default="", max_length=120)
    prompt: str = Field(min_length=1, max_length=20000)
    account_id: Optional[str] = None


class JobUpdate(BaseModel):
    status: Optional[str] = None
    account_id: Optional[str] = None
    error_message: Optional[str] = Field(default=None, max_length=2000)


class JobBulkDelete(BaseModel):
    job_ids: list[str] = Field(min_length=1, max_length=500)


VALID_STATUSES = {"queued", "ready", "generating", "downloaded", "failed", "needs_review"}
# UI buckets. The web state machine only lets machine-owned phases occupy a
# processing slot; waiting for a person is its own bucket. “已归档” is not a
# status: it is the user-confirmed archive of a downloaded job (archived_at).
STATUS_BUCKETS = {
    "pending": ("queued", "ready"),
    "processing": ("opening", "configuring", "uploading", "submitting",
                   "confirming", "generating", "downloading"),
    "attention": ("awaiting_manual_send", "needs_review"),
    "archived": ("downloaded",),
    "failed": ("failed",),
}
JOB_DELETE_BLOCKED_STATUSES = {
    "pending", "opening", "configuring", "uploading", "submitting",
    "generating", "confirming", "downloading", "canceling",
}


def account_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "note": row["note"],
        "is_member": bool(row["is_member"]),
        "priority": int(row["priority"]),
        "paused": bool(row["paused"]),
        "archived": bool(row["archived"]),
        "profile_dir": row["profile_dir"],
        "created_at": row["created_at"],
        "last_opened_at": row["last_opened_at"],
        "browser_open": row["id"] in open_contexts,
    }


def job_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "episode_id": row["episode_id"],
        "shot_id": row["shot_id"],
        "prompt": row["prompt"],
        "account_id": row["account_id"],
        "status": row["status"],
        "video_path": row["video_path"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_account(account_id: str) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="账号档案不存在")
    return row


def get_job(job_id: str) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="分镜任务不存在")
    return row


def next_account_name(conn: sqlite3.Connection) -> str:
    """Suggested profile name: doubao01, doubao02 … continuing after the highest used number."""
    taken = {str(row["name"]).strip().lower() for row in conn.execute("SELECT name FROM accounts")}
    highest = 0
    for name in taken:
        match = re.fullmatch(rf"{ACCOUNT_NAME_PREFIX}(\d+)", name)
        if match:
            highest = max(highest, int(match.group(1)))
    number = highest + 1
    while f"{ACCOUNT_NAME_PREFIX}{number:02d}" in taken:
        number += 1
    return f"{ACCOUNT_NAME_PREFIX}{number:02d}"


def account_busy(account_id: str) -> bool:
    """True while a web generation worker for this account is still running."""
    return any(not task.done()
               for job_id, task in getattr(app.state, "video_pending", {}).items()
               if get_job(job_id)["account_id"] == account_id)


def account_sequence(account_id: str) -> int:
    """Return the account's one-based position in the default account list."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM accounts WHERE archived = 0 ORDER BY priority DESC, created_at DESC"
        ).fetchall()
    return next((index for index, row in enumerate(rows, 1) if row["id"] == account_id), 0)


def account_title_script(sequence: int) -> str:
    prefix = json.dumps(account_title_prefix(sequence), ensure_ascii=False)
    return f"""(() => {{
        const prefix = {prefix};
        const install = () => {{
            if (!window.__doubaoDeskTaskTitle) {{
                try {{
                    window.__doubaoDeskTaskTitle = sessionStorage.getItem('__doubaoDeskTaskTitle') || '';
                }} catch (e) {{}}
            }}
            const apply = () => {{
                const current = document.title || '';
                const base = current.replace(/^\\[账号 \\d+\\] /, '');
                const wanted = window.__doubaoDeskTaskTitle || (prefix + base);
                if (current !== wanted) document.title = wanted;
            }};
            apply();
            if (!window.__doubaoDeskTitleObserver) {{
                window.__doubaoDeskTitleObserver = new MutationObserver(apply);
                window.__doubaoDeskTitleObserver.observe(document.head, {{subtree: true, childList: true, characterData: true}});
            }}
        }};
        if (document.head) install();
        else document.addEventListener('DOMContentLoaded', install, {{once: true}});
    }})()"""


def clear_profile_cache(profile_dir: Path) -> tuple[int, int]:
    """Delete only Chromium's regenerable cache folders from one account profile."""
    profile_dir = profile_dir.resolve()
    profiles_root = PROFILES_DIR.resolve()
    if profile_dir != profiles_root and profiles_root not in profile_dir.parents:
        raise ValueError("账号浏览器目录不在 data/profiles 内，已跳过清理")

    freed_bytes = 0
    removed_directories = 0
    for relative in CACHE_RELATIVE_DIRS:
        target = profile_dir / relative
        if not target.is_dir():
            continue
        resolved = target.resolve()
        if profile_dir not in resolved.parents:
            raise ValueError("缓存目录超出账号浏览器目录，已跳过清理")
        freed_bytes += sum(
            item.stat().st_size for item in target.rglob("*") if item.is_file()
        )
        shutil.rmtree(target)
        removed_directories += 1
    return freed_bytes, removed_directories


def _tree_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        return path.stat().st_size
    return sum(
        item.stat().st_size
        for item in path.rglob("*")
        if item.is_file() and not item.is_symlink()
    )


def clear_data_directory(path: Path) -> dict:
    """Empty a known data subdirectory while keeping the directory itself."""
    target = path.resolve()
    data_root = DATA_DIR.resolve()
    if target == data_root or data_root not in target.parents:
        raise ValueError("清理目录不在 data 目录内，已跳过")

    result = {
        "freed_bytes": 0,
        "removed_files": 0,
        "removed_directories": 0,
        "skipped": [],
    }
    if not target.is_dir():
        return result

    for item in list(target.iterdir()):
        try:
            size = _tree_size(item)
            is_directory = item.is_dir() and not item.is_symlink()
            if is_directory:
                shutil.rmtree(item)
            else:
                item.unlink()
        except OSError as exc:
            result["skipped"].append({
                "path": str(item.relative_to(data_root)),
                "reason": str(exc),
            })
            continue
        result["freed_bytes"] += size
        if is_directory:
            result["removed_directories"] += 1
        else:
            result["removed_files"] += 1
    return result



async def open_account_browser(account_id: str) -> BrowserContext:
    global playwright_runtime
    if playwright_runtime is None:
        raise HTTPException(status_code=503, detail="浏览器自动化服务尚未启动")
    lock = context_locks.setdefault(account_id, asyncio.Lock())
    async with lock:
        existing = open_contexts.get(account_id)
        if existing is not None:
            # Closing the Chrome window leaves a context whose page list is simply empty,
            # so testing `existing.pages` alone would hand back a dead browser and every
            # later job would fail instantly on new_page(). Probe the browser instead.
            browser = existing.browser
            try:
                if browser is not None and not browser.is_connected():
                    raise RuntimeError("浏览器进程已断开")
                if existing.pages:
                    page = existing.pages[0]
                    await page.bring_to_front()
                    if "doubao.com" not in (page.url or ""):
                        await page.goto(DOUBAO_URL, wait_until="domcontentloaded", timeout=45000)
                else:
                    page = await existing.new_page()
                    await page.goto(DOUBAO_URL, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                try:
                    await existing.close()
                except Exception:
                    pass
                open_contexts.pop(account_id, None)
            else:
                return existing

        row = get_account(account_id)
        sequence = account_sequence(account_id)
        profile_dir = portable_path(row["profile_dir"])
        download_dir = (DOWNLOADS_DIR / account_id).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        download_dir.mkdir(parents=True, exist_ok=True)
        try:
            project_browser = resolve_project_chrome(BASE_DIR)
            browser_options = (
                {"executable_path": str(project_browser)}
                if project_browser is not None
                else {"channel": "chrome"}
            )
            context = await playwright_runtime.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                accept_downloads=True,
                downloads_path=str(download_dir),
                no_viewport=True,
                locale="zh-CN",
                args=["--start-maximized", *extension_launch_args(BASE_DIR)],
                **browser_options,
            )
            title_script = account_title_script(sequence)
            await context.add_init_script(title_script)
            for existing_page in context.pages:
                try:
                    await existing_page.evaluate(title_script)
                except Exception:
                    pass
        except Exception as exc:
            detail = str(exc)
            if "Executable doesn't exist" in detail or "chrome" in detail.lower():
                raise HTTPException(
                    status_code=500,
                    detail="未能启动浏览器。便携版请确认 browser 目录完整；源码版请安装 Google Chrome。",
                ) from exc
            raise HTTPException(status_code=500, detail=f"启动浏览器失败：{detail[:500]}") from exc

        open_contexts[account_id] = context
        # A user closing the window must not leave a dead context cached for the account.
        context.on("close", lambda *_: open_contexts.pop(account_id, None))
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(DOUBAO_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            # 页面网络异常不销毁浏览器，允许用户在窗口中自行处理。
            pass
        if sequence:
            try:
                await asyncio.to_thread(
                    apply_account_taskbar_identity,
                    sequence,
                    account_title_prefix(sequence),
                    TASKBAR_ICONS_DIR,
                )
            except Exception:
                # Taskbar decoration is best-effort and must not block browser use.
                pass
        with db() as conn:
            conn.execute("UPDATE accounts SET last_opened_at = ? WHERE id = ?", (now_iso(), account_id))
        return context


async def close_account_browser_context(account_id: str) -> None:
    """Close and evict one account's persistent Chrome context."""
    lock = context_locks.setdefault(account_id, asyncio.Lock())
    async with lock:
        context = open_contexts.pop(account_id, None)
        if context:
            try:
                await context.close()
            except Exception:
                pass


def existing_account_browser_context(account_id: str) -> BrowserContext | None:
    """Return an already-open account context without launching Chrome."""
    return open_contexts.get(account_id)


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "doubao-account-desk", "version": "0.3.0", "time": now_iso()}


@app.get("/api/stats")
async def stats():
    with db() as conn:
        accounts_total = conn.execute("SELECT COUNT(*) FROM accounts WHERE archived = 0").fetchone()[0]
        accounts_open = len(open_contexts)
        jobs_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        counts = {
            row["status"]: row["count"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            )
        }
        unassigned_pending = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','ready') AND account_id IS NULL"
        ).fetchone()[0]
        # 同为待处理：已放行在等名额/账号，未放行在等人工点“进入调度”。
        dispatched_pending = conn.execute(
            "SELECT COUNT(*) FROM jobs j JOIN video_web_jobs w ON j.id = w.job_id "
            "WHERE j.status IN ('queued','ready') AND COALESCE(w.dispatched_at, '') != ''"
        ).fetchone()[0]
        archived_ids = {
            row["job_id"]
            for row in conn.execute(
                "SELECT w.job_id FROM video_web_jobs w JOIN jobs j ON j.id = w.job_id "
                "WHERE w.archived_at != '' AND w.archived_at IS NOT NULL"
            )
        }
        downloaded_ids = {
            row["id"] for row in conn.execute("SELECT id FROM jobs WHERE status = 'downloaded'")
        }
    # 处理完 = 已下载校验但用户还没手动归档；已归档 = 用户已归档。
    archived_count = len(archived_ids & downloaded_ids)
    finished_count = len(downloaded_ids) - archived_count
    buckets = {
        "pending": (counts.get("queued", 0) + counts.get("ready", 0)),
        # 界面上“待人工”已并入“正在处理”之下，卡片与标签口径保持一致。
        "processing": (sum(counts.get(status, 0) for status in STATUS_BUCKETS["processing"])
                       + sum(counts.get(status, 0) for status in STATUS_BUCKETS["attention"])),
        "processing_running": sum(counts.get(status, 0) for status in STATUS_BUCKETS["processing"]),
        "attention": sum(counts.get(status, 0) for status in STATUS_BUCKETS["attention"]),
        "finished": finished_count,
        "failed": counts.get("failed", 0),
        "archived": archived_count,
        "pending_unassigned": unassigned_pending,
        "pending_dispatched": dispatched_pending,
        "pending_undispatched": (counts.get("queued", 0) + counts.get("ready", 0)) - dispatched_pending,
    }
    return {
        "accounts_total": accounts_total,
        "accounts_open": accounts_open,
        "jobs_total": jobs_total,
        # Bucket counts used by the overview cards and job tabs.
        **buckets,
        # Legacy keys kept for API compatibility.
        "queued": counts.get("queued", 0),
        "generating": counts.get("generating", 0),
        "downloaded": counts.get("downloaded", 0),
    }


@app.get("/api/accounts")
async def list_accounts(include_archived: bool = False):
    query = "SELECT * FROM accounts" + ("" if include_archived else " WHERE archived = 0") + " ORDER BY priority DESC, created_at DESC"
    with db() as conn:
        rows = conn.execute(query).fetchall()
        return [
            {**account_row(row), "sequence": index, "quota": quota_summary(conn, row["id"])}
            for index, row in enumerate(rows, 1)
        ]


@app.get("/api/accounts/{account_id}/quota")
async def get_account_quota(account_id: str):
    get_account(account_id)
    with db() as conn:
        return quota_summary(conn, account_id)


@app.get("/api/accounts/suggested-name")
async def suggested_account_name():
    with db() as conn:
        return {"name": next_account_name(conn)}


@app.post("/api/accounts", status_code=201)
async def create_account(payload: AccountCreate):
    account_id = "acc_" + uuid.uuid4().hex[:10]
    stored_profile_dir, profile_dir = account_profile_path(account_id)
    with db() as conn:
        # A missing, null or blank name falls back to the doubao0N sequence.
        name = (payload.name or "").strip() or next_account_name(conn)
        conn.execute(
            "INSERT INTO accounts(id, name, note, profile_dir, is_member, priority, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, name, payload.note.strip(), stored_profile_dir, int(payload.is_member), payload.priority, now_iso()),
        )
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    profile_dir.mkdir(parents=True, exist_ok=True)
    return account_row(row)


@app.patch("/api/accounts/{account_id}")
async def update_account_settings(account_id: str, payload: AccountSettings):
    get_account(account_id)
    with db() as conn:
        conn.execute(
            "UPDATE accounts SET is_member = ?, priority = ? WHERE id = ?",
            (int(payload.is_member), payload.priority, account_id),
        )
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return account_row(row)


@app.post("/api/accounts/{account_id}/open")
async def open_account(account_id: str):
    get_account(account_id)
    await open_account_browser(account_id)
    return {"ok": True, "account_id": account_id, "browser_open": True, "message": "浏览器已打开；请在窗口内手动登录并完成豆包页面操作。"}


@app.post("/api/accounts/{account_id}/pause")
async def pause_account(account_id: str):
    get_account(account_id)
    with db() as conn:
        conn.execute("UPDATE accounts SET paused = 1 WHERE id = ?", (account_id,))
    return {"ok": True, "account_id": account_id, "paused": True}


@app.post("/api/accounts/{account_id}/resume")
async def resume_account(account_id: str):
    get_account(account_id)
    with db() as conn:
        conn.execute("UPDATE accounts SET paused = 0 WHERE id = ?", (account_id,))
    return {"ok": True, "account_id": account_id, "paused": False}


def _clear_account_caches() -> dict:
    with db() as conn:
        accounts = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()

    freed_bytes = 0
    removed_directories = 0
    cleaned_accounts = 0
    skipped = []
    for row in accounts:
        account_id = row["id"]
        if account_id in open_contexts or account_busy(account_id):
            skipped.append({"id": account_id, "name": row["name"], "reason": "浏览器窗口已打开或任务正在执行"})
            continue
        try:
            account_bytes, account_directories = clear_profile_cache(portable_path(row["profile_dir"]))
        except (OSError, ValueError) as exc:
            skipped.append({"id": account_id, "name": row["name"], "reason": str(exc)})
            continue
        freed_bytes += account_bytes
        removed_directories += account_directories
        if account_directories:
            cleaned_accounts += 1

    return {
        "ok": True,
        "freed_bytes": freed_bytes,
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
        "removed_files": 0,
        "removed_directories": removed_directories,
        "cleaned_accounts": cleaned_accounts,
        "skipped": skipped,
    }


@app.post("/api/accounts/clear-cache")
async def clear_account_caches():
    return _clear_account_caches()


@app.post("/api/cleanup/safe")
async def safe_cleanup():
    """Clear only disposable runtime files; account profiles and user outputs stay intact."""
    browser_cache = _clear_account_caches()
    areas = {"browser_cache": browser_cache}
    for name, path in (
        ("trash", TRASH_DIR),
        ("acceptance_reports", ACCEPTANCE_REPORTS_DIR),
    ):
        try:
            area = clear_data_directory(path)
        except (OSError, ValueError) as exc:
            area = {
                "freed_bytes": 0,
                "removed_files": 0,
                "removed_directories": 0,
                "skipped": [{"path": name, "reason": str(exc)}],
            }
        areas[name] = area

    freed_bytes = sum(area["freed_bytes"] for area in areas.values())
    return {
        "ok": True,
        "freed_bytes": freed_bytes,
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
        "removed_files": sum(area["removed_files"] for area in areas.values()),
        "removed_directories": sum(
            area["removed_directories"] for area in areas.values()
        ),
        "cleaned_accounts": browser_cache["cleaned_accounts"],
        "skipped": browser_cache["skipped"],
        "areas": areas,
    }


@app.post("/api/accounts/{account_id}/close")
async def close_account(account_id: str):
    get_account(account_id)
    if account_busy(account_id):
        raise HTTPException(status_code=409, detail="该账号仍在执行网页视频任务，暂不能关闭浏览器")
    await close_account_browser_context(account_id)
    return {"ok": True, "account_id": account_id, "browser_open": False}


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str, purge_data: bool = False):
    """Remove a profile for good. Login state moves to data/trash/ unless purge_data=true.

    Job rows and archived videos always survive; they simply lose the account link.
    """
    row = get_account(account_id)
    if account_id in open_contexts:
        raise HTTPException(status_code=409, detail="请先关闭该账号的浏览器窗口，再删除账号档案")
    if account_busy(account_id):
        raise HTTPException(status_code=409, detail="该账号仍在执行网页视频任务，暂不能删除")

    targets: list[tuple[str, Path]] = []
    profile_dir = portable_path(row["profile_dir"])
    # Migrated V2 profiles may live outside data/profiles: keep those directories untouched.
    if profile_dir.exists() and PROFILES_DIR.resolve() in profile_dir.parents:
        targets.append(("profiles", profile_dir))
    download_dir = (DOWNLOADS_DIR / account_id).resolve()
    # Containment guard: only ever remove a folder that sits inside data/videos.
    if download_dir.exists() and DOWNLOADS_DIR.resolve() in download_dir.parents:
        targets.append(("videos", download_dir))

    moved_to: Optional[str] = None
    moved: list[tuple[Path, Path]] = []
    try:
        if purge_data:
            for _, path in targets:
                shutil.rmtree(path)
        elif targets:
            moved_to = f"{datetime.now(timezone.utc).astimezone().strftime('%Y%m%d-%H%M%S')}_{account_id}"
            trash_dir = TRASH_DIR / moved_to
            for role, path in targets:
                # Keep the two directories apart: profile and downloads share the account id name.
                landing = trash_dir / role / path.name
                landing.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(landing))
                moved.append((landing, path))
    except OSError as exc:
        # Chrome still holds the profile: roll the move back and keep the account row.
        for landing, original in moved:
            if landing.exists():
                shutil.move(str(landing), str(original))
        if moved_to:
            # Best-effort tidy-up: rmdir only succeeds on the folders the rollback emptied.
            trash_dir = TRASH_DIR / moved_to
            for candidate in [trash_dir / role for role, _ in targets] + [trash_dir]:
                try:
                    candidate.rmdir()
                except OSError:
                    pass
        reason = getattr(exc, "strerror", None) or str(exc)
        raise HTTPException(
            status_code=409,
            detail=f"账号资料目录仍被占用，请先完全关闭 Chrome 后重试：{reason}",
        ) from exc

    with db() as conn:
        unlinked = conn.execute("SELECT COUNT(*) FROM jobs WHERE account_id = ?", (account_id,)).fetchone()[0]
        # jobs.account_id is ON DELETE SET NULL: task history and videos are kept.
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    context_locks.pop(account_id, None)
    return {
        "ok": True,
        "account_id": account_id,
        "deleted": True,
        "data_purged": bool(purge_data),
        "moved_to": (f"data/trash/{moved_to}" if moved_to else None),
        "unlinked_jobs": unlinked,
    }


@app.post("/api/accounts/{account_id}/archive")
async def archive_account(account_id: str):
    get_account(account_id)
    if account_id in open_contexts:
        raise HTTPException(status_code=409, detail="请先关闭该账号的浏览器窗口，再归档账号")
    with db() as conn:
        conn.execute("UPDATE accounts SET archived = 1 WHERE id = ?", (account_id,))
    return {"ok": True, "account_id": account_id, "archived": True}


@app.post("/api/accounts/{account_id}/restore")
async def restore_account(account_id: str):
    get_account(account_id)
    with db() as conn:
        conn.execute("UPDATE accounts SET archived = 0 WHERE id = ?", (account_id,))
    return {"ok": True, "account_id": account_id, "archived": False}


@app.get("/api/jobs")
async def list_jobs():
    with db() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        web_rows = (conn.execute(
            "SELECT job_id, web_page_url, session_url, session_id, result_card_index, "
            "quota_state, task_name, submit_mode, duration, mode, ratio, model, "
            "error_code, recall_state, result_push_url, result_pushed_at, "
            "result_push_error, archived_at, dispatched_at, operator_takeover "
            "FROM video_web_jobs").fetchall()
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_web_jobs'").fetchone()
                    else [])
    web_meta = {r["job_id"]: {
        "web_page_url": r["web_page_url"] or "",
        "session_url": r["session_url"] or "",
        "session_id": r["session_id"] or "",
        "result_card_index": r["result_card_index"],
        "quota_state": r["quota_state"] or "",
        "task_name": r["task_name"] or "",
        "submit_mode": r["submit_mode"] or "auto",
        "duration": r["duration"],
        "mode": r["mode"] or "text2video",
        "ratio": r["ratio"] or "16:9",
        "model": r["model"] or "",
        "error_code": r["error_code"] or "",
        "recall_state": r["recall_state"] or "",
        "can_push_result": bool(r["result_push_url"]),
        "result_pushed_at": r["result_pushed_at"] or "",
        "result_push_error": r["result_push_error"] or "",
        "archived_at": r["archived_at"] or "",
        "dispatched_at": r["dispatched_at"] or "",
        "operator_takeover": bool(r["operator_takeover"]),
    } for r in web_rows}
    prepared_pages = getattr(app.state, "video_prepared_pages", {})
    out = []
    for row in rows:
        meta = web_meta.get(row["id"])
        page = ((meta["session_url"] or meta["web_page_url"]) if meta else "")
        quota_state = meta["quota_state"] if meta else ""
        live_page = prepared_pages.get(row["id"])
        try:
            has_live_page = live_page is not None and not live_page.is_closed()
        except Exception:
            has_live_page = False
        item = {**job_row(row),
                    "task_name": meta["task_name"] if meta else "",
                    "is_web_generation": bool(meta),
                    "submit_mode": meta["submit_mode"] if meta else "auto",
                    "web_page_url": page,
                    "session_url": meta["session_url"] if meta else "",
                    "session_id": meta["session_id"] if meta else "",
                    "result_card_index": meta["result_card_index"] if meta else None,
                    "has_task_page": bool(has_live_page or (page and CHAT_PAGE_URL.search(page))),
                    "duration": meta["duration"] if meta else None,
                    "mode": meta["mode"] if meta else "",
                    "ratio": meta["ratio"] if meta else "",
                    "model": meta["model"] if meta else "",
                    "error_code": meta["error_code"] if meta else "",
                    "recall_state": meta["recall_state"] if meta else "",
                    "can_push_result": meta["can_push_result"] if meta else False,
                    "result_pushed_at": meta["result_pushed_at"] if meta else "",
                    "result_push_error": meta["result_push_error"] if meta else "",
                    "archived_at": meta["archived_at"] if meta else "",
                    # 非空 = 已交给调度器（等名额/账号）；空 = 还没人点“进入调度”。
                    "dispatched_at": meta["dispatched_at"] if meta else "",
                    "operator_takeover": meta["operator_takeover"] if meta else False,
                    # 用户手动归档后才算“已归档”；否则已下载完成的任务留在“处理完”。
                    "archived": bool(meta and meta["archived_at"]),
                    "manual_confirm_sent": bool(row["status"] == "needs_review" and quota_state in ("reserved", "unknown"))}
        out.append(item)
    return out


@app.post("/api/jobs", status_code=201)
async def create_job(payload: JobCreate):
    # 建单不要求账号：任务进入“待处理”，开始执行前才会分配账号。
    if payload.account_id:
        get_account(payload.account_id)
    job_id = "job_" + uuid.uuid4().hex[:12]
    timestamp = now_iso()
    with db() as conn:
        conn.execute(
            """INSERT INTO jobs(id, project_id, episode_id, shot_id, prompt, account_id, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
            (
                job_id,
                payload.project_id.strip(),
                payload.episode_id.strip(),
                payload.shot_id.strip(),
                payload.prompt.strip(),
                payload.account_id,
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return job_row(row)


@app.post("/api/jobs/bulk-delete")
async def bulk_delete_jobs(payload: JobBulkDelete):
    job_ids = list(dict.fromkeys(job_id.strip() for job_id in payload.job_ids if job_id.strip()))
    if not job_ids:
        raise HTTPException(status_code=422, detail="请选择要删除的任务")
    placeholders = ",".join("?" for _ in job_ids)
    with db() as conn:
        rows = conn.execute(f"SELECT * FROM jobs WHERE id IN ({placeholders})", job_ids).fetchall()
        found = {row["id"] for row in rows}
        missing = [job_id for job_id in job_ids if job_id not in found]
        if missing:
            raise HTTPException(status_code=404, detail=f"任务不存在：{', '.join(missing[:5])}")
        web_rows = conn.execute(
            f"SELECT * FROM video_web_jobs WHERE job_id IN ({placeholders})", job_ids
        ).fetchall()

    running = getattr(app.state, "video_pending", {})
    busy = [row["id"] for row in rows
            if row["status"] in JOB_DELETE_BLOCKED_STATUSES
            or (row["id"] in running and not running[row["id"]].done())]
    if busy:
        raise HTTPException(status_code=409, detail=f"进行中的任务不能删除：{', '.join(busy[:5])}")

    row_by_id = {row["id"]: row for row in rows}
    with db() as conn:
        for web in web_rows:
            state = web["quota_state"] or ""
            duration = int(web["duration"] or 0)
            account_id = row_by_id[web["job_id"]]["account_id"]
            if account_id and duration > 0 and state in ("", "reserved", "unknown"):
                day = web["quota_day"] or datetime.now().astimezone().strftime("%Y-%m-%d")
                conn.execute(
                    "INSERT OR IGNORE INTO account_quota(account_id,day,reserved_seconds,consumed_seconds) VALUES(?,?,0,0)",
                    (account_id, day),
                )
                # 未成功下载的任务删除时只释放预占，不因状态不确定而扣费。
                conn.execute(
                    "UPDATE account_quota SET reserved_seconds=MAX(0,reserved_seconds-?) WHERE account_id=? AND day=?",
                    (duration, account_id, day),
                )
        conn.execute(f"DELETE FROM video_web_events WHERE job_id IN ({placeholders})", job_ids)
        conn.execute(f"DELETE FROM video_web_jobs WHERE job_id IN ({placeholders})", job_ids)
        conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", job_ids)

    release_processing = getattr(app.state, "video_release_processing_job", None)
    if release_processing is not None:
        for job_id in job_ids:
            await release_processing(job_id)

    # A needs_review/manual-wait job keeps its account lock so its page stays
    # available for the operator. Deleting the job must release that lock too.
    release_account = getattr(app.state, "video_release_account_slot", None)
    if release_account is not None:
        for row in rows:
            if row["account_id"]:
                await release_account(row["id"], row["account_id"])

    prepared_pages = getattr(app.state, "video_prepared_pages", {})
    for job_id in job_ids:
        page = prepared_pages.pop(job_id, None)
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass

    deleted_files = 0
    file_errors = []
    base = BASE_DIR.resolve()
    for row in rows:
        if not row["video_path"]:
            continue
        source = portable_path(row["video_path"])
        if base not in source.parents or not source.is_file():
            continue
        try:
            source.unlink()
            deleted_files += 1
        except Exception as exc:
            file_errors.append(f"{row['id']}: {exc}")

    return {
        "ok": True,
        "deleted": len(job_ids),
        "job_ids": job_ids,
        "deleted_files": deleted_files,
        "file_errors": file_errors,
    }


@app.patch("/api/jobs/{job_id}")
async def update_job(job_id: str, payload: JobUpdate):
    row = get_job(job_id)
    updates: dict[str, object] = {}
    # Automated jobs are state-machine owned: the old manual PATCH cannot forge success,
    # change the executing account or reset a possibly submitted task to queued.
    with db() as conn:
        automated = conn.execute("SELECT 1 FROM video_web_jobs WHERE job_id = ?", (job_id,)).fetchone() if             conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_web_jobs'").fetchone() else None
    if automated and (payload.status is not None or "account_id" in payload.model_fields_set):
        raise HTTPException(409, detail="网页生成任务由专用状态机管理，不能人工修改状态或账号")
    if payload.status is not None:
        if payload.status not in VALID_STATUSES:
            raise HTTPException(status_code=422, detail=f"状态必须是：{', '.join(sorted(VALID_STATUSES))}")
        updates["status"] = payload.status
    if "account_id" in payload.model_fields_set:
        if payload.account_id:
            get_account(payload.account_id)
        updates["account_id"] = payload.account_id or None
    if payload.error_message is not None:
        updates["error_message"] = payload.error_message
    if not updates:
        return job_row(row)
    updates["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    values = list(updates.values()) + [job_id]
    with db() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)
        updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return job_row(updated)


@app.post("/api/jobs/{job_id}/prepare")
async def prepare_job(job_id: str):
    row = get_job(job_id)
    with db() as conn:
        automated = (conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_web_jobs'").fetchone()
                     and conn.execute("SELECT 1 FROM video_web_jobs WHERE job_id=?", (job_id,)).fetchone())
    if automated:
        raise HTTPException(status_code=409, detail="网页生成任务由专用状态机管理，请使用“多模式网页生成”或“复查归档”，不能执行人工准备")
    account_id = row["account_id"]
    if not account_id:
        # 待处理任务不分配账号：人工准备时同样是“进入处理前”才挑账号。
        assign = getattr(app.state, "video_assign_account_for", None)
        account_id = assign(row["id"]) if assign else None
    if not account_id:
        raise HTTPException(status_code=400, detail="请先为任务选择一个账号档案（或在开始前由系统自动分配）")
    account = get_account(account_id)
    if account["paused"]:
        raise HTTPException(status_code=409, detail="该账号已暂停使用，请先恢复账号")
    await open_account_browser(account_id)
    with db() as conn:
        conn.execute("UPDATE jobs SET status = 'ready', updated_at = ? WHERE id = ?", (now_iso(), job_id))
    return {
        "ok": True,
        "job_id": job_id,
        "prompt": row["prompt"],
        "message": "已打开对应账号的豆包页面。请手动粘贴提示词、确认平台要求并提交任务。",
    }


@app.post("/api/jobs/{job_id}/upload")
async def upload_job_video(job_id: str, file: UploadFile = File(...)):
    row = get_job(job_id)
    with db() as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_web_jobs'").fetchone() and            conn.execute("SELECT 1 FROM video_web_jobs WHERE job_id=?", (job_id,)).fetchone():
            raise HTTPException(409, detail="网页生成任务请通过生成适配器或结果复查归档，不能手工伪造成功")
    filename = Path(file.filename or "video.mp4").name
    safe_name = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", filename).strip("._") or "video.mp4"
    suffix = Path(safe_name).suffix.lower()
    allowed = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
    if suffix not in allowed:
        raise HTTPException(status_code=415, detail="只支持 MP4、MOV、WEBM、MKV、AVI 视频文件")

    parts = [row["project_id"] or "未命名项目", row["episode_id"] or "未指定集数", row["shot_id"] or job_id]
    safe_parts = [re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", str(part)).strip("._") or "未命名" for part in parts]
    target_dir = DOWNLOADS_DIR / "library" / safe_parts[0] / safe_parts[1] / safe_parts[2]
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name
    if target_path.exists():
        target_path = target_dir / f"{target_path.stem}_{uuid.uuid4().hex[:6]}{target_path.suffix}"

    total = 0
    max_bytes = 2 * 1024 * 1024 * 1024
    try:
        with target_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    out.close()
                    target_path.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="单个视频文件不能超过 2 GB")
                out.write(chunk)
    finally:
        await file.close()

    relative_path = stored_data_path(target_path)
    with db() as conn:
        conn.execute(
            "UPDATE jobs SET video_path = ?, status = 'downloaded', error_message = '', updated_at = ? WHERE id = ?",
            (relative_path, now_iso(), job_id),
        )
        updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return {"ok": True, "job": job_row(updated), "size_bytes": total}


@app.get("/api/jobs/{job_id}/video")
async def get_job_video(job_id: str):
    row = get_job(job_id)
    if not row["video_path"]:
        raise HTTPException(status_code=404, detail="该任务还没有归档视频")
    path = portable_path(row["video_path"])
    if BASE_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="视频文件不存在")
    return FileResponse(path, filename=path.name)


@app.get("/api/files")
async def list_files():
    files = []
    with db() as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE video_path IS NOT NULL ORDER BY updated_at DESC").fetchall()
    for row in rows:
        path = portable_path(row["video_path"])
        files.append({
            "job_id": row["id"],
            "project_id": row["project_id"],
            "episode_id": row["episode_id"],
            "shot_id": row["shot_id"],
            "filename": path.name,
            "path": row["video_path"],
            "exists": path.is_file(),
            "updated_at": row["updated_at"],
        })
    return files


@app.get("/api/integration")
async def integration_info():
    return {
        "base_url": "http://127.0.0.1:8765",
        "docs_url": "http://127.0.0.1:8765/docs",
        "endpoints": [
            {"method": "GET", "path": "/api/accounts", "purpose": "获取账号档案"},
            {"method": "GET", "path": "/api/accounts/suggested-name", "purpose": "获取下一个默认账号名（doubao01、doubao02…）"},
            {"method": "POST", "path": "/api/accounts", "purpose": "创建账号档案，名称留空则使用默认命名"},
            {"method": "POST", "path": "/api/accounts/{id}/open", "purpose": "打开独立浏览器窗口"},
            {"method": "POST", "path": "/api/accounts/{id}/pause", "purpose": "暂停账号用于新任务"},
            {"method": "POST", "path": "/api/accounts/{id}/resume", "purpose": "恢复账号用于新任务"},
            {"method": "DELETE", "path": "/api/accounts/{id}", "purpose": "删除账号档案，登录资料默认移入 data/trash（purge_data=true 彻底删除）"},
            {"method": "GET", "path": "/api/jobs", "purpose": "获取分镜任务"},
            {"method": "POST", "path": "/api/jobs", "purpose": "创建分镜任务"},
            {"method": "POST", "path": "/api/jobs/{id}/prepare", "purpose": "打开账号页面并返回提示词，等待人工提交"},
            {"method": "POST", "path": "/api/jobs/{id}/upload", "purpose": "上传并归档生成的视频"},
            {"method": "GET", "path": "/api/jobs/{id}/video", "purpose": "获取归档视频"},
            {"method": "POST", "path": "/api/v1/videos/generations/{id}/push-result", "purpose": "把归档视频主动推送回 H3"},
        ],
        "security_note": "服务默认仅绑定 127.0.0.1，不建议暴露到公网或局域网。",
    }


# Web generation API: isolated from legacy account/jobs endpoints.
from web_api import CHAT_PAGE_URL, mount_video_api, quota_summary
mount_video_api(app, db=db, get_account=get_account, get_job=get_job,
    browser=open_account_browser, base_dir=BASE_DIR,
    downloads_dir=DOWNLOADS_DIR, serialize=job_row,
    close_browser=close_account_browser_context,
    existing_browser=existing_account_browser_context)

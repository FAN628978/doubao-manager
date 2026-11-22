from __future__ import annotations

import os
import re

from .config import CONFIRM_ANSWER
from .utils import ts


DEFAULT_PROCESSING_LIMIT = 3


def default_processing_limit() -> int:
    raw = os.getenv("DOUBAO_MAX_CONCURRENT_ACCOUNTS", str(DEFAULT_PROCESSING_LIMIT))
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_PROCESSING_LIMIT


def ensure_schema(db) -> None:
    """Create and migrate all web-generation tables in one place."""
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS video_web_jobs (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id),
                ratio TEXT,
                duration INTEGER,
                idempotency_key TEXT UNIQUE,
                video_url TEXT DEFAULT '',
                web_page_url TEXT DEFAULT '',
                session_url TEXT DEFAULT '',
                session_id TEXT DEFAULT '',
                error_code TEXT DEFAULT '',
                mode TEXT DEFAULT 'text2video',
                assets_json TEXT DEFAULT '{}',
                request_fingerprint TEXT DEFAULT '',
                result_source TEXT DEFAULT '',
                model TEXT DEFAULT '',
                quota_day TEXT DEFAULT '',
                quota_state TEXT DEFAULT '',
                task_name TEXT DEFAULT '',
                result_card_index INTEGER,
                recall_state TEXT DEFAULT '',
                recall_token TEXT DEFAULT '',
                recall_started_at TEXT DEFAULT '',
                retry_of_job_id TEXT DEFAULT '',
                submit_mode TEXT DEFAULT 'auto',
                result_push_url TEXT DEFAULT '',
                result_push_token TEXT DEFAULT '',
                result_pushed_at TEXT DEFAULT '',
                result_push_error TEXT DEFAULT '',
                phase TEXT DEFAULT '',
                phase_started_at TEXT DEFAULT '',
                last_heartbeat_at TEXT DEFAULT '',
                attempt INTEGER DEFAULT 0,
                next_retry_at TEXT DEFAULT '',
                error_class TEXT DEFAULT '',
                delivery_state TEXT DEFAULT '',
                delivery_attempts INTEGER DEFAULT 0,
                delivery_next_retry_at TEXT DEFAULT '',
                delivery_error TEXT DEFAULT '',
                archived_at TEXT DEFAULT '',
                avoid_account_id TEXT DEFAULT '',
                bypass_processing_limit INTEGER DEFAULT 0,
                extension_binding_token TEXT DEFAULT '',
                operator_takeover INTEGER DEFAULT 0,
                takeover_status TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS video_web_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                status TEXT,
                message TEXT,
                timestamp TEXT
            );
            CREATE TABLE IF NOT EXISTS video_web_assets (
                id TEXT PRIMARY KEY,
                sha256 TEXT,
                original_name TEXT,
                stored_path TEXT,
                mime TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS account_quota (
                account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                day TEXT NOT NULL,
                reserved_seconds INTEGER NOT NULL DEFAULT 0,
                consumed_seconds INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (account_id, day)
            );
            CREATE TABLE IF NOT EXISTS confirmation_presets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS local_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO local_settings(key,value)
            VALUES('video_submit_mode','manual');
            CREATE INDEX IF NOT EXISTS idx_account_quota_day
            ON account_quota(day);
            """
        )

        stamp = ts()
        c.execute(
            """INSERT OR IGNORE INTO confirmation_presets
               (id,name,text,created_at,updated_at) VALUES(?,?,?,?,?)""",
            ("preset_default", "默认确认语", CONFIRM_ANSWER, stamp, stamp),
        )

        columns = {
            row["name"]
            for row in c.execute("PRAGMA table_info(video_web_jobs)")
        }
        migrations = (
            ("mode", "TEXT DEFAULT 'text2video'"),
            ("assets_json", "TEXT DEFAULT '{}'"),
            ("request_fingerprint", "TEXT DEFAULT ''"),
            ("result_source", "TEXT DEFAULT ''"),
            ("model", "TEXT DEFAULT ''"),
            ("quota_day", "TEXT DEFAULT ''"),
            ("quota_state", "TEXT DEFAULT ''"),
            ("task_name", "TEXT DEFAULT ''"),
            # Keep the original web_page_url field for existing callers. These two
            # fields make the persisted conversation binding explicit for window
            # reopening, while result_card_index remains the video-card binding.
            ("session_url", "TEXT DEFAULT ''"),
            ("session_id", "TEXT DEFAULT ''"),
            ("result_card_index", "INTEGER"),
            ("recall_state", "TEXT DEFAULT ''"),
            ("recall_token", "TEXT DEFAULT ''"),
            ("recall_started_at", "TEXT DEFAULT ''"),
            ("retry_of_job_id", "TEXT DEFAULT ''"),
            ("submit_mode", "TEXT DEFAULT 'auto'"),
            ("result_push_url", "TEXT DEFAULT ''"),
            ("result_push_token", "TEXT DEFAULT ''"),
            ("result_pushed_at", "TEXT DEFAULT ''"),
            ("result_push_error", "TEXT DEFAULT ''"),
            ("phase", "TEXT DEFAULT ''"),
            ("phase_started_at", "TEXT DEFAULT ''"),
            ("last_heartbeat_at", "TEXT DEFAULT ''"),
            ("attempt", "INTEGER DEFAULT 0"),
            ("next_retry_at", "TEXT DEFAULT ''"),
            ("error_class", "TEXT DEFAULT ''"),
            ("delivery_state", "TEXT DEFAULT ''"),
            ("delivery_attempts", "INTEGER DEFAULT 0"),
            ("delivery_next_retry_at", "TEXT DEFAULT ''"),
            ("delivery_error", "TEXT DEFAULT ''"),
            # 归档是用户手动动作：downloaded 只表示“处理完”，archived_at 非空才进入“已归档”。
            ("archived_at", "TEXT DEFAULT ''"),
            # 复制/重试产生的任务在分配账号时避开这里记录的账号（原任务用过的账号）。
            ("avoid_account_id", "TEXT DEFAULT ''"),
            # 交给调度器的时刻：排队任务在拿到名额前状态仍是 queued，靠它区分
            # “还没人工放行”和“已放行、正在等名额/账号”（见 start_worker）。
            ("dispatched_at", "TEXT DEFAULT ''"),
            # 显式撤回放行后，旧版自动任务恢复逻辑也必须等待再次放行。
            ("dispatch_revoked", "INTEGER DEFAULT 0"),
            # 手动点击“下一个任务”可超出全局处理上限，重派时保留此授权。
            ("bypass_processing_limit", "INTEGER DEFAULT 0"),
            # Opaque identity injected into the exact task tab for the Chrome extension.
            ("extension_binding_token", "TEXT DEFAULT ''"),
            ("operator_takeover", "INTEGER DEFAULT 0"),
            ("takeover_status", "TEXT DEFAULT ''"),
        )
        for name, ddl in migrations:
            if name not in columns:
                c.execute(f"ALTER TABLE video_web_jobs ADD COLUMN {name} {ddl}")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_video_extension_binding "
            "ON video_web_jobs(extension_binding_token) "
            "WHERE extension_binding_token!=''"
        )

        # Backfill historical jobs. session_url is only populated from a recognizable
        # chat URL; unsupported generic pages are left untouched for explicit review.
        legacy_sessions = c.execute(
            "SELECT job_id,web_page_url,session_url,session_id FROM video_web_jobs "
            "WHERE web_page_url!='' OR session_url!=''"
        ).fetchall()
        for row in legacy_sessions:
            url = (row["session_url"] or row["web_page_url"] or "").strip()
            match = re.search(r"/chat/([0-9A-Za-z_-]+)(?:[/?#]|$)", url)
            if not match:
                continue
            session_url = row["session_url"] or row["web_page_url"]
            session_id = match.group(1)
            if session_url != row["session_url"] or session_id != row["session_id"]:
                c.execute(
                    "UPDATE video_web_jobs SET session_url=?,session_id=? WHERE job_id=?",
                    (session_url, session_id, row["job_id"]),
                )

        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_video_web_delivery "
            "ON video_web_jobs(delivery_state, delivery_next_retry_at)"
        )

        # One-time migration from the old "unlimited by default" setting. An explicit
        # environment value still wins; zero remains the documented unlimited value.
        marker = c.execute(
            "SELECT 1 FROM local_settings WHERE key='processing_limit_v2'"
        ).fetchone()
        if not marker:
            limit_row = c.execute(
                "SELECT value FROM local_settings WHERE key='processing_limit'"
            ).fetchone()
            configured = os.getenv("DOUBAO_MAX_CONCURRENT_ACCOUNTS")
            if limit_row is None:
                limit = default_processing_limit()
            elif configured is not None:
                limit = default_processing_limit()
            elif (limit_row["value"] or "").strip() == "0":
                limit = DEFAULT_PROCESSING_LIMIT
            else:
                try:
                    limit = max(0, int(limit_row["value"]))
                except (TypeError, ValueError):
                    limit = DEFAULT_PROCESSING_LIMIT
            c.execute(
                "INSERT INTO local_settings(key,value) VALUES('processing_limit',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(limit),),
            )
            c.execute(
                "INSERT INTO local_settings(key,value) VALUES('processing_limit_v2','1')"
            )

        # Delivery is an outbox concern. Older releases incorrectly pushed a completed
        # download back to needs_review when only the H3 callback failed.
        c.execute(
            """UPDATE video_web_jobs
               SET delivery_state=CASE
                   WHEN result_pushed_at!='' THEN 'delivered'
                   WHEN result_push_error!='' THEN 'failed'
                   WHEN result_push_url!='' AND submit_mode='auto' THEN 'pending'
                   ELSE 'not_requested' END,
                   delivery_attempts=CASE
                   WHEN delivery_attempts=0 AND
                        (result_pushed_at!='' OR result_push_error!='') THEN 1
                   ELSE delivery_attempts END,
                   delivery_error=CASE
                   WHEN delivery_error='' THEN result_push_error
                   ELSE delivery_error END
               WHERE delivery_state=''"""
        )

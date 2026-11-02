from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    ACTIVE,
    DAILY_VIDEO_SECONDS,
    MANUAL_WAIT,
    MEMBER_MODEL,
    NON_MEMBER_MODEL,
    TERMINAL,
)
from .utils import ts

def account_value(account, key: str, default=None):
    return account[key] if key in account.keys() else default


def enforce_account_model(
    req: "VideoRequest",
    account,
    member_model: str = MEMBER_MODEL,
    non_member_model: str = NON_MEMBER_MODEL,
) -> None:
    req.model = (
        member_model
        if bool(account_value(account, "is_member", 0))
        else non_member_model
    )


def local_day() -> str:
    """本地日历日，作为额度归零边界（每天 30 秒按本地自然日计算）。"""
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def quota_summary(c: sqlite3.Connection, account_id: str, day: str | None = None) -> dict:
    """读取某账号某天的额度记录：已消耗 / 排队占用 / 剩余 / 上限。"""
    day = day or local_day()
    row = c.execute(
        "SELECT reserved_seconds, consumed_seconds "
        "FROM account_quota WHERE account_id=? AND day=?",
        (account_id, day),
    ).fetchone() if c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_quota'"
    ).fetchone() else None
    reserved = row["reserved_seconds"] if row else 0
    consumed = row["consumed_seconds"] if row else 0
    unknown = 0
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_web_jobs'").fetchone():
        unknown_row = c.execute(
            """SELECT COALESCE(SUM(w.duration), 0) AS seconds
               FROM video_web_jobs w JOIN jobs j ON j.id=w.job_id
               WHERE j.account_id=? AND w.quota_day=? AND w.quota_state='unknown'""",
            (account_id, day)).fetchone()
        unknown = int(unknown_row["seconds"] or 0) if unknown_row else 0
    return {"account_id": account_id, "day": day,
            "daily_limit_seconds": DAILY_VIDEO_SECONDS,
            "consumed_seconds": consumed,
            "reserved_seconds": reserved,
            "unknown_seconds": unknown,
            "queued_reserved_seconds": max(0, reserved - unknown),
            "remaining_seconds": max(0, DAILY_VIDEO_SECONDS - consumed - reserved)}


def reserve_quota(c: sqlite3.Connection, account_id: str, seconds: int, day: str | None = None) -> None:
    """任务创建时占用额度，避免同一账号被排队任务超额分配。"""
    if not account_id or seconds <= 0:
        return
    day = day or local_day()
    c.execute("""INSERT INTO account_quota(account_id, day, reserved_seconds, consumed_seconds)
                 VALUES(?,?,?,0)
                 ON CONFLICT(account_id, day) DO UPDATE SET reserved_seconds = reserved_seconds + excluded.reserved_seconds""",
              (account_id, day, seconds))


def quota_held_today(w) -> bool:
    """该任务今天是否已占用额度。

    新任务创建时 quota_state='reserved'；早期版本的任务 state 为空但 quota_day
    已写入且已调用 reserve_quota，同样视为已占用，避免重复占用。
    """
    state = (w["quota_state"] or "") if w else ""
    return (w["quota_day"] or "") == local_day() and state in ("", "reserved", "unknown", "consumed")


def settle_quota(c: sqlite3.Connection, jobid: str, new_status: str,
                 prior_status: str = "", error_code: str = "") -> None:
    """任务进入终态时结算额度：
    - downloaded：把排队占用转成已消耗（确实出了片、确实花了额度）。
    - needs_review：若任务可能已提交/确认，则转为 unknown 并继续占用额度；明确未提交才释放。
    - failed：网页已明确失败，释放排队占用。
    人工复查（reconcile / manual-result）会把 needs_review 重新推到 downloaded，此时再转为已消耗。
    """
    if new_status not in TERMINAL:
        return
    w = c.execute("SELECT duration, quota_day, quota_state FROM video_web_jobs WHERE job_id=?", (jobid,)).fetchone()
    if not w:
        return
    dur = w["duration"] or 0
    j = c.execute("SELECT account_id FROM jobs WHERE id=?", (jobid,)).fetchone()
    account_id = j["account_id"] if j else None
    if not account_id or dur <= 0:
        return
    day = w["quota_day"] or local_day()
    c.execute("INSERT OR IGNORE INTO account_quota(account_id, day, reserved_seconds, consumed_seconds) VALUES(?,?,0,0)",
              (account_id, day))
    state = w["quota_state"] or ""
    if new_status == "downloaded":
        if state != "consumed":
            if state == "released":
                c.execute("UPDATE account_quota SET consumed_seconds = consumed_seconds + ? WHERE account_id=? AND day=?",
                          (dur, account_id, day))
            else:
                c.execute("""UPDATE account_quota
                             SET reserved_seconds = MAX(0, reserved_seconds - ?),
                                 consumed_seconds = consumed_seconds + ?
                             WHERE account_id=? AND day=?""", (dur, dur, account_id, day))
            c.execute("UPDATE video_web_jobs SET quota_state='consumed' WHERE job_id=?", (jobid,))
    elif new_status == "needs_review":
        definitely_unspent = error_code in {
            "LOGIN_REQUIRED", "VERIFICATION_REQUIRED", "ACCOUNT_LIMIT", "SETTING_NOT_FOUND",
            "SETTING_UNSUPPORTED", "SETTING_UNVERIFIED", "MODEL_NOT_FOUND", "MODEL_UNVERIFIED",
            "UPLOAD_SLOT_NOT_FOUND", "UPLOAD_UNVERIFIED", "EDITOR_NOT_FOUND", "PROMPT_UNVERIFIED",
            "SUBMIT_NOT_FOUND", "CONFIRM_NOT_SENT", "CONFIRM_REQUIRED",
        }
        # 人工发送阶段重启时无法知道操作者是否已经在网页点过发送，按“可能已消耗”保留占用，
        # 等关联下载/人工归档成功后再转为已消耗。
        known_maybe_spent = error_code == "MANUAL_SEND_INTERRUPTED"
        uncertain = (not definitely_unspent and
                     (prior_status in ("submitting", "confirming", "generating", "downloading", MANUAL_WAIT) or
                      error_code in {"SUBMISSION_UNCONFIRMED", "WAIT_TIMEOUT",
                                     "INTERRUPTED_REVIEW_REQUIRED", "WORKER_EXCEPTION",
                                     "WEB_AUTOMATION_ERROR", "RESULT_URL_NOT_FOUND", "RESULT_NOT_READY"}) or
                     known_maybe_spent)
        if uncertain and state in ("", "reserved"):
            c.execute("UPDATE video_web_jobs SET quota_state='unknown' WHERE job_id=?", (jobid,))
        elif state in ("", "reserved", "unknown"):
            c.execute("UPDATE account_quota SET reserved_seconds = MAX(0, reserved_seconds - ?) WHERE account_id=? AND day=?",
                      (dur, account_id, day))
            c.execute("UPDATE video_web_jobs SET quota_state='released' WHERE job_id=?", (jobid,))
    elif new_status == "failed" and state in ("", "reserved", "unknown"):
        c.execute("UPDATE account_quota SET reserved_seconds = MAX(0, reserved_seconds - ?) WHERE account_id=? AND day=?",
                  (dur, account_id, day))
        c.execute("UPDATE video_web_jobs SET quota_state='released' WHERE job_id=?", (jobid,))


def local_day_from_iso(s: str) -> str:
    """把任务的 created_at（带时区 ISO）转成本地日历日，用于历史额度回填。"""
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except Exception:
        return local_day()


def backfill_existing_quota(c: sqlite3.Connection) -> None:
    """一次性回填历史任务：downloaded 视为当天已消耗；旧 queued/进行中任务不回溯占用。幂等。"""
    rows = c.execute("""SELECT j.account_id, j.created_at, w.job_id, w.duration, w.quota_state
                        FROM jobs j JOIN video_web_jobs w ON j.id = w.job_id
                        WHERE j.status = 'downloaded' AND w.quota_state = ''""").fetchall()
    for r in rows:
        if not r["account_id"] or (r["duration"] or 0) <= 0:
            continue
        day = local_day_from_iso(r["created_at"])
        c.execute("INSERT OR IGNORE INTO account_quota(account_id, day, reserved_seconds, consumed_seconds) VALUES(?,?,0,0)",
                  (r["account_id"], day))
        c.execute("UPDATE account_quota SET consumed_seconds = consumed_seconds + ? WHERE account_id=? AND day=?",
                  (r["duration"], r["account_id"], day))
        c.execute("UPDATE video_web_jobs SET quota_day=?, quota_state='consumed' WHERE job_id=?",
                  (day, r["job_id"]))


def recover_interrupted_jobs(db, base_dir=None):
    """Recover persisted work without ever resubmitting an uncertain generation."""
    from .schema import ensure_schema

    ensure_schema(db)
    base = Path(base_dir or os.getcwd())
    stamp = ts()
    with db() as c:
        unfinished = c.execute(
            "SELECT j.id,j.status,j.video_path,w.submit_mode,w.result_push_url,"
            "w.result_push_token,w.result_pushed_at,w.delivery_state "
            "FROM jobs j JOIN video_web_jobs w ON j.id=w.job_id "
            "WHERE j.status IN (" + ",".join("?" * len(ACTIVE)) + ")",
            ACTIVE,
        ).fetchall()
        for row in unfinished:
            raw_path = row["video_path"] or ""
            archived = Path(raw_path)
            if not archived.is_absolute():
                archived = base / archived
            has_archive = bool(raw_path) and archived.is_file() and archived.stat().st_size > 0
            if has_archive:
                c.execute(
                    "UPDATE jobs SET status='downloaded',error_message='',updated_at=? "
                    "WHERE id=?",
                    (stamp, row["id"]),
                )
                c.execute(
                    "UPDATE video_web_jobs SET error_code='',error_class='',"
                    "phase='downloaded',phase_started_at=?,last_heartbeat_at=?,"
                    "next_retry_at='' WHERE job_id=?",
                    (stamp, stamp, row["id"]),
                )
                if (row["submit_mode"] == "auto" and row["result_push_url"]
                        and row["result_push_token"] and not row["result_pushed_at"]
                        and row["delivery_state"] in ("", "not_requested")):
                    c.execute(
                        "UPDATE video_web_jobs SET delivery_state='pending',"
                        "delivery_next_retry_at='' WHERE job_id=?",
                        (row["id"],),
                    )
                c.execute(
                    "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                    "VALUES(?,?,?,?)",
                    (row["id"], "downloaded",
                     "重启恢复：归档文件有效，已恢复为已下载", stamp),
                )
                settle_quota(c, row["id"], "downloaded", prior_status=row["status"])
                continue

            c.execute(
                "UPDATE jobs SET status='needs_review',"
                "error_message='服务重启中断：需核对网页，禁止自动重复提交',"
                "updated_at=? WHERE id=?",
                (stamp, row["id"]),
            )
            c.execute(
                "UPDATE video_web_jobs SET error_code='INTERRUPTED_REVIEW_REQUIRED',"
                "error_class='interrupted',phase='needs_review',phase_started_at=?,"
                "last_heartbeat_at=?,next_retry_at='' WHERE job_id=?",
                (stamp, stamp, row["id"]),
            )
            c.execute(
                "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                "VALUES(?,?,?,?)",
                (row["id"], "needs_review",
                     "重启恢复：未找到有效归档，停止自动提交，等待人工复查", stamp),
            )
            settle_quota(c, row["id"], "needs_review", prior_status=row["status"],
                         error_code="INTERRUPTED_REVIEW_REQUIRED")

        waiting = c.execute(
            "SELECT id FROM jobs WHERE status=? "
            "AND id IN (SELECT job_id FROM video_web_jobs)",
            (MANUAL_WAIT,),
        ).fetchall()
        for row in waiting:
            c.execute(
                "UPDATE jobs SET status='needs_review',"
                "error_message='服务重启：任务停在人工发送阶段，请在豆包网页核对是否已发送',"
                "updated_at=? WHERE id=?",
                (stamp, row["id"]),
            )
            c.execute(
                "UPDATE video_web_jobs SET error_code='MANUAL_SEND_INTERRUPTED',"
                "error_class='interrupted',phase='needs_review',phase_started_at=?,"
                "last_heartbeat_at=? WHERE job_id=?",
                (stamp, stamp, row["id"]),
            )
            c.execute(
                "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                "VALUES(?,?,?,?)",
                (row["id"], "needs_review",
                     "重启恢复：人工发送阶段中断，等待人工核对", stamp),
            )
            settle_quota(c, row["id"], "needs_review", prior_status=MANUAL_WAIT,
                         error_code="MANUAL_SEND_INTERRUPTED")

        # A valid archived video plus a push-only failure is not a generation failure.
        push_only = c.execute(
            "SELECT j.id,j.video_path,w.result_push_error,w.delivery_error "
            "FROM jobs j JOIN video_web_jobs w ON j.id=w.job_id "
            "WHERE j.status='needs_review' AND w.error_code='RESULT_PUSH_FAILED' "
            "AND j.video_path IS NOT NULL AND j.video_path!=''"
        ).fetchall()
        for row in push_only:
            archived = Path(row["video_path"])
            if not archived.is_absolute():
                archived = base / archived
            if not archived.is_file() or archived.stat().st_size <= 0:
                continue
            c.execute(
                "UPDATE jobs SET status='downloaded',error_message='',updated_at=? "
                "WHERE id=?",
                (stamp, row["id"]),
            )
            c.execute(
                "UPDATE video_web_jobs SET error_code='',error_class='',"
                "phase='downloaded',phase_started_at=?,last_heartbeat_at=?,"
                "delivery_state='failed',delivery_error=? WHERE job_id=?",
                (stamp, stamp, row["delivery_error"] or row["result_push_error"],
                 row["id"]),
            )
            c.execute(
                "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                "VALUES(?,?,?,?)",
                (row["id"], "downloaded",
                     "启动修复：视频已归档，仅服务器推送失败，保留为已下载", stamp),
            )
            settle_quota(c, row["id"], "downloaded", prior_status="needs_review")

        stranded = c.execute(
            "SELECT id FROM jobs WHERE status='ready' "
            "AND id IN (SELECT job_id FROM video_web_jobs)"
        ).fetchall()
        for row in stranded:
            c.execute(
                "UPDATE jobs SET status='needs_review',updated_at=? WHERE id=?",
                (stamp, row["id"]),
            )
            c.execute(
                "UPDATE video_web_jobs SET phase='needs_review',"
                "phase_started_at=?,last_heartbeat_at=? WHERE job_id=?",
                (stamp, stamp, row["id"]),
            )
            c.execute(
                "INSERT INTO video_web_events(job_id,status,message,timestamp) "
                "VALUES(?,?,?,?)",
                (row["id"], "needs_review",
                     "启动修复：网页任务曾被人工准备覆盖状态，已恢复为需人工复查", stamp),
            )
            settle_quota(c, row["id"], "needs_review", prior_status="ready")
        backfill_existing_quota(c)

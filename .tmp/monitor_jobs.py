# -*- coding: utf-8 -*-
"""实时监控任务：新建任务、状态变化、视频事件。只读数据库。"""
import io, sys, time, sqlite3

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

DB = "data/manager.sqlite3"
con = sqlite3.connect(DB, timeout=5)
con.row_factory = sqlite3.Row
con.execute("PRAGMA busy_timeout=3000")

last_ev = con.execute("SELECT COALESCE(MAX(id),0) FROM video_web_events").fetchone()[0]
last_job_rowid = 0
try:
    last_job_rowid = con.execute("SELECT COALESCE(MAX(rowid),0) FROM jobs").fetchone()[0]
except Exception:
    pass
statuses = {}
for r in con.execute("SELECT id,status FROM jobs"):
    statuses[r["id"]] = r["status"]

print(f"[monitor] started, last_ev={last_ev}, last_job_rowid={last_job_rowid}, jobs={len(statuses)}")

while True:
    try:
        # 新任务
        rows = con.execute(
            "SELECT j.rowid,j.id,j.account_id,w.mode,w.ratio,w.duration,w.model,j.status,j.created_at "
            "FROM jobs j LEFT JOIN video_web_jobs w ON w.job_id=j.id WHERE j.rowid > ? ORDER BY j.rowid",
            (last_job_rowid,)).fetchall()
        for r in rows:
            last_job_rowid = r["rowid"]
            print(f"\n[NEW JOB] {r['id']} account={r['account_id']} mode={r['mode']} "
                  f"ratio={r['ratio']} dur={r['duration']} model={r['model']} at={r['created_at']}")
        # 新事件
        evs = con.execute("SELECT id,job_id,status,message,timestamp FROM video_web_events WHERE id > ? ORDER BY id",
                          (last_ev,)).fetchall()
        for e in evs:
            last_ev = e["id"]
            print(f"[EVENT] {e['timestamp']} {e['job_id']} {e['status']}: {e['message']}")
        # 状态变化
        for r in con.execute("SELECT id,status,error_message FROM jobs"):
            old = statuses.get(r["id"])
            if old != r["status"]:
                statuses[r["id"]] = r["status"]
                err = (r["error_message"] or "").replace("\n", " ")[:200]
                print(f"[STATUS] {r['id']}: {old} -> {r['status']}  err={err}")
    except Exception as exc:
        print(f"[monitor] poll error: {exc}")
    time.sleep(1.5)

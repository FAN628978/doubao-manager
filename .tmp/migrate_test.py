import sys
import sqlite3

sys.path.insert(0, r"C:\Users\fht\Desktop\doubao-manager-v3")
from web_api import recover_interrupted_jobs, quota_summary, DAILY_VIDEO_SECONDS

DB = r"C:\Users\fht\Desktop\doubao-manager-v3\data\manager.sqlite3"


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


recover_interrupted_jobs(db)
print("DAILY_VIDEO_SECONDS =", DAILY_VIDEO_SECONDS)
with db() as c:
    cols = [r["name"] for r in c.execute("PRAGMA table_info(video_web_jobs)")]
    print("video_web_jobs cols:", cols)
    for acc in c.execute("SELECT id, name FROM accounts ORDER BY created_at"):
        print(acc["name"], quota_summary(c, acc["id"]))

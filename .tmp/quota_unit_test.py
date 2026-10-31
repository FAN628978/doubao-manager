import sys
import sqlite3

sys.path.insert(0, r"C:\Users\fht\Desktop\doubao-manager-v3")
from web_api import reserve_quota, settle_quota, quota_summary, local_day

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys = ON")
conn.executescript("""
CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT, note TEXT DEFAULT '', profile_dir TEXT,
  archived INTEGER DEFAULT 0, created_at TEXT, last_opened_at TEXT);
CREATE TABLE jobs (id TEXT PRIMARY KEY, project_id TEXT DEFAULT '', episode_id TEXT DEFAULT '',
  shot_id TEXT DEFAULT '', prompt TEXT, account_id TEXT, status TEXT DEFAULT 'queued',
  video_path TEXT, error_message TEXT DEFAULT '', created_at TEXT, updated_at TEXT,
  FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL);
CREATE TABLE video_web_jobs (job_id TEXT PRIMARY KEY REFERENCES jobs(id), ratio TEXT, duration INTEGER,
  idempotency_key TEXT UNIQUE, video_url TEXT DEFAULT '', web_page_url TEXT DEFAULT '', error_code TEXT DEFAULT '',
  mode TEXT DEFAULT 'text2video', assets_json TEXT DEFAULT '{}', request_fingerprint TEXT DEFAULT '',
  result_source TEXT DEFAULT '', model TEXT DEFAULT '', quota_day TEXT DEFAULT '', quota_state TEXT DEFAULT '');
CREATE TABLE account_quota (account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  day TEXT NOT NULL, reserved_seconds INTEGER DEFAULT 0, consumed_seconds INTEGER DEFAULT 0,
  PRIMARY KEY (account_id, day));
""")

day = local_day()
acc = "acc_test"
conn.execute("INSERT INTO accounts(id,name,profile_dir,created_at) VALUES(?,?,?,?)", (acc, "t", "", "now"))
conn.execute("INSERT INTO jobs(id,prompt,account_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
             ("job1", "p", acc, "queued", "now", "now"))
conn.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,mode,quota_day) VALUES('job1','16:9',15,'text2video',?)", (day,))

# reserve
reserve_quota(conn, acc, 15, day)
print("after reserve:", quota_summary(conn, acc, day))

# settle downloaded -> consume
settle_quota(conn, "job1", "downloaded")
print("after downloaded:", quota_summary(conn, acc, day))

# another job, needs_review -> release
conn.execute("INSERT INTO jobs(id,prompt,account_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
             ("job2", "p", acc, "queued", "now", "now"))
conn.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,mode,quota_day) VALUES('job2','16:9',4,'text2video',?)", (day,))
reserve_quota(conn, acc, 4, day)
print("after reserve job2:", quota_summary(conn, acc, day))
settle_quota(conn, "job2", "needs_review")
print("after needs_review job2:", quota_summary(conn, acc, day))

# reconcile: needs_review -> downloaded (released -> consumed)
settle_quota(conn, "job2", "downloaded")
print("after reconcile job2 downloaded:", quota_summary(conn, acc, day))

# idempotency: downloaded again should not double count
settle_quota(conn, "job2", "downloaded")
print("after duplicate downloaded job2:", quota_summary(conn, acc, day))

import sqlite3, io, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
con = sqlite3.connect('data/manager.sqlite3')
con.row_factory = sqlite3.Row
cur = con.cursor()

print("== ACCOUNTS ==")
for r in cur.execute("SELECT id,name,archived,created_at,last_opened_at,profile_dir FROM accounts ORDER BY created_at"):
    print(dict(r))

print("\n== TODAY QUOTA (local day) ==")
from datetime import date
day = date.today().isoformat()
for r in cur.execute("SELECT * FROM account_quota WHERE day=?", (day,)):
    print(dict(r))

print("\n== STATUS COUNTS DAYS ==")
for r in cur.execute("SELECT substr(created_at,1,10) day, status, count(*) n FROM jobs GROUP BY day,status ORDER BY day DESC"):
    print(tuple(r))

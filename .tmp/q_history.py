import sqlite3, shutil, io, sys, os, time, datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

acc = "acc_ae35c364b1"
src = f"data/profiles/{acc}/Default/History"
tmp = f".tmp/history_{acc}.db"
for i in range(8):
    try:
        shutil.copy2(src, tmp); break
    except Exception as e:
        time.sleep(1)
else:
    print("cannot copy History"); sys.exit(1)

con = sqlite3.connect(tmp)
rows = con.execute("SELECT url, title, visit_count, last_visit_time FROM urls ORDER BY last_visit_time DESC LIMIT 40").fetchall()
# Chrome time: microseconds since 1601-01-01
def ct(v):
    if not v: return ""
    return (datetime.datetime(1601,1,1) + datetime.timedelta(microseconds=v)).astimezone().strftime("%H:%M:%S")
for url, title, vc, t in rows:
    print(ct(t), "|", (title or "")[:40], "|", url[:120])
con.close(); os.remove(tmp)

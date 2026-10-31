import sqlite3, shutil, io, sys, os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

for acc in ("acc_ae35c364b1", "acc_4f61660da1"):
    src = f"data/profiles/{acc}/Default/Network/Cookies"
    tmp = f".tmp/cookies_{acc}.db"
    try:
        shutil.copy2(src, tmp)
    except Exception as e:
        print(acc, "copy failed:", e)
        continue
    con = sqlite3.connect(tmp)
    rows = con.execute("SELECT host_key, name, expires_utc FROM cookies").fetchall()
    print(f"\n== {acc}: {len(rows)} cookies ==")
    hosts = {}
    for host, name, exp in rows:
        hosts.setdefault(host, []).append(name)
    for h, names in sorted(hosts.items()):
        print(f"  {h}: {', '.join(sorted(set(names)))}")
    con.close()
    os.remove(tmp)

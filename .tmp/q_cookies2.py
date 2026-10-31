import sqlite3, shutil, io, sys, os, time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

acc = "acc_ae35c364b1"
src = f"data/profiles/{acc}/Default/Network/Cookies"
tmp = f".tmp/cookies_{acc}.db"
ok = False
for i in range(6):
    try:
        shutil.copy2(src, tmp)
        ok = True
        break
    except Exception as e:
        print("retry", i, e)
        time.sleep(1)
if ok:
    con = sqlite3.connect(tmp)
    rows = con.execute("SELECT host_key, name FROM cookies").fetchall()
    print(f"{acc}: {len(rows)} cookies")
    hosts = {}
    for host, name in rows:
        hosts.setdefault(host, []).append(name)
    for h, names in sorted(hosts.items()):
        print(f"  {h}: {', '.join(sorted(set(names)))}")
    con.close()
    os.remove(tmp)
else:
    print("could not copy cookies db")

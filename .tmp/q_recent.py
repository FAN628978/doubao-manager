import sqlite3, json

con = sqlite3.connect('data/manager.sqlite3')
con.row_factory = sqlite3.Row
cur = con.cursor()
tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table'")]
print("TABLES:", tables)

for t in tables:
    cols = [r[1] for r in cur.execute(f"pragma table_info({t})")]
    print(f"\n== {t} == {cols}")

import sqlite3, json, textwrap

con = sqlite3.connect('data/manager.sqlite3')
con.row_factory = sqlite3.Row
cur = con.cursor()

rows = cur.execute("""
  SELECT j.id, j.status, j.account_id, j.error_message, j.created_at, j.updated_at, j.prompt,
         w.mode, w.ratio, w.duration, w.model, w.error_code, w.result_source, w.video_url, w.web_page_url
  FROM jobs j LEFT JOIN video_web_jobs w ON w.job_id=j.id
  ORDER BY j.created_at DESC LIMIT 20
""").fetchall()

for r in rows:
    print("="*100)
    print(f"job={r['id']} status={r['status']} account={r['account_id']} mode={r['mode']} ratio={r['ratio']} dur={r['duration']} model={r['model']}")
    print(f"created={r['created_at']} updated={r['updated_at']}")
    print(f"error_code={r['error_code']} result_source={r['result_source']}")
    print(f"error_message={r['error_message']}")
    print(f"prompt={ (r['prompt'] or '')[:200]!r}")
    evs = cur.execute("SELECT status,message,timestamp FROM video_web_events WHERE job_id=? ORDER BY id",(r['id'],)).fetchall()
    for e in evs:
        print(f"   [{e['timestamp']}] {e['status']}: {e['message']}")

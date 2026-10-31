from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    page = b.new_page(viewport={"width": 1700, "height": 1000})
    page.goto("http://127.0.0.1:8765/?v=autorun1", wait_until="networkidle")
    page.click("[data-view='jobs']")
    page.wait_for_timeout(1200)
    rows = page.locator("#jobsTable tr[data-job-id]")
    print("rows:", rows.count())
    for i in range(rows.count()):
        row = rows.nth(i)
        jid = row.get_attribute("data-job-id")
        util = row.locator(".next-utility")
        txt = util.inner_text() if util.count() else "(no utility)"
        print(jid, "|", txt.replace("\n", " / "), "| autoRun:", row.locator("button:has-text('自动执行')").count())
    b.close()

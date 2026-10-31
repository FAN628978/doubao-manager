from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, channel="chrome")
    page = browser.new_page()
    page.goto("http://127.0.0.1:8765/", wait_until="networkidle")
    page.wait_for_selector("#jobsTable tr[data-job-id]", timeout=15000, state="attached")
    rows = page.query_selector_all("#jobsTable tr[data-job-id]")
    print("rows:", len(rows))
    for row in rows[:3]:
        info = row.evaluate("""(tr) => ({
            title: tr.querySelector('.job-title')?.textContent,
            mode: tr.querySelector('.badge.manual, .badge.auto')?.textContent,
            buttons: [...tr.querySelectorAll('.job-title-row .btn')].map(b => b.textContent),
            status: tr.querySelector('.progress-hint')?.textContent,
        })""")
        print(info)
    browser.close()

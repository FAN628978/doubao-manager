from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    errors = []
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.goto("http://127.0.0.1:8765/?v=rowdel1", wait_until="networkidle")
    page.click("[data-view='jobs']")
    page.wait_for_timeout(1200)
    rows = page.locator("#jobsTable tr[data-job-id]")
    print("rows:", rows.count())
    btns = page.locator("#jobsTable .next-utility .text-link.danger")
    print("delete buttons:", btns.count())
    for i in range(min(btns.count(), 5)):
        el = btns.nth(i)
        print(i, repr(el.inner_text()), "onclick=", el.get_attribute("onclick"))
    # verify order inside first utility row
    if rows.count():
        print("first utility:", page.locator("#jobsTable tr[data-job-id] .next-utility").first.inner_text().replace("\n", " | "))
        page.screenshot(path=r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\row_delete_check.png")
    print("errors:", errors[:5])
    browser.close()

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    errors = []
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.goto("http://127.0.0.1:8765/?v=jobsui3", wait_until="networkidle")
    page.click("[data-view='jobs']")
    page.wait_for_timeout(1000)
    page.screenshot(path=r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\jobs_top2.png")
    print("badges:", page.locator("#jobsTable .badge").all_inner_texts()[:8])
    print("errors:", errors[:5])
    browser.close()

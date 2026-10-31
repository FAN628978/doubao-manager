from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1440, "height": 1150})
    errors = []
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.goto("http://127.0.0.1:8765/?v=jobsui1", wait_until="networkidle")
    page.click("[data-view='jobs']")
    page.wait_for_timeout(1200)
    print("rows:", page.locator("#jobsTable tr").count())
    print("chips:", page.locator(".filter-chip").count())
    print("first next button:", page.locator("#jobsTable .next-action button").first.inner_text() if page.locator("#jobsTable .next-action button").count() else "NONE")
    print("errors:", errors[:10])
    page.screenshot(path=r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\jobs_ui.png", full_page=True)
    # open the "more" modal for the first row only (no API side effects)
    page.locator("#jobsTable .next-action button", has_text="更多").first.click()
    page.wait_for_timeout(400)
    print("modal items:", page.locator("#jobActionsBody .action-item").count())
    page.screenshot(path=r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\jobs_modal.png")
    print("after modal errors:", errors[:10])
    browser.close()

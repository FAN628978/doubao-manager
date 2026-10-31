from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    page.goto("http://127.0.0.1:8765/?v=jobsui2", wait_until="networkidle")
    page.click("[data-view='jobs']")
    page.wait_for_timeout(1200)
    page.screenshot(path=r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\jobs_top.png")
    # filter to waiting
    page.locator(".filter-chip", has_text="等你发送").click()
    page.wait_for_timeout(300)
    print("waiting rows:", page.locator("#jobsTable tr").count())
    page.locator(".filter-chip", has_text="全部").click()
    page.wait_for_timeout(300)
    # sample badges from a downloaded row
    print("downloaded sample:", page.locator("#jobsTable tr", has_text="已归档").count())
    browser.close()

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    page.goto("http://127.0.0.1:8765/?v=rowdel2", wait_until="networkidle")
    page.click("[data-view='jobs']")
    page.wait_for_timeout(1200)
    wrap = page.locator(".jobs-panel .table-wrap")
    print("table scrollWidth/clientWidth:", wrap.evaluate("el=>[el.scrollWidth, el.clientWidth]"))
    wrap.evaluate("el=>el.scrollLeft=el.scrollWidth")
    page.wait_for_timeout(300)
    page.screenshot(path=r"C:\Users\fht\Desktop\doubao-manager-v3\.tmp\row_delete_check_right.png")
    util = page.locator("#jobsTable tr[data-job-id] .next-utility").first
    print("utility box:", util.bounding_box())
    print("links:", [el.bounding_box() for el in util.locator("button").all()])
    print("colors:", [el.evaluate("e=>getComputedStyle(e).color") for el in util.locator("button").all()])
    browser.close()

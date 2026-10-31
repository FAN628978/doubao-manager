import json
from playwright.sync_api import sync_playwright

out = {}

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="chrome")
    pg = b.new_page(viewport={"width": 1600, "height": 1000})
    errors = []
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto("http://127.0.0.1:8765/", wait_until="networkidle")
    pg.evaluate("switchView('jobs')")
    pg.wait_for_selector("#jobsTable tr[data-job-id]", state="attached")
    pg.wait_for_timeout(300)
    pg.screenshot(path=".tmp/bulk_default.png", full_page=False)

    boxes = pg.locator("#jobsTable .job-select-input")
    boxes.nth(0).check()
    pg.wait_for_timeout(200)
    pg.screenshot(path=".tmp/bulk_selected.png", full_page=False)

    pg.keyboard.down("Shift")
    boxes.nth(1).check()
    pg.keyboard.up("Shift")
    pg.wait_for_timeout(200)
    out["after_shift"] = pg.evaluate("""() => ({
        selected: state.selectedJobIds.size,
        checked: [...document.querySelectorAll('#jobsTable .job-select-input')].map(i => i.checked),
        count: document.getElementById('jobSelectedCount').textContent,
        total: document.getElementById('jobSelectedTotal').textContent,
    })""")
    out["buttons"] = pg.evaluate("""() => [...document.querySelectorAll('[data-bulk-action]')].map(b => ({
        action: b.dataset.bulkAction, disabled: b.disabled,
        n: b.querySelector('.bulk-n')?.textContent, title: b.title,
    }))""")
    pg.screenshot(path=".tmp/bulk_shift_selected.png", full_page=False)

    # header select-all toggles
    pg.click(".job-head-check")
    pg.wait_for_timeout(200)
    out["after_header_uncheck"] = pg.evaluate("""() => ({
        selected: state.selectedJobIds.size,
        head: document.getElementById('jobSelectAllHead').checked,
        indeterminate: document.getElementById('jobSelectAllHead').indeterminate,
    })""")

    # select all again via header
    pg.click(".job-head-check")
    pg.wait_for_timeout(200)
    out["after_header_check"] = pg.evaluate("""() => ({
        selected: state.selectedJobIds.size,
        head: document.getElementById('jobSelectAllHead').checked,
    })""")

    # hidden by filter warning
    pg.fill("#jobSearch", "zzzz-no-match")
    pg.wait_for_timeout(300)
    out["filtered_hidden"] = pg.evaluate("""() => ({
        selected: state.selectedJobIds.size,
        count: document.getElementById('jobSelectedCount').textContent,
        total: document.getElementById('jobSelectedTotal').textContent,
        warn: document.getElementById('jobHiddenSelected').textContent,
        warnHidden: document.getElementById('jobHiddenSelected').classList.contains('hidden'),
    })""")
    pg.screenshot(path=".tmp/bulk_filtered_hidden.png", full_page=False)

    # escape clears
    pg.fill("#jobSearch", "")
    pg.wait_for_timeout(300)
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)
    out["after_escape"] = pg.evaluate("() => state.selectedJobIds.size")

    # sticky bar check: scroll page down
    pg.evaluate("document.querySelector('.main').scrollTo(0, 600)")
    pg.evaluate("window.scrollTo(0, 600)")
    pg.wait_for_timeout(300)
    out["sticky"] = pg.evaluate("""() => {
        const bar = document.getElementById('jobBulkBar');
        const r = bar.getBoundingClientRect();
        const th = document.querySelector('.jobs-table th.select-col').getBoundingClientRect();
        return {barTop: Math.round(r.top), barPos: getComputedStyle(bar).position,
                colPos: getComputedStyle(document.querySelector('.jobs-table th.select-col')).position,
                colLeft: Math.round(th.left)};
    }""")
    pg.screenshot(path=".tmp/bulk_scrolled.png", full_page=False)

    out["errors"] = errors
    b.close()

open(".tmp/bulk_check_result.json", "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=2))
print("done")

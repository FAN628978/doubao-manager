"""Measure how fast the right-side detail panel and its Save button appear after a card click."""
import asyncio, sqlite3, time
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
JOB = "job_dbbb71fb1835"

async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3"); con.row_factory = sqlite3.Row
    job = con.execute("""SELECT j.account_id, w.web_page_url FROM jobs j JOIN video_web_jobs w ON w.job_id=j.id WHERE j.id=?""", (JOB,)).fetchone()
    profile = con.execute("SELECT profile_dir FROM accounts WHERE id=?", (job["account_id"],)).fetchone()["profile_dir"]
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile, channel="chrome", headless=False, accept_downloads=True,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        page = await ctx.new_page()
        await page.goto(job["web_page_url"], wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)
        card = page.locator('[class*="block-video"], [data-testid="video-result-card"]').last
        await card.scroll_into_view_if_needed(timeout=5000)
        t0 = time.monotonic()
        await card.click(timeout=5000)
        marks = {}
        for _ in range(100):  # 10s max, 100ms steps
            now = time.monotonic() - t0
            panel = await page.locator('[data-testid="samantha_layout_right_side"], [data-testid="canvas_panel_container"]').count()
            save = await page.locator('[data-testid="edit_image_download_button"]').count()
            vis = 0
            if save:
                for i in range(save):
                    if await page.locator('[data-testid="edit_image_download_button"]').nth(i).is_visible():
                        vis += 1
            vids = await page.evaluate("""() => Array.from(document.querySelectorAll('video'))
                .map(v => v.currentSrc || v.querySelector('source[src]')?.src || '')
                .filter(u => u.startsWith('http')).length""")
            for k, v in (("panel", panel), ("save", vis), ("video", vids)):
                if v and k not in marks:
                    marks[k] = round(now, 2)
            if {"panel", "save", "video"} <= set(marks):
                break
            await page.wait_for_timeout(100)
        print("marks:", marks)
        await ctx.close()

asyncio.run(main())

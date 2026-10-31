"""Live verification of ensure_chat_mode against the real Doubao home. No prompt is sent."""
import asyncio
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from web_api import click_home_mode, ensure_chat_mode, home_mode, work_mode_active

ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "acc_ae35c364b1"


async def video_skill_visible(page):
    return await page.evaluate(r"""() => Array.from(document.querySelectorAll('[data-testid^="skill_bar_button"]'))
      .some(b => /视频生成/.test(b.innerText || '') && b.getBoundingClientRect().width > 4)""")


async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3")
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT profile_dir, name FROM accounts WHERE id=?", (ACCOUNT,)).fetchone()
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=row["profile_dir"], channel="chrome", headless=False,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://www.doubao.com/", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(8000)
            print("home mode:", await home_mode(page), "| work:", await work_mode_active(page),
                  "| video skill:", await video_skill_visible(page), flush=True)

            assert await click_home_mode(page, "工作")
            await page.wait_for_timeout(2500)
            in_work = await work_mode_active(page)
            video_in_work = await video_skill_visible(page)
            print("after clicking 工作 -> mode:", await home_mode(page), "| work:", in_work,
                  "| video skill:", video_in_work, flush=True)

            await ensure_chat_mode(page)
            recovered = not await work_mode_active(page)
            video_after = await video_skill_visible(page)
            print("after ensure_chat_mode -> mode:", await home_mode(page), "| chat:", recovered,
                  "| video skill:", video_after, flush=True)
            assert in_work and not video_in_work, "fixture did not enter work mode"
            assert recovered and video_after, "ensure_chat_mode did not restore chat mode"
            print("LIVE OK", flush=True)
        finally:
            await ctx.close()

asyncio.run(main())

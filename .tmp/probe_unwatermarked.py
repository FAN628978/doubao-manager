"""Verify the Save-downloaded file is byte-identical to the panel's unwatermarked source."""
import asyncio, hashlib, json, sqlite3
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
JOB = "job_dbbb71fb1835"
SAVED = ROOT / ".tmp" / "save_probe_dl" / "江大教学楼外程砚收美金震惊.mp4"
OUT = ROOT / ".tmp" / "unwatermarked_fetch.mp4"

async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3"); con.row_factory = sqlite3.Row
    job = con.execute("""SELECT j.account_id, w.web_page_url, w.result_card_index
                         FROM jobs j JOIN video_web_jobs w ON w.job_id=j.id WHERE j.id=?""", (JOB,)).fetchone()
    profile = con.execute("SELECT profile_dir FROM accounts WHERE id=?", (job["account_id"],)).fetchone()["profile_dir"]
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile, channel="chrome", headless=False, accept_downloads=True,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        page = await ctx.new_page()
        await page.goto(job["web_page_url"], wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(7000)
        card = page.locator('[class*="block-video"], [data-testid="video-result-card"]').last
        await card.scroll_into_view_if_needed(timeout=5000)
        await card.click(timeout=5000)
        await page.wait_for_timeout(5000)
        urls = await page.evaluate("""() => Array.from(document.querySelectorAll('video'))
            .map(v => v.currentSrc || v.src || v.querySelector('source[src]')?.src || '')
            .filter(u => u.startsWith('http'))""")
        print("panel urls:", *[u[:100] for u in urls], sep="\n  ")
        url = urls[0]
        resp = await page.request.get(url, headers={"Range": "bytes=0-"}, timeout=120000, max_redirects=0)
        print("status", resp.status, "len-hdr", resp.headers.get("content-length"), "cr", resp.headers.get("content-range"))
        data = await resp.body()
        OUT.write_bytes(data)
        print("fetched", len(data), hashlib.sha256(data).hexdigest())
        saved = SAVED.read_bytes()
        print("saved  ", len(saved), hashlib.sha256(saved).hexdigest())
        print("identical:", hashlib.sha256(data).hexdigest() == hashlib.sha256(saved).hexdigest())
        await ctx.close()

asyncio.run(main())

"""Live end-to-end check of the new download path against a real archived job.

Uses web_api.download_result_video (the same function recall/download-result calls)
on the saved conversation; no generation, no quota.
"""
import asyncio, hashlib, sqlite3, sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import web_api

JOB = sys.argv[1] if len(sys.argv) > 1 else "job_dbbb71fb1835"
TARGET = ROOT / ".tmp" / "live_clean_download.mp4"


async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3"); con.row_factory = sqlite3.Row
    job = con.execute("""SELECT j.account_id, w.web_page_url, w.result_card_index, w.video_url
                         FROM jobs j JOIN video_web_jobs w ON w.job_id=j.id WHERE j.id=?""", (JOB,)).fetchone()
    profile = con.execute("SELECT profile_dir FROM accounts WHERE id=?", (job["account_id"],)).fetchone()["profile_dir"]
    print("[JOB]", JOB, job["web_page_url"], "stored_url_watermarked=", "video_gen_watermark" in (job["video_url"] or ""))
    TARGET.unlink(missing_ok=True)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile, channel="chrome", headless=False, accept_downloads=True,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        page = await ctx.new_page()
        await page.goto(job["web_page_url"], wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)
        state = await web_api.read_state(page)
        print("[STATE] cards=", state["videoCards"], "runningLast=", state["runningLast"],
              "urls=", len(state["videoUrls"]))
        idx = job["result_card_index"] if job["result_card_index"] is not None else 0
        result = await web_api.download_result_video(
            page, set(state["videoUrls"] or []), TARGET, card_index=idx, allow_known=True)
        path = Path(result["output"])
        data = path.read_bytes()
        print("[RESULT]", result["video_url"][:140])
        print("[FILE]", path, len(data), hashlib.sha256(data).hexdigest())
        print("[CLEAN_MARK]", web_api.CLEAN_MEDIA_MARK in (result["video_url"] or ""))
        await ctx.close()

asyncio.run(main())

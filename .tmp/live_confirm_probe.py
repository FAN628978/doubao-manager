# -*- coding: utf-8 -*-
"""Real-site diagnostic: drive one short reference-image generation and trace the
parameter-confirmation turn at millisecond granularity.

Does not touch the manager DB. Uses the same web_api functions as the service.
Usage: python .tmp/live_confirm_probe.py acc_4f61660da1
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from playwright.async_api import async_playwright
import web_api as W
from web_api import VideoRequest, ReviewNeeded

ACC = sys.argv[1] if len(sys.argv) > 1 else "acc_4f61660da1"
PROFILE = ROOT / "data" / "profiles" / ACC
DOWNLOADS = ROOT / "data" / "videos" / ACC
TARGET = ROOT / ".tmp" / "live_probe_result.mp4"
TRACE = ROOT / ".tmp" / f"live_confirm_trace_{ACC}.jsonl"
trace_fh = TRACE.open("w", encoding="utf-8")


def log(kind: str, **kw):
    rec = {"t": round(time.time(), 3), "kind": kind, **kw}
    line = json.dumps(rec, ensure_ascii=False)
    print(line, flush=True)
    trace_fh.write(line + "\n")
    trace_fh.flush()


orig_read_composer = W.read_composer_state
orig_confirm = W.confirm_pending_generation
orig_read_state = W.read_state


async def logged_read_composer(page):
    st = await orig_read_composer(page)
    log("composer_read", prompt=st["prompt"][:120], prompt_len=len(st["prompt"]),
        attachments=st["attachmentCount"], send_disabled=st["sendDisabled"])
    return st


async def poll_timeline(page, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        await asyncio.sleep(0.25)
        try:
            cs = await orig_read_composer(page)
            st = await orig_read_state(page)
            log("poll", prompt_len=len(cs["prompt"]), prompt=cs["prompt"][:80],
                msg_count=st["messageCount"], awaiting=st["awaitingConfirm"],
                started=st["generationStarted"], running_last=st["runningLast"],
                cards=st["videoCards"], done=st["done"], url=st["url"],
                last=st["lastMessageText"][-160:])
        except Exception as exc:
            log("poll_err", err=str(exc)[:200])
            return


async def logged_confirm(page):
    try:
        before = await orig_read_state(page)
        log("confirm_before", awaiting=before["awaitingConfirm"], msg_count=before["messageCount"],
            cards=before["videoCards"], last=before["lastMessageText"][-300:])
    except Exception as exc:
        log("confirm_before_err", err=str(exc)[:200])
    how = await orig_confirm(page)
    log("confirm_sent", how=how)
    asyncio.create_task(poll_timeline(page, 10.0))
    return how


W.read_composer_state = logged_read_composer
W.confirm_pending_generation = logged_confirm
# wait_for_result resolves the module globals at call time, so the patches apply.


async def main():
    req = VideoRequest(account_id=ACC, mode="reference_images", ratio="16:9", duration=4,
                       prompt="参考图1中的场景，镜头缓慢推进，电影感。",
                       reference_asset_ids=["asset_dceec041fb9b4eb2"])
    statuses = []

    async def on_status(status, message):
        statuses.append(status)
        log("status", status=status, message=message)

    async def on_page(url):
        log("page", url=url)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            accept_downloads=True, downloads_path=str(DOWNLOADS), no_viewport=True,
            locale="zh-CN", args=["--start-maximized"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://www.doubao.com/", wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_selector('[data-testid="chat_input"], [data-testid="chat_input_input"]',
                                             state="visible", timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)
            st = await orig_read_state(page)
            log("login_check", url=st["url"], text_tail=st["text"][-400:])
            if any(x in st["text"] for x in ("登录后才能", "请先登录", "扫码登录")):
                log("ABORT", reason="not_logged_in")
                return 2
            editor = await W.first_visible(page, W.EDITOR_SELECTORS)
            log("editor_present", present=bool(editor))
            if not editor:
                log("ABORT", reason="editor_missing")
                return 3
            try:
                out = await W.perform(ctx, req, {"asset_dceec041fb9b4eb2": ROOT / "data/web_assets/asset_dceec041fb9b4eb2.jpg"},
                                      TARGET, on_status, on_page)
                log("RESULT", output=out["output"], video_url=out["video_url"], page=out["web_page_url"])
                return 0
            except ReviewNeeded as exc:
                log("REVIEW_NEEDED", code=exc.code, message=exc.message)
                await asyncio.sleep(10)  # let the background timeline finish
                try:
                    st = await orig_read_state(page)
                    cs = await orig_read_composer(page)
                    log("FINAL_STATE", url=st["url"], msg_count=st["messageCount"],
                        awaiting=st["awaitingConfirm"], cards=st["videoCards"],
                        done=st["done"], running_last=st["runningLast"],
                        prompt=cs["prompt"][:200], last=st["lastMessageText"][-500:])
                except Exception as exc2:
                    log("FINAL_STATE_ERR", err=str(exc2)[:300])
                return 4
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
            trace_fh.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

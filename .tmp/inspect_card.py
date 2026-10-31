"""Probe #4: ground truth needed to rewrite the adapter.

Collects (a) every data-testid on the finished conversation page, (b) the ancestor
chain / testids around the video cover card, (c) the network requests that carry real
video bytes once the card is opened, (d) screenshots for visual confirmation.
No submit, no quota.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "probe_card.json"
SHOT_BEFORE = ROOT / ".tmp" / "card_before.png"
SHOT_AFTER = ROOT / ".tmp" / "card_after.png"
KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "城楼"
ACCOUNT = sys.argv[2] if len(sys.argv) > 2 else "acc_9b661e5a38"

TESTIDS = r"""() => Array.from(new Set(Array.from(document.querySelectorAll('[data-testid]'))
  .map(e => e.getAttribute('data-testid')).filter(Boolean))).sort()"""

CARD = r"""() => {
  const covers = Array.from(document.querySelectorAll('img[class*="cover"], [class*="video-card"], [class*="videoCard"]'));
  const last = covers.at(-1) || null;
  const chain = [];
  for (let e = last; e && chain.length < 14; e = e.parentElement) {
    chain.push({tag: e.tagName, testid: e.getAttribute('data-testid') || '',
      cls: String(e.className || '').slice(0, 120), role: e.getAttribute('role') || ''});
  }
  const scope = last ? (last.closest('[data-testid]') || last.closest('[class*="message"]') || last.parentElement) : document.body;
  const buttons = Array.from((scope || document.body).parentElement?.querySelectorAll('button,[role="button"],a') || [])
    .map(b => ({tag: b.tagName, text: (b.innerText || '').trim().slice(0, 24),
      aria: b.getAttribute('aria-label') || '', testid: b.getAttribute('data-testid') || '',
      disabled: b.disabled ?? b.getAttribute('aria-disabled')})).slice(0, 30);
  return {coverCount: covers.length,
    coverSrc: last ? (last.currentSrc || last.src || '').slice(0, 200) : '',
    coverClass: last ? String(last.className).slice(0, 120) : '',
    ancestorChain: chain, nearbyButtons: buttons,
    tailText: (document.body?.innerText || '').slice(-700)};
}"""


async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3")
    con.row_factory = sqlite3.Row
    profile = con.execute("SELECT profile_dir FROM accounts WHERE id=?", (ACCOUNT,)).fetchone()["profile_dir"]
    media = []
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile, channel="chrome", headless=False,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        page = await ctx.new_page()

        def on_response(res):
            ct = (res.headers.get("content-type") or "")
            u = res.url
            if ct.startswith("video/") or any(k in u.lower() for k in (".mp4", ".webm", ".m3u8", "videocloud", "vod", "playback")):
                media.append({"url": u[:400], "status": res.status, "contentType": ct,
                              "length": res.headers.get("content-length", ""),
                              "range": res.request.headers.get("range", "")[:20]})

        page.on("response", on_response)
        page.on("request", lambda r: media.append({"kind": "request", "url": r.url[:400],
                                                   "resourceType": r.resource_type})
                if r.resource_type == "media" else None)

        await page.goto("https://www.doubao.com/chat", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(7000)
        await page.locator(f'text={KEYWORD}').first.click(timeout=8000)
        await page.wait_for_timeout(9000)
        for _ in range(5):
            await page.mouse.wheel(0, 1500)
            await page.wait_for_timeout(1000)

        out = {"url": page.url, "testids": await page.evaluate(TESTIDS),
               "card": await page.evaluate(CARD), "mediaBeforeClick": list(media)}
        await page.screenshot(path=str(SHOT_BEFORE), full_page=False)

        cover = page.locator('img[class*="cover"]').last
        clicked = False
        try:
            await cover.click(timeout=6000)
            clicked = True
            await page.wait_for_timeout(6000)
        except Exception as exc:
            out["clickError"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        out["clickedCover"] = clicked
        out["videosAfterClick"] = await page.evaluate(r"""() => Array.from(document.querySelectorAll('video')).map(v => ({
            currentSrc: (v.currentSrc || '').slice(0, 300), src: (v.getAttribute('src') || '').slice(0, 300),
            childSource: (v.querySelector('source[src]')?.src || '').slice(0, 300), w: v.videoWidth, h: v.videoHeight}))""")
        out["mediaAfterClick"] = media[len(out["mediaBeforeClick"]):]
        await page.screenshot(path=str(SHOT_AFTER), full_page=False)
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[DUMP]", OUT, "| covers=", out["card"]["coverCount"],
              "| clicked=", clicked, "| videos=", len(out["videosAfterClick"]),
              "| mediaHits=", len(out["mediaAfterClick"]))
        await ctx.close()

asyncio.run(main())

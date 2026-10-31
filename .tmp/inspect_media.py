"""Probe #5: open the finished video card's player and capture the real media URL.

Hovers the card (the hover button group holds the expand/play control), dumps that
group, clicks through, and records every media-bearing response. No submit, no quota.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "probe_media.json"
SHOT = ROOT / ".tmp" / "player_open.png"
KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "城楼"
ACCOUNT = sys.argv[2] if len(sys.argv) > 2 else "acc_9b661e5a38"

CARD_INNER = r"""() => {
  const cards = Array.from(document.querySelectorAll('[class*="block-video"]'));
  const last = cards.at(-1);
  if (!last) return {cardCount: 0};
  const pick = (r) => Array.from(r.querySelectorAll('button,[role="button"],[class*="icon"],[class*="btn"],[class*="hover"]'))
    .map(b => ({tag: b.tagName, cls: String(b.className || '').slice(0, 70),
      text: (b.innerText || '').trim().slice(0, 16), aria: b.getAttribute('aria-label') || '',
      testid: b.getAttribute('data-testid') || '', src: (b.getAttribute('src') || '').slice(0, 120)})).slice(0, 25);
  return {cardCount: cards.length, cardClass: String(last.className).slice(0, 140),
    inner: pick(last), parentInner: pick(last.parentElement || last),
    outerHead: last.outerHTML.slice(0, 1200)};
}"""

VIDEOS = r"""() => Array.from(document.querySelectorAll('video')).map(v => ({
  currentSrc: (v.currentSrc || '').slice(0, 400), src: (v.getAttribute('src') || '').slice(0, 400),
  child: (v.querySelector('source[src]')?.src || '').slice(0, 400),
  w: v.videoWidth, h: v.videoHeight, dur: v.duration, readyState: v.readyState}))"""


async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3")
    con.row_factory = sqlite3.Row
    profile = con.execute("SELECT profile_dir FROM accounts WHERE id=?", (ACCOUNT,)).fetchone()["profile_dir"]
    hits = []
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile, channel="chrome", headless=False,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        page = await ctx.new_page()
        page.on("response", lambda r: hits.append(
            {"url": r.url[:500], "status": r.status, "ct": r.headers.get("content-type", ""),
             "len": r.headers.get("content-length", "")})
            if (r.headers.get("content-type", "").startswith("video/")
                or any(k in r.url.lower() for k in (".mp4", ".webm", "videocloud", "playback", "/vod"))) else None)
        page.on("request", lambda r: hits.append({"kind": "media-request", "url": r.url[:500]})
                if r.resource_type == "media" else None)

        await page.goto("https://www.doubao.com/chat", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(7000)
        await page.locator(f'text={KEYWORD}').first.click(timeout=8000)
        await page.wait_for_timeout(9000)
        await page.mouse.wheel(0, 2000)
        await page.wait_for_timeout(1500)

        out = {"url": page.url}
        card = page.locator('[class*="block-video"]').last
        out["before"] = await page.evaluate(CARD_INNER)
        try:
            await card.scroll_into_view_if_needed(timeout=5000)
            await card.hover(timeout=5000)
            await page.wait_for_timeout(1200)
            out["hovered"] = True
        except Exception as exc:
            out["hoverError"] = f"{type(exc).__name__}: {str(exc)[:140]}"
        out["afterHover"] = await page.evaluate(CARD_INNER)
        for attempt in ("click-card", "click-cover-force", "press-enter"):
            try:
                if attempt == "click-card":
                    await card.click(timeout=4000)
                elif attempt == "click-cover-force":
                    await card.locator("img").first.click(timeout=4000, force=True)
                else:
                    await card.focus(); await page.keyboard.press("Enter")
                out["openedVia"] = attempt
                break
            except Exception as exc:
                out.setdefault("openErrors", []).append(f"{attempt}: {type(exc).__name__}: {str(exc)[:110]}")
        await page.wait_for_timeout(7000)
        out["videos"] = await page.evaluate(VIDEOS)
        out["mediaHits"] = hits[:25]
        await page.screenshot(path=str(SHOT))
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[DUMP]", OUT, "| cards=", out["before"].get("cardCount"), "| openedVia=", out.get("openedVia"),
              "| videos=", len(out["videos"]), "| mediaHits=", len(out["mediaHits"]))
        await ctx.close()

asyncio.run(main())

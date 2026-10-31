"""Probe #3: where does Doubao's finished video card actually live?

Checks every frame (iframe/webframe) and walks open shadow roots in the main frame.
No submit, no quota. Writes UTF-8 JSON to .tmp/probe_frames.json.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "probe_frames.json"
KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "城楼"
ACCOUNT = sys.argv[2] if len(sys.argv) > 2 else "acc_9b661e5a38"

SHADOW_WALK = r"""() => {
  const found = [];
  const walk = (root, depth) => {
    if (depth > 12 || found.length > 40) return;
    for (const el of root.querySelectorAll ? root.querySelectorAll('*') : []) {
      if (el.tagName === 'VIDEO' || el.tagName === 'IFRAME' || el.tagName === 'CANVAS'
          || (el.tagName === 'IMG' && /video|play|cover/i.test(el.className || ''))) {
        found.push({tag: el.tagName, src: (el.currentSrc || el.src || '').slice(0, 240),
          poster: (el.poster || el.getAttribute('poster') || '').slice(0, 160),
          cls: String(el.className || '').slice(0, 80), depth,
          testid: el.getAttribute && (el.getAttribute('data-testid') || '')});
      }
      if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
    }
  };
  walk(document, 0);
  return {shadowHostCount: document.querySelectorAll('*').length && Array.from(document.querySelectorAll('*')).filter(e => e.shadowRoot).length,
          found};
}"""

FRAME = r"""() => ({href: location.href.slice(0, 200),
  videos: Array.from(document.querySelectorAll('video')).map(v => ({
    src: (v.currentSrc || v.src || v.querySelector('source')?.src || '').slice(0, 300),
    poster: (v.poster || '').slice(0, 160), w: v.videoWidth, h: v.videoHeight, dur: v.duration})),
  mp4ish: (document.documentElement.outerHTML.match(/https[^"'\s\\)]{10,240}?\.(?:mp4|webm|m3u8)[^"'\s\\)]{0,80}/gi) || []).slice(0, 8),
  textHit: /生成好了|视频生成|下载/.test(document.body?.innerText || '')})"""


async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3")
    con.row_factory = sqlite3.Row
    profile = con.execute("SELECT profile_dir FROM accounts WHERE id=?", (ACCOUNT,)).fetchone()["profile_dir"]
    out = {"frames": [], "shadow": None}
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile, channel="chrome", headless=False,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        page = await ctx.new_page()
        await page.goto("https://www.doubao.com/chat", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(7000)
        await page.locator(f'text={KEYWORD}').first.click(timeout=8000)
        await page.wait_for_timeout(9000)
        for _ in range(5):
            await page.mouse.wheel(0, 1500)
            await page.wait_for_timeout(1200)
        out["url"] = page.url
        out["shadow"] = await page.evaluate(SHADOW_WALK)
        # Playwright's own CSS engine pierces open shadow roots — compare against querySelectorAll.
        out["playwrightLocatorVideoCount"] = await page.locator("video").count()
        for fr in page.frames:
            try:
                out["frames"].append({"name": fr.name, **(await fr.evaluate(FRAME))})
            except Exception as exc:
                out["frames"].append({"name": fr.name, "error": type(exc).__name__})
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[DUMP]", OUT, "frames=", len(out["frames"]),
              "shadowVideos=", sum(1 for f in out["shadow"]["found"] if f["tag"] == "VIDEO"),
              "locatorVideos=", out["playwrightLocatorVideoCount"])
        await ctx.close()

asyncio.run(main())

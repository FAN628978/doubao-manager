"""Read-only probe #2: reach the finished generation via the sidebar conversation
list (the recorded local_* URL does not restore), then dump what the adapter can see.

No submit, no quota. Writes UTF-8 JSON to .tmp/probe_dump.json (console is cp936).
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "probe_dump.json"
KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "城楼"
ACCOUNT = sys.argv[2] if len(sys.argv) > 2 else "acc_9b661e5a38"

DUMP = r"""() => {
  const vids = [];
  for (const v of document.querySelectorAll('video')) {
    vids.push({currentSrc: (v.currentSrc || '').slice(0, 300), src: (v.getAttribute('src') || '').slice(0, 300),
      childSource: (v.querySelector('source[src]')?.src || '').slice(0, 300),
      poster: (v.getAttribute('poster') || '').slice(0, 160),
      readyState: v.readyState, w: v.videoWidth, h: v.videoHeight, dur: v.duration,
      testidAncestors: (() => { let e = v, out = [];
        for (let i = 0; i < 8 && e; i = e.parentElement) { if (e.dataset) for (const k in e.dataset) if (/testid|id|url|src|type|status/i.test(k)) out.push(e.tagName + '[' + k + '=' + String(e.dataset[k]).slice(0, 120) + ']'); e = e.parentElement; }
        return out.slice(0, 10); })()});
  }
  const dataAttrs = {};
  for (const el of document.querySelectorAll('[data-src],[data-url],[data-video-url],[data-play-url],[data-download-url],[data-vid]'))
    for (const k of Object.keys(el.dataset)) if (/src|url|vid/i.test(k)) (dataAttrs[k] ||= []).push(String(el.dataset[k]).slice(0, 260));
  const mediaLinks = Array.from(document.querySelectorAll('a[href]'))
    .map(a => a.href).filter(h => /\.(mp4|webm|m3u8)(\?|$)/i.test(h) || /video|media|\.volc|tos\.|byteimg/i.test(h)).slice(0, 15);
  const buttons = Array.from(document.querySelectorAll('button,[role="button"],[data-testid]'))
    .filter(b => /下载|导出|download|保存/i.test((b.innerText || '') + (b.getAttribute('aria-label') || '') + (b.getAttribute('data-testid') || '')))
    .slice(0, 15).map(b => ({tag: b.tagName, text: (b.innerText || '').trim().slice(0, 20),
      aria: b.getAttribute('aria-label') || '', testid: b.getAttribute('data-testid') || ''}));
  const text = document.body?.innerText || '';
  return {url: location.href, videoCount: vids.length, videos: vids,
    httpVideoUrls: vids.map(v => v.currentSrc || v.src || v.childSource).filter(u => u && /^https?:/.test(u)),
    blobUrls: vids.map(v => v.currentSrc || v.src || v.childSource).filter(u => u && /^blob:/.test(u)),
    dataAttrs, mediaLinks, buttons,
    keywordHits: Object.fromEntries(['生成中','正在生成','排队中','处理中','生成完成','视频已生成','生成成功','下载','预计','秒']
      .map(k => [k, text.includes(k)])),
    textTail: text.slice(-1500)};
}"""


async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3")
    con.row_factory = sqlite3.Row
    profile = con.execute("SELECT profile_dir FROM accounts WHERE id=?", (ACCOUNT,)).fetchone()["profile_dir"]
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile, channel="chrome", headless=False,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        page = await ctx.new_page()
        await page.goto("https://www.doubao.com/chat", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(7000)
        hit = page.locator(f'text={KEYWORD}').first
        reached = False
        try:
            await hit.click(timeout=8000)
            reached = True
            await page.wait_for_timeout(9000)
        except Exception as exc:
            print("[WARN] sidebar item not clicked:", type(exc).__name__, str(exc)[:120])
        for _ in range(4):
            await page.mouse.wheel(0, 1500)
            await page.wait_for_timeout(1200)
        dump = {"reachedViaSidebar": reached, "keyword": KEYWORD,
                **await page.evaluate(DUMP)}
        OUT.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[DUMP]", OUT, "videoCount=", dump["videoCount"], "url=", dump["url"])
        await ctx.close()

asyncio.run(main())

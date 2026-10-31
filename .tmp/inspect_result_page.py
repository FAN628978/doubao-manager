"""Read-only probe: what does a finished Doubao result page actually expose?

Opens the recorded session URL in the account's own profile and dumps every video
source the current read_state() would see, plus the things it ignores (blob:,
poster-only cards, download buttons). Performs no clicks that submit a generation.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
URL = sys.argv[1] if len(sys.argv) > 1 else None
ACCOUNT = sys.argv[2] if len(sys.argv) > 2 else "acc_9b661e5a38"

DUMP = r"""() => {
  const vids = [];
  for (const v of document.querySelectorAll('video')) {
    vids.push({currentSrc: v.currentSrc || null, src: v.getAttribute('src') || null,
      childSource: v.querySelector('source[src]')?.src || null,
      poster: v.getAttribute('poster') || null,
      networks: Array.from(v.querySelectorAll('source')).map(s => s.src),
      readyState: v.readyState, w: v.videoWidth, h: v.videoHeight, dur: v.duration});
  }
  const anchors = Array.from(document.querySelectorAll('a[href]'))
    .filter(a => /\.(mp4|webm)(\?|$)/i.test(a.href) || /video|media|download/i.test(a.href))
    .slice(0, 12).map(a => ({text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 30), href: a.href.slice(0, 220)}));
  const buttons = Array.from(document.querySelectorAll('button,[role="button"],[data-testid]'))
    .filter(b => /下载|导出|download/i.test((b.innerText || '') + (b.getAttribute('aria-label') || '') + (b.getAttribute('data-testid') || '')))
    .slice(0, 12).map(b => ({tag: b.tagName, text: (b.innerText || '').trim().slice(0, 24),
      aria: b.getAttribute('aria-label') || '', testid: b.getAttribute('data-testid') || ''}));
  const text = document.body?.innerText || '';
  const keys = ['生成中', '正在生成', '排队中', '处理中', '生成完成', '视频已生成', '生成成功', '下载', '预计'];
  return {url: location.href,
    videoCount: vids.length, videos: vids,
    httpVideoUrls: vids.map(v => v.currentSrc || v.src || v.childSource).filter(u => u && /^https?:/.test(u)),
    blobUrls: vids.map(v => v.currentSrc || v.src || v.childSource).filter(u => u && /^blob:/.test(u)),
    anchors, buttons,
    keywordHits: Object.fromEntries(keys.map(k => [k, text.includes(k)])),
    textTail: text.slice(-900)};
}"""


async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3")
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT profile_dir FROM accounts WHERE id=?", (ACCOUNT,)).fetchone()
    target = URL or con.execute(
        "SELECT web_page_url FROM video_web_jobs ORDER BY rowid DESC LIMIT 1").fetchone()["web_page_url"]
    print("[PROBE]", target)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=row["profile_dir"], channel="chrome", headless=False,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        page = await ctx.new_page()
        await page.goto(target, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(9000)
        for _ in range(3):  # let lazy media cards mount
            await page.mouse.wheel(0, 1200)
            await page.wait_for_timeout(1500)
        print(json.dumps(await page.evaluate(DUMP), ensure_ascii=False, indent=2))
        await ctx.close()

asyncio.run(main())

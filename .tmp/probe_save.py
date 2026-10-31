"""Read-only live probe: open the saved result, click the video card, inspect the
right-side detail panel and try its Save (保存) button. No generation, no quota.

Writes .tmp/save_probe.json + .tmp/save_panel.png; downloads land in .tmp/save_probe_dl/.
"""
import asyncio, json, sqlite3, sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
JOB = sys.argv[1] if len(sys.argv) > 1 else "job_dbbb71fb1835"
OUT = ROOT / ".tmp" / "save_probe.json"
SHOT = ROOT / ".tmp" / "save_panel.png"
DLDIR = ROOT / ".tmp" / "save_probe_dl"

DUMP = r"""() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = el => (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40);
  const buttons = Array.from(document.querySelectorAll('button,[role="button"],[data-testid],a'))
    .filter(vis)
    .map(el => ({tag: el.tagName, text: txt(el), aria: el.getAttribute('aria-label') || '',
                 title: el.getAttribute('title') || '', testid: el.getAttribute('data-testid') || '',
                 cls: String(el.className?.baseVal ?? el.className ?? '').slice(0, 90),
                 href: (el.getAttribute('href') || '').slice(0, 200),
                 rect: (r => ({x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}))(el.getBoundingClientRect())}))
    .filter(b => b.text || b.aria || b.title || b.testid);
  const interesting = buttons.filter(b => /保存|下载|导出|save|download/i.test(b.text + b.aria + b.title + b.testid + b.cls));
  const videos = Array.from(document.querySelectorAll('video')).filter(vis).map(v => ({
    src: (v.currentSrc || v.src || v.querySelector('source[src]')?.src || '').slice(0, 300),
    w: v.videoWidth, h: v.videoHeight, dur: v.duration}));
  const svgs = Array.from(document.querySelectorAll('svg'))
    .filter(vis).map(s => { const p = s.parentElement; return {parentTag: p?.tagName, parentText: txt(p || s).slice(0, 20),
      parentAria: p?.getAttribute?.('aria-label') || '', parentCls: String(p?.className || '').slice(0, 80)}; })
    .filter(x => /保存|下载|download/i.test(x.parentText + x.parentAria));
  return {url: location.href, bodyTail: (document.body?.innerText || '').slice(-600),
          buttonCount: buttons.length, interesting, videos, svgs,
          docTop: document.elementFromPoint(innerWidth - 60, 24)?.outerHTML.slice(0, 400) || ''};
}"""


async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3"); con.row_factory = sqlite3.Row
    job = con.execute("""SELECT j.account_id, w.web_page_url, w.result_card_index
                         FROM jobs j JOIN video_web_jobs w ON w.job_id=j.id WHERE j.id=?""", (JOB,)).fetchone()
    if not job:
        raise SystemExit("job not found")
    profile = con.execute("SELECT profile_dir FROM accounts WHERE id=?", (job["account_id"],)).fetchone()["profile_dir"]
    url = job["web_page_url"]
    print("[PROBE]", JOB, url, profile)
    DLDIR.mkdir(exist_ok=True)
    out = {"job": JOB, "url": url}
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile, channel="chrome", headless=False, accept_downloads=True,
            downloads_path=str(DLDIR), no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        page = await ctx.new_page()
        reqs = []
        page.on("request", lambda r: reqs.append({"rt": r.resource_type, "url": r.url[:300]})
                if any(k in r.url.lower() for k in ("download", "save_video", "savevideo", "video/tos")) else None)
        page.on("response", lambda r: reqs.append({"rt": "resp", "status": r.status, "ct": r.headers.get("content-type", ""),
                                                   "cd": r.headers.get("content-disposition", ""), "url": r.url[:300]})
                if "download" in r.url.lower() or "save" in r.url.lower() else None)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(7000)
        out["before"] = await page.evaluate(DUMP)

        idx = job["result_card_index"] if job["result_card_index"] is not None else 0
        cards = page.locator('[class*="block-video"], [data-testid="video-result-card"]')
        n = await cards.count()
        out["cardCount"] = n
        if n:
            card = cards.nth(idx if idx < n else n - 1)
            await card.scroll_into_view_if_needed(timeout=5000)
            await card.hover(timeout=4000)
            await page.wait_for_timeout(500)
            await card.click(timeout=5000)
            await page.wait_for_timeout(5000)
        out["afterOpen"] = await page.evaluate(DUMP)
        await page.screenshot(path=str(SHOT))

        # Try Save via several strategies; first that fires a download wins.
        attempts = []
        save = None
        for sel in ['button:has-text("保存")', '[role="button"]:has-text("保存")',
                    'button[aria-label*="保存"]', 'button[aria-label*="下载"]',
                    '[data-testid*="save"]', '[data-testid*="download"]']:
            loc = page.locator(sel)
            for i in range(min(await loc.count(), 5)):
                try:
                    if await loc.nth(i).is_visible():
                        save = loc.nth(i); break
                except Exception:
                    continue
            if save is not None:
                attempts.append({"strategy": sel, "found": True,
                                 "text": (await save.inner_text())[:30]})
                break
        if save is None:
            out["attempts"] = attempts + [{"strategy": "none", "found": False}]
        else:
            try:
                async with page.expect_download(timeout=20000) as dl:
                    await save.click()
                d = await dl.value
                path = DLDIR / (d.suggested_filename or "download.mp4")
                await d.save_as(path)
                out["download"] = {"filename": d.suggested_filename, "url": d.url[:400],
                                   "path": str(path), "size": path.stat().st_size}
            except Exception as exc:
                out["downloadError"] = f"{type(exc).__name__}: {str(exc)[:400]}"
                # The button may have opened a menu; dump what's visible now.
                await page.wait_for_timeout(1200)
                out["afterSaveClick"] = await page.evaluate(DUMP)
                try:
                    await page.screenshot(path=str(ROOT / ".tmp" / "save_panel_after.png"))
                except Exception:
                    pass
        out["attempts"] = attempts
        out["requests"] = reqs[:40]
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[DONE]", OUT, "| download:", out.get("download"), "| err:", out.get("downloadError"))
        await ctx.close()

asyncio.run(main())

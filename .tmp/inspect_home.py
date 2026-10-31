"""Probe #6: what does the doubao landing page offer for video-mode activation right now?

Read-only. Explains SETTING_NOT_FOUND on mode without spending quota.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "probe_home.json"
SHOT = ROOT / ".tmp" / "home.png"
ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "acc_9b661e5a38"

DUMP = r"""() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const skill = Array.from(document.querySelectorAll('[data-testid^="skill_bar_button"]')).map(b => ({
    testid: b.getAttribute('data-testid'), text: (b.innerText || '').trim().slice(0, 20),
    visible: vis(b), aria: b.getAttribute('aria-label') || ''}));
  const modeish = Array.from(document.querySelectorAll('button,[role="button"],[data-testid]'))
    .filter(e => /视频生成|生成视频|文生视频|首帧/.test((e.innerText || '') + (e.getAttribute('aria-label') || '') + (e.getAttribute('title') || '')))
    .slice(0, 14).map(e => ({tag: e.tagName, testid: e.getAttribute('data-testid') || '',
      text: (e.innerText || '').trim().slice(0, 24), visible: vis(e)}));
  const params = document.querySelector('[data-input-engine-actionbar-render-entry-key="video-generation-params-panel"]');
  const composer = document.querySelector('[data-testid="chat_input"]');
  return {url: location.href,
    skillButtons: skill, modeish,
    paramsPanel: params ? {visible: vis(params), text: (params.innerText || '').trim().slice(0, 60)} : null,
    composer: composer ? {visible: vis(composer),
      hasEditable: !!composer.querySelector('[contenteditable="true"]'),
      actions: Array.from(composer.querySelectorAll('[data-testid]')).map(e => e.getAttribute('data-testid'))} : null,
    testids: Array.from(new Set(Array.from(document.querySelectorAll('[data-testid]')).map(e => e.getAttribute('data-testid')).filter(Boolean))).sort(),
    bodyHead: (document.body?.innerText || '').slice(0, 500)};
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
        await page.goto("https://www.doubao.com/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(9000)
        out = {"before": await page.evaluate(DUMP)}
        try:
            await page.locator('[data-testid="skill_bar_button_17"]').first.click(timeout=6000)
            out["clicked"] = True
        except Exception as exc:
            out["clicked"] = False
            out["clickError"] = f"{type(exc).__name__}: {str(exc)[:140]}"
        await page.wait_for_timeout(6000)
        out["after"] = await page.evaluate(DUMP)
        await page.screenshot(path=str(SHOT))
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[DUMP]", OUT, "| clicked=", out["clicked"], "| after url=", out["after"]["url"],
              "| paramsPanel=", out["after"]["paramsPanel"], "| skills=", len(out["after"]["skillButtons"]))
        await ctx.close()

asyncio.run(main())

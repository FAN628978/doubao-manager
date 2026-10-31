"""Probe #2: enter 工作, then test every recovery path and storage persistence. Read-only."""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "probe_work_mode2.json"
SHOT = ROOT / ".tmp" / "wm2"
ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "acc_ae35c364b1"

STATE = r"""() => {
  const vis = (e) => { if (!e) return false; const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = (e) => ((e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim());
  const sw = document.querySelector('[data-testid="conversation-mode-switch"]');
  const seg = sw ? Array.from(sw.querySelectorAll('button')).map(b => ({
      text: txt(b), cls: (b.className || '').toString().replace(/\s+/g, ' ').slice(0, 220)})) : [];
  const ind = sw && sw.children[0] ? {cls: (sw.children[0].className || '').toString().replace(/\s+/g,' ').slice(0,200),
      style: sw.children[0].getAttribute('style') || ''} : null;
  const g = document.querySelector('[data-testid="flow_chat_guidance_page"]');
  return {url: location.href,
    modeSwitch: seg, indicator: ind,
    heading: g ? txt(g).slice(0, 30) : null,
    hasChatInput: vis(document.querySelector('[data-testid="chat_input"]')),
    hasVideoSkill: Array.from(document.querySelectorAll('[data-testid^="skill_bar_button"]')).some(b => /视频生成/.test(txt(b)) && vis(b)),
    hasMoreSkill: !!document.querySelector('[data-testid="skill_bar_button_more"]'),
    workHome: /今天有什么工作要处理/.test(txt(g)),
    ls: Object.fromEntries(Object.entries(localStorage).filter(([k]) => /mode|work|chat|agent/i.test(k))),
    ss: Object.fromEntries(Object.entries(sessionStorage).filter(([k]) => /mode|work|chat|agent/i.test(k)))};
}"""


async def state(page):
    return await page.evaluate(STATE)


async def click(page, scope, text):
    try:
        loc = page.locator(scope).get_by_text(text, exact=True)
        for i in range(min(await loc.count(), 8)):
            if await loc.nth(i).is_visible():
                await loc.nth(i).click()
                return True
    except Exception as exc:
        return f"ERR {type(exc).__name__}: {str(exc)[:100]}"
    return False


async def snap(page, name, out):
    out[name] = await state(page)
    await page.screenshot(path=str(SHOT.with_name(SHOT.name + "_" + name + ".png")))
    print("[snap]", name, "workHome=", out[name]["workHome"], "videoSkill=", out[name]["hasVideoSkill"],
          "chatInput=", out[name]["hasChatInput"], flush=True)


async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3")
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT profile_dir, name FROM accounts WHERE id=?", (ACCOUNT,)).fetchone()
    out = {"account": dict(row)}
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=row["profile_dir"], channel="chrome", headless=False,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://www.doubao.com/", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(8000)
            await snap(page, "01_home", out)

            out["enter_work"] = await click(page, '[data-testid="conversation-mode-switch"]', "工作")
            await page.wait_for_timeout(3000)
            await snap(page, "02_work", out)

            # Persisted? wait longer and re-snapshot before reload
            await page.wait_for_timeout(6000)
            await snap(page, "03_work_later", out)

            # A) new tab to doubao.com, like perform() does
            page2 = await ctx.new_page()
            await page2.goto("https://www.doubao.com/", wait_until="domcontentloaded", timeout=60000)
            await page2.wait_for_timeout(7000)
            await snap(page2, "04_new_tab", out)
            await page2.close()

            # B) from work mode: click sidebar 新对话
            out["new_chat_click"] = await click(page, "body", "新对话")
            await page.wait_for_timeout(4000)
            await snap(page, "05_after_new_chat", out)

            # C) if still work: click 对话 segment
            s = await state(page)
            if s["workHome"] or not s["hasVideoSkill"]:
                out["chat_tab_click"] = await click(page, '[data-testid="conversation-mode-switch"]', "对话")
                await page.wait_for_timeout(4000)
                await snap(page, "06_after_chat_tab", out)

            OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            print("[DUMP]", OUT, flush=True)
        finally:
            await ctx.close()

asyncio.run(main())

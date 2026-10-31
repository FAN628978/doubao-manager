"""Probe #3: verify recovery actions are safe and idempotent. Read-only."""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "probe_work_mode3.json"
ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "acc_ae35c364b1"

STATE = r"""() => {
  const vis = (e) => { if (!e) return false; const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = (e) => ((e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim());
  const sw = document.querySelector('[data-testid="conversation-mode-switch"]');
  const g = document.querySelector('[data-testid="flow_chat_guidance_page"]');
  return {url: location.href,
    indicator: sw && sw.children[0] ? (sw.children[0].getAttribute('style') || '') : null,
    selected: sw ? Array.from(sw.querySelectorAll('button')).map(b => ({
        text: txt(b), primary: /text-dbx-text-primary/.test((b.className||'').toString())})) : [],
    heading: g ? txt(g).slice(0, 40) : null,
    videoSkill: Array.from(document.querySelectorAll('[data-testid^="skill_bar_button"]')).some(b => /视频生成/.test(txt(b)) && vis(b)),
    moreSkill: !!document.querySelector('[data-testid="skill_bar_button_more"]') && vis(document.querySelector('[data-testid="skill_bar_button_more"]')),
    workHome: g ? /今天有什么工作要处理|下载豆包工作电脑版/.test(txt(g)) : false,
    chatInput: vis(document.querySelector('[data-testid="chat_input"]'))};
}"""


async def st(page):
    return await page.evaluate(STATE)


async def click_seg(page, text):
    loc = page.locator('[data-testid="conversation-mode-switch"]').get_by_text(text, exact=True)
    for i in range(min(await loc.count(), 8)):
        if await loc.nth(i).is_visible():
            await loc.nth(i).click()
            return True
    return False


async def click_btn(page, testid):
    loc = page.locator(f'[data-testid="{testid}"]').first
    if await loc.count() and await loc.is_visible():
        await loc.click()
        return True
    return False


async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3")
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT profile_dir, name FROM accounts WHERE id=?", (ACCOUNT,)).fetchone()
    out = {"account": dict(row), "steps": []}

    async def step(page, label):
        s = await st(page)
        out["steps"].append({"label": label, **s})
        print(f"[{label}] work={s['workHome']} video={s['videoSkill']} more={s['moreSkill']} "
              f"primary={[x['text'] for x in s['selected'] if x['primary']]} ind={s['indicator']}", flush=True)
        return s

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=row["profile_dir"], channel="chrome", headless=False,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://www.doubao.com/", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(8000)
            s = await step(page, "home")

            # 1) click 对话 while already in 对话 (idempotency)
            out["chat_click_in_chat"] = await click_seg(page, "对话")
            await page.wait_for_timeout(2500)
            await step(page, "chat_click_in_chat_after")

            # 2) enter 工作, then click 对话
            out["work_click"] = await click_seg(page, "工作")
            await page.wait_for_timeout(2500)
            await step(page, "work")
            out["chat_click_in_work"] = await click_seg(page, "对话")
            await page.wait_for_timeout(3000)
            await step(page, "chat_click_in_work_after")

            # 3) enter 工作 again, recover with sidebar 新对话
            out["work_click2"] = await click_seg(page, "工作")
            await page.wait_for_timeout(2500)
            await step(page, "work2")
            out["new_chat_click"] = await click_btn(page, "create_conversation_button")
            await page.wait_for_timeout(3500)
            await step(page, "new_chat_after")

            OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            print("[DUMP]", OUT, flush=True)
        finally:
            await ctx.close()

asyncio.run(main())

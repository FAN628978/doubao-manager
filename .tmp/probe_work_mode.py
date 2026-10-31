"""Read-only probe: how does Doubao's 对话/工作 mode switch render, and how to get back to 对话?

Never sends a prompt, never spends quota. Writes JSON + screenshots to .tmp/.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "probe_work_mode.json"
SHOT = ROOT / ".tmp" / "work_mode"
ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "acc_ae35c364b1"

DUMP = r"""() => {
  const vis = (e) => { if (!e) return false; const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = (e) => ((e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim());
  const sw = document.querySelector('[data-testid="conversation-mode-switch"]');
  const switchInfo = sw ? {
    visible: vis(sw), text: txt(sw), html: sw.outerHTML.slice(0, 4000),
    buttons: Array.from(sw.querySelectorAll('button,[role="button"],[role="tab"],[role="radio"]'))
      .map((b, i) => ({i, tag: b.tagName, testid: b.getAttribute('data-testid') || '',
        role: b.getAttribute('role') || '', text: txt(b).slice(0, 40),
        aria: b.getAttribute('aria-label') || '', checked: b.getAttribute('aria-selected') || b.getAttribute('aria-checked') || '',
        visible: vis(b)})),
    children: Array.from(sw.children).slice(0, 10).map(c => ({tag: c.tagName,
      testid: c.getAttribute('data-testid') || '', text: txt(c).slice(0, 40), cls: (c.className || '').toString().slice(0, 120)}))
  } : null;
  const nav = Array.from(document.querySelectorAll('nav [data-testid], nav button, [data-testid="chat_route_layout_leftside_nav"] [data-testid]'))
    .slice(0, 40).map(e => ({tag: e.tagName, testid: e.getAttribute('data-testid') || '',
      text: txt(e).slice(0, 40), visible: vis(e)}));
  const workish = Array.from(document.querySelectorAll('[data-testid]'))
    .filter(e => /office|work|task/i.test(e.getAttribute('data-testid') || ''))
    .slice(0, 30).map(e => ({testid: e.getAttribute('data-testid'), text: txt(e).slice(0, 50), visible: vis(e)}));
  const composer = document.querySelector('[data-testid="chat_input"]');
  return {url: location.href, title: document.title,
    switch: switchInfo, nav, workish,
    hasChatInput: !!composer && vis(composer),
    hasVideoSkill: Array.from(document.querySelectorAll('[data-testid^="skill_bar_button"]'))
      .some(b => /视频生成/.test(txt(b)) && vis(b)),
    guidance: (() => { const g = document.querySelector('[data-testid="flow_chat_guidance_page"]');
      return g ? {visible: vis(g), text: txt(g).slice(0, 120)} : null; })(),
    bodyHead: txt(document.body).slice(0, 400)};
}"""


async def snap(page, name, out):
    data = await page.evaluate(DUMP)
    out[name] = data
    await page.screenshot(path=str(SHOT.with_name(SHOT.name + "_" + name + ".png")))
    print(f"[{name}] url={data['url']} chatInput={data['hasChatInput']} videoSkill={data['hasVideoSkill']} "
          f"switch={bool(data['switch'])} guidance={bool(data['guidance'])}", flush=True)


async def click_text(page, scope_sel, text):
    """Click the first visible element whose exact text is `text` inside scope."""
    try:
        loc = page.locator(scope_sel).get_by_text(text, exact=True)
        n = await loc.count()
        for i in range(min(n, 8)):
            el = loc.nth(i)
            if await el.is_visible():
                await el.click()
                return True
    except Exception as exc:
        print("click error", text, type(exc).__name__, str(exc)[:120], flush=True)
    return False


async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3")
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT profile_dir, name FROM accounts WHERE id=?", (ACCOUNT,)).fetchone()
    if not row:
        raise SystemExit("account not found")
    out = {"account": dict(row)}
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=row["profile_dir"], channel="chrome", headless=False,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://www.doubao.com/", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(9000)
            await snap(page, "home", out)

            # 1) Can we enter 工作 from the switch, and what changes?
            clicked_work = await click_text(page, '[data-testid="conversation-mode-switch"]', "工作")
            out["clicked_work_tab"] = clicked_work
            if not clicked_work:
                out["clicked_work_fallback"] = await click_text(page, "body", "工作")
            await page.wait_for_timeout(4000)
            await snap(page, "after_work", out)

            # 2) Reload the homepage: is 工作 the persisted landing mode?
            await page.goto("https://www.doubao.com/", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(8000)
            await snap(page, "reload_after_work", out)

            # 3) Recovery A: sidebar 新对话
            recover_new_chat = await click_text(page, '[data-testid="chat_route_layout_leftside_nav"], body', "新对话")
            out["clicked_new_chat"] = recover_new_chat
            await page.wait_for_timeout(4000)
            await snap(page, "after_new_chat", out)

            # 4) If still not chat, Recovery B: the 对话 segment on the switch
            if not (await page.evaluate(DUMP))["hasChatInput"]:
                d = await page.evaluate(DUMP)
                if d["switch"]:
                    out["clicked_chat_tab"] = await click_text(page, '[data-testid="conversation-mode-switch"]', "对话")
            else:
                out["clicked_chat_tab"] = "not-needed"
            await page.wait_for_timeout(4000)
            await snap(page, "after_chat_tab", out)

            OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            print("[DUMP]", OUT, flush=True)
        finally:
            await ctx.close()

asyncio.run(main())

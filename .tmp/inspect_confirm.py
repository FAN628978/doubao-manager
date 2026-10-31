"""Probe #7: how is Doubao's parameter-confirmation actually answered?

Dumps the trailing assistant message's controls and the video-mode composer state.
Read-only, no quota.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "probe_confirm.json"
URL = sys.argv[1]
ACCOUNT = sys.argv[2] if len(sys.argv) > 2 else "acc_9b661e5a38"

DUMP = r"""() => {
  const msgs = Array.from(document.querySelectorAll('[data-testid="receive_message"]'));
  const last = msgs.at(-1);
  const ctl = (e) => ({tag: e.tagName, text: (e.innerText || '').trim().slice(0, 24),
    aria: e.getAttribute('aria-label') || '', testid: e.getAttribute('data-testid') || '',
    disabled: e.disabled === true || e.getAttribute('aria-disabled') === 'true',
    cls: String(e.className || '').slice(0, 60)});
  const composer = document.querySelector('[data-testid="chat_input"]');
  const editables = Array.from(document.querySelectorAll('[contenteditable="true"],textarea')).map(e => ({
    tag: e.tagName, ce: e.getAttribute('contenteditable'), cls: String(e.className || '').slice(0, 60),
    testid: e.getAttribute('data-testid') || '', insideComposer: !!(e.closest && e.closest('[data-testid="chat_input"]')),
    visible: (() => { const r = e.getBoundingClientRect(); return r.width > 4 && r.height > 4; })()}));
  const sends = Array.from(document.querySelectorAll('[data-testid="send_message"],[data-testid="chat_input_send_button"],button[type="submit"],button[aria-label*="发送"]')).map(e => ({
    ...ctl(e), insideComposer: !!(e.closest && e.closest('[data-testid="chat_input"]'))}));
  return {url: location.href, messageCount: msgs.length,
    lastMessage: last ? {text: (last.innerText || '').replace(/\s+/g,' ').slice(0, 600),
      buttons: Array.from(last.querySelectorAll('button,[role="button"],[data-testid]')).map(ctl).slice(0, 20),
      htmlHead: (last.innerHTML || '').slice(0, 900)} : null,
    composer: composer ? {present: true, htmlHead: (composer.innerHTML || '').slice(0, 500)} : {present: false},
    editables, sends};
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
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(9000)
        await page.mouse.wheel(0, 2500)
        await page.wait_for_timeout(1500)
        out = await page.evaluate(DUMP)
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[DUMP]", OUT, "| messages=", out["messageCount"])
        await ctx.close()

asyncio.run(main())

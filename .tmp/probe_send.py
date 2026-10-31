"""Probe #8: why does the confirmation message never reach Doubao?

Replays perform() up to the parameter-confirmation prompt, dumps the composer topology
(there may be a stale guidance composer plus the live conversation composer), then
attempts the confirm send exactly like the adapter and reports what changed.
"""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from web_api import (EDITOR_SELECTORS, SUBMIT_SELECTORS, VideoRequest, combined_video_settings,  # noqa: E402
                     read_composer_state, read_state, set_ui_option, target_page_url)
import web_api  # noqa: E402

ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "acc_9b661e5a38"
PROMPT = "电影级3D半写实国漫，雨后城楼远景，云雾缓慢流动，摄影机缓慢推进，暖色夕阳光。"
OUT = ROOT / ".tmp" / "probe_send.json"

TOPOLOGY = r"""() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 4 && r.height > 4; };
  const composers = Array.from(document.querySelectorAll('[data-testid="chat_input"]')).map((c, i) => ({
    index: i, visible: vis(c),
    text: (c.innerText || '').replace(/\s+/g, ' ').slice(0, 80),
    editor: (() => { const e = c.querySelector('[contenteditable="true"],textarea');
      return e ? {tag: e.tagName, cls: String(e.className).slice(0, 50),
                  text: (e.innerText || e.value || '').slice(0, 60), connected: e.isConnected} : null; })(),
    send: (() => { const s = c.querySelector('[data-testid="chat_input_send_button"]');
      return s ? {found: true, enabled: !(s.disabled || s.getAttribute('aria-disabled') === 'true'),
                  cls: String(s.className).slice(0, 70)} : {found: false}; })()}));
  const orphanEditors = Array.from(document.querySelectorAll('[contenteditable="true"],textarea'))
    .filter(e => !(e.closest && e.closest('[data-testid="chat_input"]')))
    .map(e => ({tag: e.tagName, cls: String(e.className).slice(0, 50), visible: vis(e),
                text: (e.innerText || e.value || '').slice(0, 40)}));
  const sendRows = document.querySelectorAll('[data-testid="send_message"]').length;
  const receive = document.querySelectorAll('[data-testid="receive_message"]').length;
  const cards = document.querySelectorAll('[class*="block-video"]').length;
  return {url: location.href, composerCount: composers.length, composers, orphanEditors, sendRows, receive, cards};
}"""


async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3")
    con.row_factory = sqlite3.Row
    profile = con.execute("SELECT profile_dir FROM accounts WHERE id=?", (ACCOUNT,)).fetchone()["profile_dir"]
    out = {"steps": []}
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile, channel="chrome", headless=False,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        page = await ctx.new_page()
        try:
            await page.goto(target_page_url(), wait_until="domcontentloaded", timeout=60000)
            await page.bring_to_front()
            await page.wait_for_selector('[data-testid="chat_input"], [data-testid="chat_input_input"]',
                                         state="visible", timeout=20000)
            await page.wait_for_timeout(1200)
            for kind, value in (("mode", "text2video"), ("ratio", "16:9"), ("duration", 4)):
                await set_ui_option(page, kind, value)
            out["steps"].append("settings ok")
            editor = await page.locator(EDITOR_SELECTORS[0]).first if await page.locator(EDITOR_SELECTORS[0]).count() \
                else page.locator(EDITOR_SELECTORS[3]).first
            await editor.fill(PROMPT)
            out["beforeSubmit"] = await page.evaluate(TOPOLOGY)
            submit = page.locator(SUBMIT_SELECTORS[0])
            out["steps"].append(f"submit selector matched {await submit.count()} element(s)")
            await submit.first.click()
            # wait for the parameter-confirmation reply
            for _ in range(30):
                await page.wait_for_timeout(1000)
                st = await read_state(page)
                if st["awaitingConfirm"]:
                    out["steps"].append("confirm prompt seen at last message")
                    break
            else:
                out["steps"].append("no confirm prompt within 30s")
            out["beforeConfirm"] = await page.evaluate(TOPOLOGY)
            out["beforeConfirmComposer"] = await read_composer_state(page)
            how = await web_api.confirm_pending_generation(page)
            out["confirmHow"] = how
            await page.wait_for_timeout(4000)
            out["afterConfirm"] = await page.evaluate(TOPOLOGY)
            out["afterConfirmComposer"] = await read_composer_state(page)
            await page.screenshot(path=str(ROOT / ".tmp" / "send_after_confirm.png"))
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
        finally:
            OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            print("[DUMP]", OUT)
            for s in out.get("steps", []):
                print("  step:", s)
            if "error" in out:
                print("  ERROR:", out["error"])
            print("  confirmHow:", out.get("confirmHow"))
            for key in ("beforeConfirm", "afterConfirm"):
                t = out.get(key)
                if t:
                    print(f"  {key}: url={t['url']} composers={t['composerCount']} "
                          f"sendRows={t['sendRows']} receive={t['receive']} cards={t['cards']}")
                    for c in t["composers"]:
                        print("     composer", c["index"], "visible=", c["visible"],
                              "editorText=", (c["editor"] or {}).get("text", "")[:30],
                              "connected=", (c["editor"] or {}).get("connected"), "send=", c["send"])
                    if t["orphanEditors"]:
                        print("     orphans:", t["orphanEditors"])
            print("  composerPrompt before/after:",
                  (out.get("beforeConfirmComposer") or {}).get("prompt", "")[:30], "->",
                  (out.get("afterConfirmComposer") or {}).get("prompt", "")[:30])
            await ctx.close()

asyncio.run(main())

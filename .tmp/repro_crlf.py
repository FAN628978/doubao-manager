"""Repro: does fill() + innerText readback on a contenteditable preserve CRLF?

Mimics the Doubao composer (a contenteditable div) and the exact read_composer_state()
readback, feeding it the real failing prompt (CRLF line endings). If the readback loses
the '\r', we have the root cause of PROMPT_UNVERIFIED.
"""
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]

HTML = r'''<!doctype html><html><head><meta charset="utf-8"/></head><body>
<div data-testid="chat_input">
  <div data-testid="chat_input_input"><div contenteditable="true" id="editor"></div></div>
  <button data-testid="chat_input_send_button" type="submit">发送</button>
</div>
</body></html>'''

READBACK = r"""() => {
  const composer = document.querySelector('[data-testid="chat_input"]');
  const scope = composer || document;
  const editor = scope.querySelector('[data-testid="chat_input_input"] [contenteditable="true"],'
    + '[role="textbox"][contenteditable="true"],div[contenteditable="true"],textarea');
  const prompt = editor ? ('value' in editor ? editor.value : editor.innerText || editor.textContent || '') : '';
  return {prompt: prompt.trim(), innerText: editor ? editor.innerText : null,
          textContent: editor ? editor.textContent : null,
          html: editor ? editor.innerHTML.slice(0, 400) : null};
}"""


def diff_pos(a, b):
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n if len(a) != len(b) else -1


async def main():
    prompt = (ROOT / ".tmp" / "prompt_translated.txt").read_text(encoding="utf-8")
    # short single-line control (the kind that succeeded before)
    short = "电影级3D半写实国漫，雨后城楼远景"
    # short multi-line CRLF control
    multi = "Visual_Style:\r\nline two\r\nline three"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        await page.set_content(HTML)
        editor = page.locator('[data-testid="chat_input_input"] [contenteditable="true"]')

        for name, text in (("short", short), ("multi_crlf", multi), ("real_prompt", prompt)):
            await editor.fill(text)
            rb = await page.evaluate(READBACK)
            got = rb["prompt"]
            want = text.strip()
            i = diff_pos(got, want)
            print(f"=== {name} ===")
            print(f"  want len={len(want)} got len={len(got)} equal={got == want}")
            if i >= 0:
                print(f"  first diff at {i}:")
                print(f"    want[{i-8}:{i+8}] = {want[max(0,i-8):i+8]!r}")
                print(f"    got [{i-8}:{i+8}] = {got[max(0,i-8):i+8]!r}")
            # report line-ending profile of the readback
            print(f"  got CRLF={got.count(chr(13)+chr(10))} bare LF={got.count(chr(10))-got.count(chr(13)+chr(10))} bare CR={got.count(chr(13))-got.count(chr(13)+chr(10))}")
            print(f"  innerText CRLF={rb['innerText'].count(chr(13)+chr(10))} bareLF={(rb['innerText'].count(chr(10))-rb['innerText'].count(chr(13)+chr(10)))}")
            print(f"  textContent CRLF={rb['textContent'].count(chr(13)+chr(10))} bareLF={(rb['textContent'].count(chr(10))-rb['textContent'].count(chr(13)+chr(10)))}")
        await browser.close()

asyncio.run(main())

"""Repro part 3: is LF-normalized input round-tripped exactly by fill()+innerText?

Goal: confirm the fix = normalize CRLF->LF (and any lone CR) before fill(), so the
strict equality passes for multi-line prompts with blank lines.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]

HTML = r'''<!doctype html><html><head><meta charset="utf-8"/></head><body>
<div data-testid="chat_input">
  <div data-testid="chat_input_input"><div contenteditable="true" id="editor"></div></div>
</div>
</body></html>'''

READ = r"""() => {
  const e = document.getElementById('editor');
  return {innerText: e.innerText, textContent: e.textContent};
}"""


async def main():
    # clean, hand-built LF-only string with blank lines
    samples = {
        "two_lines": "line one\nline two",
        "blank_line": "a\n\nb\n\nc",
        "crlf": "a\r\nb\r\nc",
        "lone_cr": "a\rb\rc",
        "real_lf": None,  # filled below from DB, normalized
    }
    import sqlite3, re
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3")
    con.row_factory = sqlite3.Row
    raw = con.execute("SELECT prompt FROM jobs WHERE id='job_c6cb739ef8a5'").fetchone()["prompt"]
    MENTION_RE = re.compile(r"@图(\d+)")
    translated = MENTION_RE.sub(lambda m: f"参考图{int(m.group(1))}", raw)
    samples["real_lf"] = translated.replace("\r\n", "\n").replace("\r", "\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        await page.set_content(HTML)
        editor = page.locator("#editor")
        for name, text in samples.items():
            await editor.fill(text)
            rb = await page.evaluate(READ)
            got = rb["innerText"].strip()
            want = text.strip()
            equal = got == want
            print(f"=== {name} ===  equal={equal}")
            if not equal:
                n = min(len(got), len(want))
                i = next((k for k in range(n) if got[k] != want[k]), n)
                print(f"  want len={len(want)} got len={len(got)} first diff@{i}:")
                print(f"    want[{max(0,i-10)}:{i+10}] = {want[max(0,i-10):i+10]!r}")
                print(f"    got [{max(0,i-10)}:{i+10}] = {got[max(0,i-10):i+10]!r}")
        await browser.close()

asyncio.run(main())

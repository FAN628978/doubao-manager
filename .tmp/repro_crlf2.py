"""Repro part 2: understand the contenteditable's DOM after fill() with long multi-line text.

Checks: (a) CRLF->LF normalization, (b) whether innerText gains/loses lines vs input,
(c) how the DOM splits lines (div vs br), (d) effect of white-space:pre-wrap.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]

HTML = r'''<!doctype html><html><head><meta charset="utf-8"/></head><body>
<div data-testid="chat_input">
  <div data-testid="chat_input_input"><div contenteditable="true" id="editor" style="white-space:__WS__"></div></div>
</div>
</body></html>'''

READ = r"""() => {
  const e = document.getElementById('editor');
  const html = e.innerHTML;
  return {
    innerText: e.innerText,
    textContent: e.textContent,
    innerTextLF: (e.innerText.match(/\n/g)||[]).length,
    htmlLen: html.length,
    divCount: (html.match(/<div/g)||[]).length,
    brCount: (html.match(/<br\b/g)||[]).length,
    pCount: (html.match(/<p\b/g)||[]).length,
    htmlHead: html.slice(0, 500),
  };
}"""


async def run(ws):
    prompt = (ROOT / ".tmp" / "prompt_translated.txt").read_text(encoding="utf-8")  # LF (read_text normalizes)
    # rebuild CRLF from the DB to be exact
    import sqlite3
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3")
    con.row_factory = sqlite3.Row
    raw = con.execute("SELECT prompt FROM jobs WHERE id='job_c6cb739ef8a5'").fetchone()["prompt"]
    import re
    MENTION_RE = re.compile(r"@图(\d+)")
    crlf = MENTION_RE.sub(lambda m: f"参考图{int(m.group(1))}", raw)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        await page.set_content(HTML.replace("__WS__", ws))
        editor = page.locator("#editor")
        for name, text in (("lf", prompt), ("crlf", crlf)):
            await editor.fill(text)
            rb = await page.evaluate(READ)
            want_lf = text.count("\n")
            print(f"=== ws={ws} input={name} ===")
            print(f"  input len={len(text)} LF={want_lf} CRLF={text.count(chr(13)+chr(10))}")
            print(f"  innerText len={len(rb['innerText'])} LF={rb['innerTextLF']} "
                  f"divs={rb['divCount']} brs={rb['brCount']} ps={rb['pCount']} htmlLen={rb['htmlLen']}")
            print(f"  htmlHead={rb['htmlHead']!r}")
        await browser.close()

asyncio.run(run("normal"))
asyncio.run(run("pre-wrap"))

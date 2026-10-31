"""Zero-quota pre-flight: run the adapter's opening + settings stage against real Doubao.

Replicates perform()'s page-open timing and the three set_ui_option() calls, then stops
before anything is typed or submitted, so no generation quota is spent.
"""
import asyncio
import json
import sqlite3
import sys
import traceback
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from web_api import set_ui_option, target_page_url, combined_video_settings  # noqa: E402

ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "acc_9b661e5a38"
OUT = ROOT / ".tmp" / "preflight.json"


async def main():
    con = sqlite3.connect(ROOT / "data" / "manager.sqlite3")
    con.row_factory = sqlite3.Row
    profile = con.execute("SELECT profile_dir FROM accounts WHERE id=?", (ACCOUNT,)).fetchone()["profile_dir"]
    result = {"steps": []}
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile, channel="chrome", headless=False,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        page = await ctx.new_page()

        def record(step, ok, detail=""):
            result["steps"].append({"step": step, "ok": ok, "detail": str(detail)[:300]})
            print(f"[{'OK ' if ok else 'ERR'}] {step} {detail if not ok else ''}")

        try:
            await page.goto(target_page_url(), wait_until="domcontentloaded", timeout=60000)
            await page.bring_to_front()
            try:
                await page.wait_for_selector('[data-testid="chat_input"], [data-testid="chat_input_input"]',
                                             state="visible", timeout=20000)
                record("composer-ready", True)
            except Exception as exc:
                record("composer-ready", False, exc)
            await page.wait_for_timeout(1200)
            for kind, value in (("mode", "text2video"), ("ratio", "16:9"), ("duration", 4)):
                try:
                    await set_ui_option(page, kind, value)
                    record(f"set-{kind}={value}", True)
                except Exception as exc:
                    record(f"set-{kind}={value}", False, f"{type(exc).__name__}: {exc}")
                    break
            panel = await combined_video_settings(page)
            result["panelText"] = (await panel.inner_text()) if panel else None
            print("[PANEL]", result["panelText"])
            await page.screenshot(path=str(ROOT / ".tmp" / "preflight.png"))
        finally:
            OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            await ctx.close()

asyncio.run(main())

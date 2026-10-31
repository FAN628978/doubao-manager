# -*- coding: utf-8 -*-
"""用修复后的 web_api 走真实 UI：进入视频模式 → 比例 → 时长 → 模型。不提交、不消耗额度。"""
import asyncio, io, json, sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "data" / "profiles" / "acc_4f61660da1"
OUT = ROOT / ".tmp" / "diag"
sys.path.insert(0, str(ROOT))
import web_api


async def panel_text(page):
    loc = page.locator(web_api.SETTINGS_PANEL)
    if not await loc.count():
        return None
    try:
        return (await loc.first.inner_text() or "").strip()
    except Exception:
        return None


async def step(page, label, fn):
    print(f"\nSTEP {label}")
    try:
        await fn()
        print("  -> OK")
    except Exception as e:
        print("  -> FAIL:", getattr(e, "code", type(e).__name__), str(e))
    print("  panel:", await panel_text(page))
    await page.screenshot(path=str(OUT / f"f_{label.replace(':', '_')}.png"))


async def main():
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto("https://www.doubao.com/", wait_until="domcontentloaded", timeout=60000)
            await page.bring_to_front()
            await page.wait_for_selector('[data-testid="chat_input"]', state="visible", timeout=20000)
            await page.wait_for_timeout(1500)
            print("初始 panel:", await panel_text(page))

            await step(page, "A_mode", lambda: web_api.set_ui_option(page, "mode", "reference_images"))
            await step(page, "B_ratio", lambda: web_api.set_ui_option(page, "ratio", "16:9"))
            await step(page, "C_duration9", lambda: web_api.set_ui_option(page, "duration", 9))
            await step(page, "D_model", lambda: web_api.select_model(page, "Seedance 2.0 Fast"))

            print("\nFINAL panel:", await panel_text(page))
            txt = await page.evaluate("""() => {
              const c = document.querySelector('[data-testid="chat_input"]');
              return c ? (c.innerText||'').replace(/\\n/g,' | ') : null;
            }""")
            print("composer:", txt)
            await page.screenshot(path=str(OUT / "f_final.png"))
        finally:
            await ctx.close()
            print("\n浏览器已关闭（未提交）")


asyncio.run(main())

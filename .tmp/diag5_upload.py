# -*- coding: utf-8 -*-
"""新 UI 下验证参考图上传 + 提示词写入（不点提交、不消耗额度）。"""
import asyncio, io, json, sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "data" / "profiles" / "acc_ae35c364b1"
OUT = ROOT / ".tmp" / "diag"
sys.path.insert(0, str(ROOT))
import web_api

IMGS = [ROOT / "data/web_assets/asset_5a79386da6a44567.jpg",
        ROOT / "data/web_assets/asset_5843b04f0cda48ca.jpg"]


async def dump_attachments(page, label):
    data = await page.evaluate("""() => {
      const vis = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
      const composer = document.querySelector('[data-testid="chat_input"]');
      const out = {cards: [], testids: [], imgs: 0, text: composer ? (composer.innerText||'').replace(/\\n/g,' | ') : null};
      if (!composer) return out;
      composer.querySelectorAll('[data-testid]').forEach(e => {
        const id = e.getAttribute('data-testid');
        if (/attach|image|upload|file|asset|thumb|media/i.test(id)) out.testids.push(id);
      });
      composer.querySelectorAll('img').forEach(e => { if (vis(e)) out.imgs++; });
      return out;
    }""")
    print(f"  [{label}] composer:", json.dumps(data, ensure_ascii=False))
    await page.screenshot(path=str(OUT / f"u_{label}.png"))


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

            print("进入视频模式 + 参数…")
            await web_api.set_ui_option(page, "mode", "reference_images")
            await web_api.set_ui_option(page, "ratio", "16:9")
            await web_api.set_ui_option(page, "duration", 9)
            await web_api.select_model(page, "Seedance 2.0 Fast")
            print("  panel:", (await page.locator(web_api.SETTINGS_PANEL).first.inner_text()).strip())
            await dump_attachments(page, "before_upload")

            print("\n真实上传 2 张参考图…")
            try:
                await web_api.upload_slot(page, "references", IMGS)
                print("  -> UPLOAD OK")
            except Exception as e:
                print("  -> UPLOAD FAIL:", getattr(e, "code", type(e).__name__), str(e))
            await page.wait_for_timeout(1500)
            await dump_attachments(page, "after_upload")

            print("\n写入提示词（参考图1 红色纸船，参考图2 蓝色湖面）…")
            editor = await web_api.first_visible(page, web_api.EDITOR_SELECTORS)
            print("  editor:", "FOUND" if editor else "None")
            if editor:
                text = "参考图1 的红色纸船作为主体，参考图2 的蓝色湖面作为背景，缓缓漂动"
                await editor.fill(text)
                state = await web_api.read_composer_state(page)
                print("  prompt readback:", repr(state.get("prompt")))
                print("  equivalent:", web_api.prompts_equivalent(state.get("prompt"), text))
            await dump_attachments(page, "after_prompt")
        finally:
            await ctx.close()
            print("\n浏览器已关闭（未提交、未消耗额度）")


asyncio.run(main())

# -*- coding: utf-8 -*-
"""进入视频模式后，逐步验证 比例/时长/模型 的真实 UI 流程。不提交。"""
import asyncio, io, json, sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "data" / "profiles" / "acc_ae35c364b1"
OUT = ROOT / ".tmp" / "diag"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
import web_api


async def panel_text(page):
    loc = page.locator('[data-input-engine-actionbar-render-entry-key="video-generation-params-panel"]')
    if await loc.count() == 0:
        return None
    try:
        return (await loc.first.inner_text() or "").strip()
    except Exception:
        return None


async def enter_video(page):
    more = await web_api.first_visible(page, ['[data-testid="skill_bar_button_more"]'])
    print("更多技能按钮:", "FOUND" if more else "None")
    if more:
        await more.click()
        await page.wait_for_timeout(800)
        btn = await web_api.first_visible(page, ['button:text-is("视频生成")',
                                                 '[role="menuitem"]:text-is("视频生成")'])
        print("菜单内视频生成按钮:", "FOUND" if btn else "None")
        if btn:
            await btn.click()
            await page.wait_for_timeout(1500)
            return True
    return False


async def main():
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            accept_downloads=True, no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto("https://www.doubao.com/", wait_until="domcontentloaded", timeout=60000)
            await page.bring_to_front()
            await page.wait_for_selector('[data-testid="chat_input"]', state="visible", timeout=20000)
            await page.wait_for_timeout(1200)

            print("STEP A: 进入视频模式")
            print("  entered:", await enter_video(page))
            print("  panel:", await panel_text(page))
            await page.screenshot(path=str(OUT / "t_a_mode.png"))

            print("\nSTEP B: set_ui_option(mode, reference_images)")
            try:
                await web_api.set_ui_option(page, "mode", "reference_images")
                print("  OK, panel:", await panel_text(page))
            except Exception as e:
                print("  FAIL:", getattr(e, "code", type(e).__name__), e)
            await page.screenshot(path=str(OUT / "t_b_mode_set.png"))

            print("\nSTEP C: set_ui_option(ratio, 16:9)")
            try:
                await web_api.set_ui_option(page, "ratio", "16:9")
                print("  OK, panel:", await panel_text(page))
            except Exception as e:
                print("  FAIL:", getattr(e, "code", type(e).__name__), e)
            await page.screenshot(path=str(OUT / "t_c_ratio.png"))

            print("\nSTEP D: set_ui_option(duration, 9)")
            try:
                await web_api.set_ui_option(page, "duration", 9)
                print("  OK, panel:", await panel_text(page))
            except Exception as e:
                print("  FAIL:", getattr(e, "code", type(e).__name__), e)
            await page.screenshot(path=str(OUT / "t_d_duration.png"))

            print("\nSTEP E: 打开参数面板，列出真实可选比例/时长")
            el = await web_api.combined_video_settings(page)
            print("  panel element:", "FOUND" if el else "None")
            if el is not None:
                await el.click()
                await page.wait_for_timeout(1000)
                items = await page.evaluate("""() => {
                  const vis = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
                  const out = [];
                  for (const e of document.querySelectorAll('[role="option"],[role="radio"],[role="menuitem"],button')) {
                    if (!vis(e)) continue;
                    const t = (e.innerText||'').trim();
                    if (t && t.length <= 12 && /(\\d+\\s*[s秒]|16:9|9:16|1:1|自动|高清|标清|4:3|3:4)/.test(t))
                      out.push({tag:e.tagName, role:e.getAttribute('role')||'', text:t,
                                checked:e.getAttribute('aria-checked'), selected:e.getAttribute('aria-selected')});
                  }
                  return out;
                }""")
                print(json.dumps(items, ensure_ascii=False, indent=1))
                await page.screenshot(path=str(OUT / "t_e_params_popover.png"))
                await page.keyboard.press("Escape")

            print("\nSTEP F: select_model(Seedance 2.0 Fast)")
            try:
                await web_api.select_model(page, "Seedance 2.0 Fast")
                print("  OK")
            except Exception as e:
                print("  FAIL:", getattr(e, "code", type(e).__name__), e)
            await page.screenshot(path=str(OUT / "t_f_model.png"))
        finally:
            await ctx.close()
            print("\n浏览器已关闭（未提交、未消耗额度）")


asyncio.run(main())

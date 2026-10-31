# -*- coding: utf-8 -*-
"""逐步复现 app 的「配置视频模式」流程，定位 SETTING_NOT_FOUND 的真实原因。
不提交任务、不消耗额度。"""
import asyncio, io, json, re, sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "data" / "profiles" / "acc_ae35c364b1"
OUT = ROOT / ".tmp" / "diag"
OUT.mkdir(parents=True, exist_ok=True)
VIDEO_PAGE = "https://www.doubao.com/"

sys.path.insert(0, str(ROOT))


async def visible_texts(page, selector):
    out = []
    loc = page.locator(selector)
    n = min(await loc.count(), 60)
    for i in range(n):
        el = loc.nth(i)
        try:
            if await el.is_visible():
                out.append((await el.inner_text() or "").strip()[:80])
        except Exception:
            pass
    return out


async def dump(page, label):
    print(f"\n{'='*30} {label} {'='*30}")
    print("URL:", page.url)
    print("TITLE:", await page.title())
    info = await page.evaluate("""() => {
      const vis = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
      const q = s => Array.from(document.querySelectorAll(s));
      const skills = q('[data-testid^="skill_bar_button_"]').map(e => ({
        testid: e.getAttribute('data-testid'), text: (e.innerText||'').trim(), visible: vis(e)
      }));
      const video = q('*').filter(e => vis(e) && (e.innerText||'').trim() === '视频生成').slice(0,10).map(e => ({
        tag: e.tagName, testid: e.getAttribute('data-testid'),
        cls: (e.className||'').toString().slice(0,80),
        parentTestid: (e.parentElement && e.parentElement.getAttribute('data-testid')) || ''
      }));
      const more = q('*').filter(e => vis(e) && (e.innerText||'').trim() === '更多技能').slice(0,10).map(e => ({
        tag: e.tagName, testid: e.getAttribute('data-testid'),
        cls: (e.className||'').toString().slice(0,80),
        parentTestid: (e.parentElement && e.parentElement.getAttribute('data-testid')) || ''
      }));
      const panel = q('[data-input-engine-actionbar-render-entry-key="video-generation-params-panel"]')
        .map(e => ({testid: e.getAttribute('data-input-engine-actionbar-render-entry-key'), text: (e.innerText||'').trim(), visible: vis(e)}));
      const composer = document.querySelector('[data-testid="chat_input"]');
      const editable = document.querySelector('[data-testid="chat_input_input"] [contenteditable="true"]');
      const actionbar = composer ? (composer.innerText||'').replace(/\\n/g,' | ').slice(0,300) : null;
      const entryKeys = q('[data-input-engine-actionbar-render-entry-key]').map(e => ({
        key: e.getAttribute('data-input-engine-actionbar-render-entry-key'),
        text: (e.innerText||'').trim().slice(0,60), visible: vis(e)
      }));
      return {skills, video, more, panel, composerVisible: vis(composer), editable: !!editable,
              actionbar, entryKeys};
    }""")
    print(json.dumps(info, ensure_ascii=False, indent=1))
    shot = OUT / f"{label}.png"
    await page.screenshot(path=str(shot), full_page=False)
    print("screenshot:", shot)


async def main():
    import web_api

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            accept_downloads=True, no_viewport=True, locale="zh-CN",
            args=["--start-maximized"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            print("STEP 1: goto", VIDEO_PAGE)
            await page.goto(VIDEO_PAGE, wait_until="domcontentloaded", timeout=60000)
            await page.bring_to_front()

            print("STEP 2: wait chat_input visible (20s)")
            try:
                await page.wait_for_selector(
                    '[data-testid="chat_input"], [data-testid="chat_input_input"]',
                    state="visible", timeout=20000)
                print("  -> chat_input visible")
            except Exception as e:
                print("  -> chat_input NOT visible:", type(e).__name__)

            await page.wait_for_timeout(1500)
            await dump(page, "s1_home")

            print("\nSTEP 3: 调用 web_api.combined_video_settings(page)")
            el = await web_api.combined_video_settings(page)
            print("  ->", "FOUND: " + (await el.inner_text()) if el is not None else "None")

            print("\nSTEP 4: 代码原逻辑 —— first_visible(skill_bar_button_17)")
            launch = await web_api.first_visible(page, ['[data-testid="skill_bar_button_17"]'])
            print("  -> skill_bar_button_17:", "FOUND" if launch else "None")

            print("\nSTEP 5: 找可见的「视频生成」文字")
            chips = page.get_by_text("视频生成", exact=False)
            n = min(await chips.count(), 10)
            found_visible = False
            for i in range(n):
                c = chips.nth(i)
                try:
                    vis = await c.is_visible()
                    box = await c.bounding_box() if vis else None
                    is_launcher = await c.evaluate(
                        "e => !!e.closest('[data-testid=\\\"skill_bar_button_17\\\"]')")
                    print(f"  chip[{i}] visible={vis} box={box} launcher={is_launcher}")
                    if vis:
                        found_visible = True
                except Exception as e:
                    print(f"  chip[{i}] err {e}")
            print("  -> 可见视频生成:", found_visible)

            print("\nSTEP 6: 真实点击「更多技能」看看菜单")
            more = None
            texts = page.get_by_text("更多技能", exact=False)
            for i in range(min(await texts.count(), 6)):
                t = texts.nth(i)
                try:
                    if await t.is_visible():
                        more = t
                        break
                except Exception:
                    pass
            if more is None:
                print("  -> 未找到「更多技能」")
            else:
                box = await more.bounding_box()
                print("  -> 点击 更多技能 box=", box)
                await more.click()
                await page.wait_for_timeout(1500)
                await dump(page, "s2_more_menu")
                print("\n  菜单项文本（role=menuitem/option/button 可见）:")
                for sel in ('[role="menuitem"]', '[role="option"]', '[role="menuitemradio"]'):
                    print(f"   {sel}:", await visible_texts(page, sel))
                # 试试在弹出层里点「视频生成」
                items = page.get_by_text("视频生成", exact=True)
                clicked = False
                for i in range(min(await items.count(), 10)):
                    it = items.nth(i)
                    try:
                        if await it.is_visible():
                            await it.click()
                            clicked = True
                            print("  -> 点击了弹出菜单中的「视频生成」")
                            break
                    except Exception:
                        pass
                if not clicked:
                    print("  -> 弹出层中未找到可点的「视频生成」")
                await page.wait_for_timeout(2000)
                await dump(page, "s3_after_video_click")

                print("\nSTEP 7: 再调用 combined_video_settings")
                el2 = await web_api.combined_video_settings(page)
                print("  ->", "FOUND: " + (await el2.inner_text()) if el2 is not None else "None")

            print("\nSTEP 8: 直接调用 set_ui_option(mode) —— 与任务失败时完全一致的代码路径")
            try:
                await web_api.set_ui_option(page, "mode", "reference_images")
                print("  -> SET MODE OK")
            except Exception as e:
                print("  -> set_ui_option 抛错:", getattr(e, "code", type(e).__name__), str(e))
            await dump(page, "s4_final")
        finally:
            await ctx.close()
            print("\n浏览器已关闭")


asyncio.run(main())

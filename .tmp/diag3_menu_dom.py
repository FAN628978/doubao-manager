# -*- coding: utf-8 -*-
"""抓「更多技能」弹出层的真实 DOM 结构，为修复选择器取证。"""
import asyncio, io, json, sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "data" / "profiles" / "acc_ae35c364b1"
OUT = ROOT / ".tmp" / "diag"
sys.path.insert(0, str(ROOT))
import web_api


async def main():
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            no_viewport=True, locale="zh-CN", args=["--start-maximized"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto("https://www.doubao.com/", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_selector('[data-testid="chat_input"]', state="visible", timeout=20000)
            await page.wait_for_timeout(1500)

            print("点击 更多技能…")
            more = await web_api.first_visible(page, ['[data-testid="skill_bar_button_more"]'])
            await more.click()
            await page.wait_for_timeout(1800)
            await page.screenshot(path=str(OUT / "menu_open.png"))

            data = await page.evaluate("""() => {
              const vis = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
              const hits = [];
              for (const e of document.querySelectorAll('*')) {
                if (!vis(e)) continue;
                const t = (e.innerText||'').trim();
                if (t === '视频生成') {
                  let chain = [];
                  for (let p = e, i = 0; p && i < 6; p = p.parentElement, i++) {
                    chain.push({tag: p.tagName, testid: p.getAttribute('data-testid'),
                                role: p.getAttribute('role'),
                                cls: (p.className||'').toString().slice(0,120)});
                  }
                  const r = e.getBoundingClientRect();
                  hits.push({tag: e.tagName, role: e.getAttribute('role'),
                             cls: (e.className||'').toString().slice(0,120),
                             box: {x: r.x, y: r.y, w: r.width, h: r.height},
                             chain});
                }
              }
              // 弹层容器候选
              const popovers = [];
              for (const s of ['[role="dialog"]','[role="menu"]','[role="listbox"]','[data-testid*="popover"]','[data-testid*="dropdown"]','[data-testid*="menu"]']) {
                document.querySelectorAll(s).forEach(e => {
                  if (vis(e)) popovers.push({sel: s, tag: e.tagName,
                    testid: e.getAttribute('data-testid'), role: e.getAttribute('role'),
                    text: (e.innerText||'').replace(/\\n/g,' | ').slice(0,200)});
                });
              }
              const skills = Array.from(document.querySelectorAll('[data-testid^="skill_bar_button_"]'))
                .map(e => ({testid:e.getAttribute('data-testid'), text:(e.innerText||'').trim(), visible:vis(e)}));
              return {hits, popovers, skills};
            }""")
            print(json.dumps(data, ensure_ascii=False, indent=1))

            # 用 innerText 精确匹配的多种方式测试命中情况
            print("\n选择器命中测试:")
            for sel in ['button:text-is("视频生成")', 'li:text-is("视频生成")',
                        'span:text-is("视频生成")', 'text="视频生成"']:
                loc = page.locator(sel)
                cnt = await loc.count()
                visn = 0
                for i in range(min(cnt, 10)):
                    try:
                        if await loc.nth(i).is_visible(): visn += 1
                    except Exception: pass
                print(f"  {sel}: count={cnt} visible={visn}")

            # 点击命中项，进入视频模式，验证面板
            loc = page.locator('li:has-text("视频生成")').first
            if await loc.count():
                await loc.click()
                await page.wait_for_timeout(2000)
                panel = page.locator('[data-input-engine-actionbar-render-entry-key="video-generation-params-panel"]')
                print("点击后面板可见:", await panel.count() > 0 and await panel.first.is_visible())
                if await panel.count():
                    print("面板文本:", (await panel.first.inner_text()).strip())
                await page.screenshot(path=str(OUT / "menu_clicked_video.png"))
        finally:
            await ctx.close()
            print("\n浏览器已关闭（未提交）")


asyncio.run(main())

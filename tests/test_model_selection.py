"""Model selection regressions using local HTML, without live Doubao sessions."""
import asyncio

import pytest
from playwright.async_api import async_playwright

from test_v3 import launch_chromium
from doubao_manager.web.automation import select_model


HTML = """
<button id="model" data-testid="chat_input_action_model">Seedance 2.0</button>
<div id="menu" hidden>
  <button role="menuitem">Seedance 2.0 Fast</button>
</div>
<script>
const model = document.getElementById('model');
window.modelClicks = 0;
window.clickedWhileBusy = false;
model.onclick = () => {
  window.modelClicks++;
  window.clickedWhileBusy = model.disabled ||
    model.getAttribute('aria-busy') === 'true' ||
    model.getAttribute('data-disabled') === 'true';
  document.getElementById('menu').hidden = false;
};
document.querySelector('[role="menuitem"]').onclick = (event) => {
  model.textContent = event.target.textContent;
  document.getElementById('menu').hidden = true;
};
</script>
"""


def test_matching_model_skips_disabled_selector():
    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await browser.new_page()
                await page.set_content(HTML)
                await page.evaluate("""() => {
                  model.textContent = '  Seedance 2.0 Fast  ';
                  model.disabled = true;
                  model.setAttribute('aria-busy', 'true');
                  model.setAttribute('data-disabled', 'true');
                }""")
                await asyncio.wait_for(select_model(page, 'Seedance 2.0 Fast'), timeout=3)
                assert await page.evaluate('window.modelClicks') == 0
            finally:
                await browser.close()

    asyncio.run(run())


@pytest.mark.parametrize('blocking_attribute', ['disabled', 'aria-busy', 'data-disabled'])
def test_different_model_waits_for_selector(blocking_attribute):
    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await browser.new_page()
                await page.set_content(HTML)
                await page.evaluate("""attribute => {
                  model.setAttribute(attribute, 'true');
                  setTimeout(() => model.removeAttribute(attribute), 700);
                }""", blocking_attribute)
                await asyncio.wait_for(select_model(page, 'Seedance 2.0 Fast'), timeout=5)
                assert await page.locator('#model').inner_text() == 'Seedance 2.0 Fast'
                assert await page.evaluate('window.modelClicks') == 1
                assert await page.evaluate('window.clickedWhileBusy') is False
            finally:
                await browser.close()

    asyncio.run(run())


def test_model_becoming_current_while_disabled_skips_click():
    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await browser.new_page()
                await page.set_content(HTML)
                await page.evaluate("""() => {
                  model.disabled = true;
                  setTimeout(() => { model.textContent = 'Seedance 2.0 Fast'; }, 700);
                }""")
                await asyncio.wait_for(select_model(page, 'Seedance 2.0 Fast'), timeout=3)
                assert await page.evaluate('window.modelClicks') == 0
                assert await page.locator('#model').is_disabled()
            finally:
                await browser.close()

    asyncio.run(run())


@pytest.mark.parametrize('stable_selector', [True, False])
def test_video_model_takes_priority_over_disabled_chat_model(stable_selector):
    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await browser.new_page()
                await page.set_content(HTML)
                await page.evaluate("""stable => {
                  model.textContent = '快速';
                  model.disabled = true;
                  model.setAttribute('aria-busy', 'true');
                  const videoModel = document.createElement('button');
                  videoModel.id = 'video-model';
                  if (stable) videoModel.setAttribute(
                    'data-input-engine-actionbar-control-key', 'video-model');
                  videoModel.innerHTML = '<span>模型</span><span>Seedance 2.0 Fast</span>';
                  videoModel.onclick = () => { throw new Error('Matching video model was clicked'); };
                  document.body.appendChild(videoModel);
                }""", stable_selector)
                await asyncio.wait_for(select_model(page, 'Seedance 2.0 Fast'), timeout=3)
                assert await page.evaluate('window.modelClicks') == 0
            finally:
                await browser.close()

    asyncio.run(run())

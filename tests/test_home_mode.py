"""Offline regression for Doubao's 对话/工作 home switch (observed live 2026-10-29).

The 工作 home keeps [data-testid=chat_input] mounted but hides every video skill
chip, so the old "composer appeared" check let the job proceed and die later with
SETTING_NOT_FOUND. The selected segment exposes no aria state - only the active
button carries the exact ``text-dbx-text-primary`` class token - so the fixture
mirrors that shape. Real Chromium on a local synthetic page; never Doubao.
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web_api import ReviewNeeded, ensure_chat_mode, work_mode_active


async def launch_chromium(pw):
    exe = os.getenv('DOUBAO_TEST_CHROMIUM_EXE')
    if not exe and Path('/usr/bin/chromium').is_file():
        exe = '/usr/bin/chromium'
    options = dict(headless=True, args=['--no-sandbox'])
    if exe:
        options['executable_path'] = exe
    else:
        options['channel'] = 'chrome'
    return await pw.chromium.launch(**options)


SWITCH_HTML = r'''<!doctype html><html lang="zh"><head><meta charset="utf-8"/></head><body>
<div data-testid="conversation-mode-switch">
  <div class="indicator"></div>
  <button id="chat-btn" class="text-dbx-text-secondary hover:text-dbx-text-primary">对话</button>
  <button id="work-btn" class="font-medium text-dbx-text-primary">工作</button>
</div>
<div data-testid="flow_chat_guidance_page"><span id="heading">今天有什么工作要处理？</span></div>
<button data-testid="skill_bar_button_17" id="video-skill" hidden>视频生成</button>
<button data-testid="create_conversation_button" id="new-chat">新对话</button>
<div data-testid="chat_input"><div contenteditable="true"></div></div>
<script>
const chat=document.getElementById('chat-btn'), work=document.getElementById('work-btn');
function select(which){
  const isChat = which === 'chat';
  chat.className = isChat ? 'font-medium text-dbx-text-primary'
                          : 'text-dbx-text-secondary hover:text-dbx-text-primary';
  work.className = isChat ? 'text-dbx-text-secondary hover:text-dbx-text-primary'
                          : 'font-medium text-dbx-text-primary';
  document.getElementById('heading').textContent = isChat ? '有什么我能帮你的吗？' : '今天有什么工作要处理？';
  document.getElementById('video-skill').hidden = !isChat;
  window.__selected = which;
}
window.__selected='work'; window.__clicks=0;
chat.onclick=()=>{window.__clicks++; select('chat');};
work.onclick=()=>{window.__clicks++; select('work');};
document.getElementById('new-chat').onclick=()=>{window.__clicks++; select('chat');};
</script></body></html>'''

# Older rollout without the segmented control: only the work-home copy identifies it.
LEGACY_WORK_HTML = r'''<!doctype html><html lang="zh"><head><meta charset="utf-8"/></head><body>
<div data-testid="flow_chat_guidance_page"><span id="heading">今天有什么工作要处理？</span></div>
<button data-testid="create_conversation_button" id="new-chat">新对话</button>
<div data-testid="chat_input"><div contenteditable="true"></div></div>
<script>
window.__clicks=0;
document.getElementById('new-chat').onclick=()=>{window.__clicks++;
  document.getElementById('heading').textContent='有什么我能帮你的吗？';};
</script></body></html>'''

STUCK_WORK_HTML = r'''<!doctype html><html lang="zh"><head><meta charset="utf-8"/></head><body>
<div data-testid="flow_chat_guidance_page"><span>今天有什么工作要处理？</span></div>
<div data-testid="chat_input"><div contenteditable="true"></div></div>
</body></html>'''


def drive(html, action):
    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await page.set_content(html)
            try:
                return await action(page)
            finally:
                await browser.close()
    return asyncio.run(run())


def test_work_home_is_switched_back_to_chat():
    async def action(page):
        assert await work_mode_active(page) is True
        await ensure_chat_mode(page)
        return await page.evaluate(
            "() => ({selected: window.__selected, clicks: window.__clicks,"
            " videoHidden: document.getElementById('video-skill').hidden})")
    assert drive(SWITCH_HTML, action) == {'selected': 'chat', 'clicks': 1, 'videoHidden': False}


def test_chat_home_is_left_untouched():
    async def action(page):
        await page.evaluate("select('chat')")
        await page.evaluate("window.__clicks = 0")
        assert await work_mode_active(page) is False
        await ensure_chat_mode(page)
        return await page.evaluate("window.__clicks")
    assert drive(SWITCH_HTML, action) == 0


def test_legacy_work_home_recovers_via_new_chat():
    async def action(page):
        assert await work_mode_active(page) is True
        await ensure_chat_mode(page)
        return await page.evaluate(
            "() => ({heading: document.getElementById('heading').textContent,"
            " clicks: window.__clicks})")
    result = drive(LEGACY_WORK_HTML, action)
    assert result['heading'] == '有什么我能帮你的吗？'
    assert result['clicks'] == 1


def test_stuck_work_home_needs_review_instead_of_failing_later():
    async def action(page):
        with pytest.raises(ReviewNeeded) as err:
            await ensure_chat_mode(page)
        return err.value.code
    assert drive(STUCK_WORK_HTML, action) == 'WORK_MODE_ACTIVE'

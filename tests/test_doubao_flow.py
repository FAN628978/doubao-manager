"""Offline regression for the real Doubao conversation flow (observed 2026-10-28).

Two behaviours the adapter used to get wrong and must never regress:
  1. Doubao echoes the parsed parameters and waits for a confirmation before it spends
     quota - one confirmation answer, never a blind resend.
  2. The finished result is a cover-image card with no <video> in the DOM; the playable
     URL only exists after the card is opened.
Real Chromium on a local synthetic page; the video download is mocked. Never Doubao.
"""
import asyncio
import io
import os
import sys
from pathlib import Path

import pytest
from PIL import Image
from playwright.async_api import async_playwright, Page, APIRequestContext

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import web_api
from web_api import CONFIRM_ANSWER, ReviewNeeded, VideoRequest, perform, persisted_url
from doubao_manager.web import automation
from doubao_manager.web.utils import (
    CONFIRMATION_RESULT_POLL_DELAYS,
    confirmation_result_poll_interval,
)

VIDEO_URL = "https://v26-vdl.doubao.com/abc123/video/tos/cn/mock.mp4?mime_type=video_mp4&lr=video_gen_watermark_dyn"
CLEAN_URL = "https://v26-vdl.doubao.com/abc123/video/tos/cn/mock.mp4?mime_type=video_mp4&lr=unwatermarked"

HTML = r'''<!doctype html><html lang="zh"><head><meta charset="utf-8"/></head><body>
<script>window.__MODE='__MODE__';window.__sends=[];</script>
<select data-testid="video-mode" aria-label="生成模式"><option value="text2video">文生视频</option><option value="image2video">首帧图生视频</option></select>
<select data-testid="video-ratio" aria-label="画幅比例"><option>16:9</option><option>9:16</option></select>
<select data-testid="video-duration" aria-label="时长"><option value="4">4秒</option><option value="10">10秒</option></select>
<div data-testid="chat_input"><textarea id="prompt"></textarea><button type="submit">发送</button></div>
<div id="thread"><div data-testid="receive_message">你好，我是豆包。</div></div>
<!-- Real Doubao marks the sent-message ROW with data-testid="send_message"; it is not a
     control. Kept here so any code that treats it as the send button fails this test. -->
<div data-testid="send_message" style="position:fixed;left:700px;bottom:160px">已发送的消息</div>
<script>
const thread=document.getElementById('thread');
const editor=document.getElementById('prompt'), send=document.querySelector('[data-testid="chat_input"] button');
function say(html){const m=document.createElement('div');m.setAttribute('data-testid','receive_message');
  m.innerHTML=html;thread.appendChild(m);return m;}
let asked=false, confirmMsg=null;
function openDetail(){
  if(document.getElementById('detail-panel')) return;
  const aside=document.createElement('aside');
  aside.id='detail-panel';
  aside.setAttribute('data-testid','samantha_layout_right_side');
  aside.innerHTML='<div data-testid="canvas_panel_container">'
    + '<video src="''' + CLEAN_URL + r'''"></video>'
    + '<div data-testid="control_bar_action_list">'
    + '<button data-testid="edit_image_download_button">保存</button></div></div>';
  document.body.appendChild(aside);
  aside.querySelector('button').onclick=()=>{
    const bytes=new Uint8Array(4096);
    new DataView(bytes.buffer).setUint32(0,32);
    bytes.set([0x66,0x74,0x79,0x70,0x69,0x73,0x6f,0x6d],4);
    const a=document.createElement('a');
    a.href=URL.createObjectURL(new Blob([bytes],{type:'video/mp4'}));
    a.download='clean.mp4';
    document.body.appendChild(a);a.click();a.remove();
  };
}
function showCard(withPlayer){
  const m=say('你的视频生成好了。<div class="block-video-x"><img class="cover-x" src="https://p3.byteimg.com/cover.png"></div>');
  const card=m.querySelector('.block-video-x');
  if(window.__MODE==='detail_panel') card.onclick=openDetail;
  else if(withPlayer) card.onclick=()=>card.innerHTML='<video src="''' + VIDEO_URL + r'''"></video>';
}
send.addEventListener('click',()=>{
  const text=editor.value; window.__sends.push(text);
  const mode=window.__MODE;
  // Real Doubao may leave the contenteditable text in place until the send is acked.
  if(mode==='slowclear'&&asked) setTimeout(()=>{editor.value='';},900);
  else editor.value='';
  if(mode!=='needconfirm'&&mode!=='card_no_player'&&mode!=='slowclear'&&mode!=='mutateconfirm'&&mode!=='detail_panel') return;
  if(mode==='detail_panel'){setTimeout(()=>showCard(true),200);return;}
  if(!asked){asked=true;
    confirmMsg=say('视频生成参数确认<br>模型： Seedance 2.0 Mini<br>时长： 4 秒<br>确认后我再开始生成视频。');return;}
  if(mode==='mutateconfirm'){
    // The confirmation message is updated in place (timestamp/action bar) while it
    // still contains the old "确认后..." text; a text comparison would answer twice.
    let n=0;
    const timer=setInterval(()=>{
      confirmMsg.innerText='视频生成参数确认\n模型： Seedance 2.0 Mini\n时长： 4 秒\n确认后我再开始生成视频。\n今天 10:5'+(n++);
    },150);
    setTimeout(()=>{clearInterval(timer);
      say('本次使用 Seedance 2.0 Mini 生成，预计等待 5 分钟。视频生成好后我会发送给你。');
      setTimeout(()=>showCard(true),200);},1600);
    return;
  }
  say('本次使用 Seedance 2.0 Mini 生成，预计等待 5 分钟。视频生成好后我会发送给你。');
  setTimeout(()=>showCard(mode!=='card_no_player'),200);
});
</script></body></html>'''


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


class Downloaded:
    ok = True
    status = 200
    headers = {'content-length': '4096'}

    def __init__(self, blob, url):
        self.blob = blob

    async def body(self):
        return self.blob


def mp4_bytes():
    # Minimal container: padding + ftyp box, enough for the adapter's header check.
    return b'\x00\x00\x00\x20' + b'ftyp' + b'isom' + b'\x00' * 4 + b'\x00' * 4064


def prepare(monkeypatch, mode):
    monkeypatch.setenv('DOUBAO_TEST_MODE', '1')
    monkeypatch.setenv('DOUBAO_POLL_SECONDS', '0.12')
    monkeypatch.setenv('DOUBAO_RESULT_TIMEOUT', '25')
    html = HTML.replace('__MODE__', mode)

    async def goto_fixture(self, *args, **kwargs):
        await self.set_content(html)
        return None
    monkeypatch.setattr(Page, 'goto', goto_fixture)
    seen = []

    class Response:
        ok = True
        status = 200
        headers = {'content-length': str(len(mp4_bytes()))}

        async def body(self):
            return mp4_bytes()

    async def get_video(self, url, *args, **kwargs):
        seen.append(url)
        return Response()
    monkeypatch.setattr(APIRequestContext, 'get', get_video)
    return seen


async def drive(ctx, req, target, log):
    async def on_status(state, message):
        log.append(state)

    async def on_page(url):
        log.append(f"url:{url}")
    return await perform(ctx, req, {}, target, on_status, on_page,
                         confirmation_text=CONFIRM_ANSWER)


REQUEST = VideoRequest(account_id='acc_fixture', mode='text2video', ratio='16:9',
                       duration=4, prompt='电影级3D半写实国漫，雨后城楼远景')


def test_confirmation_answer_is_operator_specified_text():
    assert CONFIRM_ANSWER == ("以上严格执行我的要求，不许乱换模型，好好给我出视频，"
                              "不然就投诉你，请使用 Seedance 2.0 模型")


def test_confirm_gate_answered_once_then_archived(monkeypatch, tmp_path):
    seen = prepare(monkeypatch, 'needconfirm')
    target = tmp_path / 'result.mp4'
    statuses = []

    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            ctx = await browser.new_context()
            try:
                out = await drive(ctx, REQUEST, target, statuses)
                sends = await ctx.pages[-1].evaluate('window.__sends')
            finally:
                await browser.close()
            return out, sends
    out, sends = asyncio.run(run())

    assert 'confirming' in statuses, statuses
    confirm_index = statuses.index('confirming')
    assert statuses[confirm_index + 1] == 'generating', statuses
    # The gate is answered exactly once, with our own confirmation text - never a resend
    # of the original prompt (that would double-charge the account).
    assert sends == [REQUEST.prompt, CONFIRM_ANSWER], sends
    assert out['video_url'].startswith('https://v26-vdl.doubao.com/'), out['video_url']
    assert seen == [out['video_url']]
    assert target.stat().st_size > 256


def test_confirm_send_survives_delayed_composer_clear(monkeypatch, tmp_path):
    seen = prepare(monkeypatch, 'slowclear')
    target = tmp_path / 'result.mp4'
    statuses = []

    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            ctx = await browser.new_context()
            try:
                out = await drive(ctx, REQUEST, target, statuses)
                sends = await ctx.pages[-1].evaluate('window.__sends')
            finally:
                await browser.close()
            return out, sends
    out, sends = asyncio.run(run())

    assert 'confirming' in statuses, statuses
    confirm_index = statuses.index('confirming')
    assert statuses[confirm_index + 1] == 'generating', statuses
    # The contenteditable still shows 确认生成 right after the click on the real page.
    # A single immediate read would wrongly report CONFIRM_NOT_SENT and abandon a video
    # that Doubao actually started generating.
    assert sends == [REQUEST.prompt, CONFIRM_ANSWER], sends
    assert out['video_url'].startswith('https://v26-vdl.doubao.com/'), out['video_url']
    assert seen == [out['video_url']]
    assert target.stat().st_size > 256


def test_confirmation_never_resent_when_message_mutates(monkeypatch, tmp_path):
    seen = prepare(monkeypatch, 'mutateconfirm')
    target = tmp_path / 'result.mp4'
    statuses = []

    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            ctx = await browser.new_context()
            try:
                out = await drive(ctx, REQUEST, target, statuses)
                sends = await ctx.pages[-1].evaluate('window.__sends')
            finally:
                await browser.close()
            return out, sends
    out, sends = asyncio.run(run())

    # Doubao updates the trailing confirmation message in place (timestamp/action bar)
    # while it starts generating. The old text comparison treated that as a new gate and
    # sent a second confirmation - two paid requests for one video.
    assert sends == [REQUEST.prompt, CONFIRM_ANSWER], sends
    assert out['video_url'].startswith('https://v26-vdl.doubao.com/'), out['video_url']
    assert seen == [out['video_url']]
    assert target.stat().st_size > 256


def test_result_is_downloaded_from_detail_panel_clean_source(monkeypatch, tmp_path):
    """The chat card's <video> is the watermarked stream; only the panel's 保存 is archived."""
    seen = prepare(monkeypatch, 'detail_panel')
    target = tmp_path / 'result.mp4'
    statuses = []

    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            ctx = await browser.new_context()
            try:
                out = await drive(ctx, REQUEST, target, statuses)
                sends = await ctx.pages[-1].evaluate('window.__sends')
            finally:
                await browser.close()
            return out, sends
    out, sends = asyncio.run(run())

    assert sends == [REQUEST.prompt], sends
    assert out['video_url'] == CLEAN_URL, out['video_url']
    assert target.is_file() and target.stat().st_size == 4096
    # The watermark-flagged chat-player URL must never be fetched or archived.
    assert seen == []
    assert 'downloading' in statuses


def test_result_card_without_playable_url_needs_review(monkeypatch, tmp_path):
    seen = prepare(monkeypatch, 'card_no_player')
    target = tmp_path / 'result.mp4'
    statuses = []

    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            ctx = await browser.new_context()
            try:
                with pytest.raises(ReviewNeeded) as err:
                    await drive(ctx, REQUEST, target, statuses)
            finally:
                await browser.close()
            return err.value
    error = asyncio.run(run())

    # A card without any downloadable media must never be reported as a completed job.
    assert error.code == 'RESULT_URL_NOT_FOUND'
    assert not target.exists()
    assert seen == []
    assert 'downloading' not in statuses


def test_persisted_url_waits_past_generic_and_local_routes(monkeypatch):
    urls = ['https://www.doubao.com/chat/',
            'https://www.doubao.com/chat/local_2421202142124240',
            'https://www.doubao.com/chat/38449987205058306']

    class FakePage:
        calls = 0

        async def wait_for_timeout(self, _ms):
            return None

    async def fake_read_state(page):
        page.calls += 1
        return {'url': urls[min(page.calls, len(urls) - 1)]}

    monkeypatch.setattr(web_api, 'read_state', fake_read_state)
    page = FakePage()
    assert asyncio.run(persisted_url(page, urls[0])) == urls[-1]
    assert page.calls == 2

    # Off-site fixtures must never be delayed by the Doubao URL heuristic.
    assert asyncio.run(persisted_url(FakePage(), 'http://localhost:8001/test')) == 'http://localhost:8001/test'


def test_confirmation_result_poll_schedule(monkeypatch):
    monkeypatch.delenv("DOUBAO_POLL_SECONDS", raising=False)
    delays = [confirmation_result_poll_interval(index) for index in range(8)]
    assert delays == [120.0, 60.0, 30.0, 15.0, 10.0, 10.0, 10.0, 10.0]
    assert CONFIRMATION_RESULT_POLL_DELAYS == (120.0, 60.0, 30.0, 15.0, 10.0)


def test_result_probes_start_after_confirmation_schedule(monkeypatch):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.delenv("DOUBAO_POLL_SECONDS", raising=False)
    waits = []

    class FakePage:
        async def wait_for_timeout(self, milliseconds):
            waits.append(milliseconds)

    states = [
        {"text": "", "failed": False, "awaitingConfirm": True,
         "videoCards": 0, "runningLast": False, "url": "https://www.doubao.com/chat/1"},
        {"text": "", "failed": False, "awaitingConfirm": False,
         "videoCards": 0, "runningLast": False, "url": "https://www.doubao.com/chat/1"},
        {"text": "", "failed": False, "awaitingConfirm": False,
         "videoCards": 1, "runningLast": False, "url": "https://www.doubao.com/chat/1"},
        {"text": "", "failed": False, "awaitingConfirm": False,
         "videoCards": 1, "runningLast": False, "url": "https://www.doubao.com/chat/1"},
    ]

    async def fake_read_state(_page):
        return states.pop(0)

    async def fake_dismiss(_page):
        return False

    async def fake_confirm(_page, _text):
        return "composer:test"

    async def fake_confirm_wait(_page, _state, _baseline, _timeout):
        return "sent", states[0]

    async def fake_download(_page, _known_urls, _target, **_kwargs):
        return {"video_url": "https://www.doubao.com/result.mp4",
                "output": "/tmp/result.mp4"}

    async def no_status(_state, _message):
        return None

    monkeypatch.setattr(automation, "read_state", fake_read_state)
    monkeypatch.setattr(automation, "dismiss_blocking_dialogs", fake_dismiss)
    monkeypatch.setattr(automation, "confirm_pending_generation", fake_confirm)
    monkeypatch.setattr(automation, "wait_confirm_send", fake_confirm_wait)
    monkeypatch.setattr(automation, "download_result_video", fake_download)

    initial = {"videoCards": 0, "videoUrls": [], "videoUrl": None}
    result = asyncio.run(automation.wait_for_result(
        FakePage(), initial, Path("result.mp4"), no_status, "confirm"
    ))

    assert result["video_url"] == "https://www.doubao.com/result.mp4"
    assert waits == [120000, 60000, 30000]

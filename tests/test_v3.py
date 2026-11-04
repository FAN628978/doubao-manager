"""Offline tests: real Chromium against a LOCAL synthetic website, never Doubao production."""
import asyncio
import io
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from playwright.async_api import async_playwright, Page, APIRequestContext
from media_fixture import sample_mp4_bytes

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
from web_api import (VideoRequest, ReviewNeeded, perform, recover_interrupted_jobs,
                     target_page_url, read_state, read_composer_state, set_ui_option,
                     normalize_prompt_newlines, prompts_equivalent)

HTML = r'''<!doctype html><html lang="zh"><head><meta charset="utf-8"/></head><body>
<button type="button" data-testid="chat_input_action_model" onclick="document.getElementById('model-menu').hidden=false">Seedance 2.0 Mini</button>
<div id="model-menu" hidden>
<button type="button" role="menuitem">Seedance 2.5</button>
<button type="button" role="menuitem">Seedance 2.0</button>
<button type="button" role="menuitem">Seedance 2.0 Fast</button>
<button type="button" role="menuitem">Seedance 2.0 Mini</button>
</div>
<select data-testid="video-mode" aria-label="生成模式"><option value="text2video">文生视频</option><option value="image2video">首帧图生视频</option><option value="first_last_frame">首尾帧生视频</option><option value="reference_images">参考生视频</option></select>
<select data-testid="video-ratio" aria-label="画幅比例"><option>16:9</option><option>9:16</option><option>1:1</option></select>
<select data-testid="video-duration" aria-label="时长"><option value="4">4秒</option><option value="10">10秒</option><option value="15">15秒</option></select>
<input data-slot="first" type="file" accept="image/png,image/jpeg"/>
<input data-slot="last" type="file" accept="image/png,image/jpeg"/>
<input data-slot="references" type="file" accept="image/png,image/jpeg" multiple/>
<div data-slot="first" data-asset-status="pending" hidden>首帧</div>
<div data-slot="last" data-asset-status="pending" hidden>尾帧</div>
<div data-slot="references" data-asset-status="pending" hidden>参考图</div>
<div data-testid="chat_input"><textarea id="prompt" data-testid="chat_input_input"></textarea><button type="submit" id="generate">生成</button></div>
<div id="message"></div><div id="result"></div>
<script>
for(const input of document.querySelectorAll('input[type=file]')) {
 input.addEventListener('change',()=>{
 const status=document.querySelector(`[data-asset-status][data-slot="${input.dataset.slot}"]`);
 status.setAttribute('data-asset-status','uploaded'); status.setAttribute('data-count',input.files.length);status.hidden=false;
 });
}
const msg=document.getElementById('message'),result=document.getElementById('result');
function complete(){msg.innerText='生成完成';result.innerHTML='<video id="output" src="https://www.doubao.com/mock.mp4"></video>';
 result.removeAttribute('data-generation-status');
 result.setAttribute('data-generation-status','completed'); }
if(location.search.includes('submitted=1'))complete();
document.getElementById('generate').addEventListener('click',()=>{
  msg.innerText='正在生成';result.setAttribute('data-generation-status','running');
  setTimeout(complete,250);
});
</script></body></html>'''


async def launch_chromium(pw):
    exe = os.getenv('DOUBAO_TEST_CHROMIUM_EXE')
    if not exe and Path('/usr/bin/chromium').is_file(): exe = '/usr/bin/chromium'
    options = dict(headless=True, args=['--no-sandbox'])
    if exe: options['executable_path'] = exe
    else: options['channel'] = 'chrome'
    return await pw.chromium.launch(**options)


def png_bytes():
    im=Image.new('RGB',(256,256),(38,63,112))
    b=io.BytesIO();im.save(b,format='PNG');return b.getvalue()


@pytest.fixture(scope='module')
def local_site(tmp_path_factory):
    folder=tmp_path_factory.mktemp('local_fixture')
    (folder/'index.html').write_text(HTML,encoding='utf-8')
    (folder/'video.mp4').write_bytes(sample_mp4_bytes())
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            f=folder/('video.mp4' if self.path.startswith('/video.mp4') else 'index.html')
            data=f.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type','video/mp4' if f.name.endswith('.mp4') else 'text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers();self.wfile.write(data)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield {'url':'http://127.0.0.1:'+str(server.server_port)+'/', 'video':(folder/'video.mp4').read_bytes()}
    server.shutdown();server.server_close()


@pytest.mark.parametrize('mode', ['text2video','image2video','first_last_frame','reference_images'])
def test_playwright_browser_video_modes(local_site, tmp_path, monkeypatch, mode):
    monkeypatch.setenv('DOUBAO_TEST_MODE','1')
    monkeypatch.setenv('DOUBAO_TEST_PAGE',local_site['url'])
    monkeypatch.setenv('DOUBAO_POLL_SECONDS','0.12')
    monkeypatch.setenv('DOUBAO_RESULT_TIMEOUT','6')
    # Hosted Chromium is policy-restricted: external AND localhost navigation are blocked.
    # Exercise real browser DOM interactions with set_content, and mock only network I/O.
    async def goto_fixture(self, *args, **kwargs):
        await self.set_content(HTML); return None
    monkeypatch.setattr(Page, 'goto', goto_fixture)
    class Response:
        ok=True; status=200
        headers={'content-length':str(len(local_site['video']))}
        async def body(self):return local_site['video']
    async def get_video(self, *args, **kwargs): return Response()
    monkeypatch.setattr(APIRequestContext,'get',get_video)
    img=tmp_path/'test.png';img.write_bytes(png_bytes())
    fields={'text2video':{},'image2video':{'first_frame_asset_id':'x'},
            'first_last_frame':{'first_frame_asset_id':'x','last_frame_asset_id':'y'},
            'reference_images':{'reference_asset_ids':['x','y']}}[mode]
    req=VideoRequest(account_id='acc_fixture',mode=mode,ratio='9:16',duration=10,prompt='一只猫跳跃',**fields)
    async def run():
        async with async_playwright() as pw:
            b=await launch_chromium(pw)
            ctx=await b.new_context()
            status_log=[]
            async def on_status(s,m): status_log.append(s)
            urls=[]
            async def on_page(u): urls.append(u)
            try:
                out=await perform(ctx,req,{'x':img,'y':img},tmp_path/'result.mp4',on_status,on_page)
            except Exception:
                print('DEBUG STATE=',await read_state(ctx.pages[-1]))
                print('DEBUG VIDEO=',await ctx.pages[-1].locator('video').count())
                print('DEBUG STATUSES=',status_log)
                raise
            assert out['video_url'].endswith('/mock.mp4')
            assert (tmp_path/'result.mp4').stat().st_size>256
            assert 'downloading' in status_log
            assert urls[-1] == 'about:blank'
            pages=ctx.pages
            assert await pages[-1].locator('[data-testid="video-mode"]').input_value()==mode
            assert await pages[-1].locator('[data-testid="video-ratio"]').input_value()=='9:16'
            assert await pages[-1].locator('[data-testid="video-duration"]').input_value()=='10'
            assert await pages[-1].locator('textarea').input_value()=='一只猫跳跃'
            await b.close()
    asyncio.run(run())


def test_missing_ratio_blocks_submission(local_site,tmp_path,monkeypatch):
    monkeypatch.setenv('DOUBAO_TEST_MODE','1');monkeypatch.setenv('DOUBAO_TEST_PAGE',local_site['url'])
    async def goto_fixture(self,*args,**kwargs):
        await self.set_content(HTML.replace('data-testid="video-ratio"','data-testid="unsupported"'));return None
    monkeypatch.setattr(Page, 'goto', goto_fixture)
    req=VideoRequest(account_id='test',prompt='测试',ratio='9:16')
    async def run():
        async with async_playwright() as pw:
            b=await launch_chromium(pw)
            ctx=await b.new_context();page=await ctx.new_page()
            # Does not intercept page loaded by perform: override HTML response for all document requests.
            # Navigation replaced with local synthetic DOM; no network or platform access.
            async def no(*args): pass
            with pytest.raises(ReviewNeeded) as error:
                await perform(ctx,req,{},tmp_path/'should_not_exist.mp4',no,no)
            assert error.value.code=='SETTING_NOT_FOUND'
            assert not (tmp_path/'should_not_exist.mp4').exists()
            assert await ctx.pages[-1].locator('video').count()==0
            await b.close()
    asyncio.run(run())


def test_current_composer_video_chip_confirms_mode():
    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            page = await browser.new_page()
            await page.set_content('''
              <div role="button" style="position:fixed;left:400px;bottom:20px"
                   onclick="document.body.insertAdjacentHTML('beforeend', '<div data-input-engine-actionbar-render-entry-key=\\'video-generation-params-panel\\'>自动 · 10s</div>')">
                <span>视频生成</span>
              </div>''')
            await set_ui_option(page, 'mode', 'reference_images')
            await browser.close()
    asyncio.run(run())


def test_current_composer_activates_video_skill():
    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            page = await browser.new_page()
            await page.set_content('''
              <button data-testid="skill_bar_button_17"
                onclick="this.remove();settings.hidden=false">视频生成</button>
              <div id="settings" role="button" hidden>自动 · 10s</div>''')
            await set_ui_option(page, 'mode', 'reference_images')
            assert await page.locator('#settings').is_visible()
            await browser.close()
    asyncio.run(run())


def test_current_composer_combined_video_settings():
    html = '''
      <button id="settings" data-input-engine-actionbar-render-entry-key="video-generation-params-panel">自动 · 10s</button>
      <div id="menu" hidden>
        <button id="ratio">16:9</button><button id="duration">4s</button>
      </div>
      <script>
        settings.onclick=()=>menu.hidden=false;
        ratio.onclick=()=>{settings.innerText='16:9 · 10s'};
        duration.onclick=()=>{settings.innerText='16:9 · 4s';menu.hidden=true};
      </script>'''
    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            page = await browser.new_page()
            await page.set_content(html)
            await set_ui_option(page, 'ratio', '16:9')
            await set_ui_option(page, 'duration', 4)
            assert await page.locator('#settings').inner_text() == '16:9 · 4s'
            await browser.close()
    asyncio.run(run())


def test_current_composer_nested_editor_state():
    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            page = await browser.new_page()
            await page.set_content('''
              <div data-testid="chat_input">
                <div data-testid="chat_input_input"><div contenteditable="true">测试提示词</div></div>
                <div data-testid="attachment-image-card"></div>
                <button data-testid="chat_input_send_button"></button>
              </div>''')
            state = await read_composer_state(page)
            assert state == {'exists': True, 'prompt': '测试提示词',
                             'attachmentCount': 1, 'sendDisabled': False}
            await browser.close()
    asyncio.run(run())


def test_prompts_equivalent_tolerates_editor_normalization():
    # CRLF and lone CR are collapsed to LF
    assert prompts_equivalent("a\nb\nc", "a\r\nb\r\nc")
    assert prompts_equivalent("a\nb\nc", "a\rb\rc")
    # contenteditable renders each blank line as an extra <br>, i.e. one more \n
    assert prompts_equivalent("a\n\nb\n\nc", "a\n\n\nb\n\n\nc")
    # surrounding whitespace is ignored
    assert prompts_equivalent("  x  ", "x")
    # real differences must still be caught: dropped line, truncation, empty vs content
    assert not prompts_equivalent("a\nb\nc", "a\nc")
    assert not prompts_equivalent("a\nb\nc", "a\nb")
    assert not prompts_equivalent("", "a\nb")
    assert not prompts_equivalent("同一行换词", "同一行换成另一个词")
    # fill-time normalization keeps content intact, unifies endings only
    assert normalize_prompt_newlines("a\r\nb\rc\nd") == "a\nb\nc\nd"


def test_contenteditable_multiline_prompt_reads_back_equivalent():
    prompt = ("Visual_Style:\r\nKorean webtoon manhwa style\r\nsubject_definitions\r\n"
              "\r\nsummary\r\n<Subject 1> is the environment: 教学楼外观")
    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            page = await browser.new_page()
            await page.set_content('''
              <div data-testid="chat_input">
                <div data-testid="chat_input_input"><div contenteditable="true"></div></div>
                <button data-testid="chat_input_send_button"></button>
              </div>''')
            await page.locator('div[contenteditable="true"]').fill(normalize_prompt_newlines(prompt))
            state = await read_composer_state(page)
            assert prompts_equivalent(state["prompt"], prompt), (state["prompt"], prompt)
            await browser.close()
    asyncio.run(run())


def test_multiline_crlf_prompt_passes_verification(tmp_path, monkeypatch):
    # Regression: a multi-line CRLF prompt with blank lines used to fail the strict
    # byte-for-byte verification (PROMPT_UNVERIFIED) because the contenteditable reads
    # back \n and expands blank lines into <br>. It must now pass end-to-end.
    monkeypatch.setenv('DOUBAO_TEST_MODE','1')
    monkeypatch.setenv('DOUBAO_TEST_PAGE','http://localhost:8001/test')
    monkeypatch.setenv('DOUBAO_POLL_SECONDS','0.12')
    monkeypatch.setenv('DOUBAO_RESULT_TIMEOUT','6')
    html = HTML.replace('<textarea id="prompt"></textarea>',
                        '<div contenteditable="true" id="prompt" style="min-height:20px"></div>')
    async def goto_fixture(self, *args, **kwargs):
        await self.set_content(html); return None
    monkeypatch.setattr(Page, 'goto', goto_fixture)
    # Minimal MP4 container (ftyp at offset 4), enough for download_media's header check.
    mp4 = b'\x00\x00\x00\x20' + b'ftyp' + b'isom' + b'\x00' * 4 + b'\x00' * 4064
    class Response:
        ok=True; status=200
        headers={'content-length':str(len(mp4))}
        async def body(self): return mp4
    async def get_video(self, *args, **kwargs): return Response()
    monkeypatch.setattr(APIRequestContext,'get',get_video)
    prompt = ("Visual_Style:\r\nKorean webtoon manhwa style\r\nsubject_definitions\r\n\r\n"
              "summary\r\n<Subject 1> is the environment: the teaching building")
    req = VideoRequest(account_id='acc', mode='text2video', ratio='16:9', duration=4, prompt=prompt)
    async def run():
        async with async_playwright() as pw:
            b = await launch_chromium(pw)
            ctx = await b.new_context()
            statuses = []
            async def on_status(s, m): statuses.append(s)
            async def on_page(u): pass
            out = await perform(ctx, req, {}, tmp_path/'result.mp4', on_status, on_page)
            assert out['video_url'].endswith('/mock.mp4')
            assert 'downloading' in statuses
            await b.close()
    asyncio.run(run())


def test_api_validation_and_idempotency(tmp_path,monkeypatch):
    dbpath=tmp_path/'test.sqlite3'
    monkeypatch.setattr(application,'DB_PATH',dbpath)
    with TestClient(application.app) as client:
        acc=client.post('/api/accounts',json={'name':'测试账号'}).json()['id']
        f=client.post('/api/v1/videos/assets',files={'file':('face.png',png_bytes(),'image/png')})
        assert f.status_code==201,f.text
        aid=f.json()['asset_id']
        for mode,payload in [
            ('image2video',{}),('first_last_frame',{'first_frame_asset_id':aid}),
            ('reference_images',{'reference_asset_ids':[]})]:
            r=client.post('/api/v1/videos/generations',json={
                'account_id':acc,'prompt':'你好','mode':mode,**payload})
            assert r.status_code==422,r.text
        body={'job_id':'job_42','account_id':acc,'prompt':'猫跳跃','mode':'image2video',
              'first_frame_asset_id':aid,'ratio':'9:16','duration':10,
              'idempotency_key':'same-request','task_name':'视频任务 #42','auto_start':False}
        first=client.post('/api/v1/videos/generations',json=body)
        assert first.status_code==202,first.text
        assert first.json()['id']=='job_42'
        second=client.post('/api/v1/videos/generations',json=body)
        assert second.status_code==202 and first.json()['id']==second.json()['id']
        assert second.json()['task_name'] == '视频任务 #42'
        assert next(j for j in client.get('/api/jobs').json() if j['id'] == first.json()['id'])['task_name'] == '视频任务 #42'
        different=client.post('/api/v1/videos/generations',json={**body,'prompt':'不同内容'})
        assert different.status_code==409
        invalid_id=client.post('/api/v1/videos/generations',json={**body,'job_id':'job_random'})
        assert invalid_id.status_code==422
        jobid=first.json()['id']
        forged=client.patch(f'/api/jobs/{jobid}',json={'status':'downloaded'})
        assert forged.status_code==409
        bad=client.post('/api/v1/videos/assets',files={'file':('not.png',b'not image','image/png')})
        assert bad.status_code==422
        # Simulate sudden process termination AFTER a task may have been submitted.
        with application.db() as conn: conn.execute('UPDATE jobs SET status="generating" WHERE id=?',(jobid,))
        recover_interrupted_jobs(application.db)
        current=client.get('/api/v1/videos/generations/'+jobid).json()
        assert current['status']=='needs_review'
        assert current['web']['error_code']=='INTERRUPTED_REVIEW_REQUIRED'
        no_restart=client.post('/api/v1/videos/generations/'+jobid+'/start')
        assert no_restart.status_code==409
        assert client.get('/api/v1/videos/generations/'+jobid+'/events').status_code==200
        assert client.get('/api/v1/videos/capabilities').json()['precise_ui_controls_required']


def test_member_account_priority_and_forced_model(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'member.sqlite3')
    with TestClient(application.app) as client:
        normal = client.post('/api/accounts', json={
            'name': '普通账号', 'priority': 10,
        }).json()
        member = client.post('/api/accounts', json={
            'name': '会员账号', 'is_member': True, 'priority': 20,
        }).json()

        assert member['is_member'] is True
        assert member['priority'] == 20
        accounts = client.get('/api/accounts').json()
        assert [item['id'] for item in accounts[:2]] == [member['id'], normal['id']]
        assert [item['sequence'] for item in accounts[:2]] == [1, 2]

        created = client.post('/api/v1/videos/generations', json={
            'prompt': '会员模型覆盖测试',
            'model': 'Seedance 2.0 Mini',
            'auto_start': False,
        })
        assert created.status_code == 202, created.text
        body = created.json()
        # 待处理任务不分配账号；进入处理前才选出优先级的会员账号并强制使用会员模型。
        assert body['account_id'] is None
        started = client.post(f"/api/v1/videos/generations/{body['id']}/start")
        assert started.status_code == 200, started.text
        # 分配发生在拿到处理名额之后，这里轮询等待。
        body = started.json()
        for _ in range(500):
            body = client.get(f"/api/v1/videos/generations/{created.json()['id']}").json()
            if body['account_id']:
                break
            time.sleep(0.01)
        assert body['account_id'] == member['id']
        assert body['web']['model'] == 'Seedance 2.0'

        updated = client.patch(f"/api/accounts/{normal['id']}", json={
            'is_member': True, 'priority': 30,
        })
        assert updated.status_code == 200, updated.text
        assert updated.json()['is_member'] is True
        assert updated.json()['priority'] == 30


def test_member_and_non_member_models_follow_local_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'model-settings.sqlite3')
    with TestClient(application.app) as client:
        defaults = client.get('/api/settings/video-models')
        assert defaults.status_code == 200
        assert defaults.json()['member_model'] == 'Seedance 2.0'
        assert defaults.json()['non_member_model'] == 'Seedance 2.0 Mini'

        saved = client.put('/api/settings/video-models', json={
            'member_model': 'Seedance 2.0 Fast',
            'non_member_model': 'Seedance 2.5',
        })
        assert saved.status_code == 200, saved.text

        member = client.post('/api/accounts', json={
            'name': '模型会员', 'is_member': True,
        }).json()
        normal = client.post('/api/accounts', json={
            'name': '模型非会员', 'is_member': False,
        }).json()
        member_job = client.post('/api/v1/videos/generations', json={
            'account_id': member['id'], 'prompt': '会员模型',
            'model': 'Seedance 2.0 Mini', 'auto_start': False,
        })
        normal_job = client.post('/api/v1/videos/generations', json={
            'account_id': normal['id'], 'prompt': '非会员模型',
            'model': 'Seedance 2.0', 'auto_start': False,
        })
        assert member_job.status_code == 202, member_job.text
        assert normal_job.status_code == 202, normal_job.text
        assert member_job.json()['web']['model'] == 'Seedance 2.0 Fast'
        assert normal_job.json()['web']['model'] == 'Seedance 2.5'

        invalid = client.put('/api/settings/video-models', json={
            'member_model': 'unknown',
            'non_member_model': 'Seedance 2.0 Mini',
        })
        assert invalid.status_code == 422


def test_paused_account_is_not_used_for_new_generations(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'paused.sqlite3')
    with TestClient(application.app) as client:
        paused = client.post('/api/accounts', json={
            'name': '暂停账号', 'priority': 100,
        }).json()
        active = client.post('/api/accounts', json={
            'name': '可用账号', 'priority': 1,
        }).json()
        queued = client.post('/api/v1/videos/generations', json={
            'account_id': paused['id'], 'prompt': '暂停前已排队',
            'duration': 4, 'auto_start': False,
        }).json()

        response = client.post(f"/api/accounts/{paused['id']}/pause")
        assert response.status_code == 200
        assert response.json()['paused'] is True
        listed = {item['id']: item for item in client.get('/api/accounts').json()}
        assert listed[paused['id']]['paused'] is True
        blocked_start = client.post(f"/api/v1/videos/generations/{queued['id']}/start")
        assert blocked_start.status_code == 409
        assert '暂停使用' in blocked_start.json()['detail']
        manual_job = client.post('/api/jobs', json={
            'account_id': paused['id'], 'prompt': '人工任务也不能启动',
        }).json()
        blocked_prepare = client.post(f"/api/jobs/{manual_job['id']}/prepare")
        assert blocked_prepare.status_code == 409
        assert '暂停使用' in blocked_prepare.json()['detail']

        selected = client.post('/api/v1/videos/generations', json={
            'prompt': '自动选择可用账号', 'duration': 4, 'auto_start': False,
        })
        assert selected.status_code == 202, selected.text
        assert selected.json()['account_id'] is None
        selected_start = client.post(f"/api/v1/videos/generations/{selected.json()['id']}/start")
        assert selected_start.status_code == 200, selected_start.text
        # 分配发生在拿到处理名额之后，这里轮询等待。
        selected_state = selected_start.json()
        for _ in range(500):
            selected_state = client.get(f"/api/v1/videos/generations/{selected.json()['id']}").json()
            if selected_state['account_id']:
                break
            time.sleep(0.01)
        assert selected_state['account_id'] == active['id']

        rejected = client.post('/api/v1/videos/generations', json={
            'account_id': paused['id'], 'prompt': '不能使用暂停账号',
            'duration': 4, 'auto_start': False,
        })
        assert rejected.status_code == 409
        assert '暂停使用' in rejected.json()['detail']

        resumed = client.post(f"/api/accounts/{paused['id']}/resume")
        assert resumed.status_code == 200
        assert resumed.json()['paused'] is False
        created = client.post('/api/v1/videos/generations', json={
            'account_id': paused['id'], 'prompt': '恢复后可用',
            'duration': 4, 'auto_start': False,
        })
        assert created.status_code == 202, created.text


def test_copied_portable_data_rebases_account_profiles(tmp_path, monkeypatch):
    import sqlite3

    new_data = tmp_path / 'new-version' / 'data'
    new_profiles = new_data / 'profiles'
    new_profiles.mkdir(parents=True)
    db_path = new_data / 'manager.sqlite3'
    account_id = 'acc_portable'
    copied_profile = new_profiles / account_id
    copied_profile.mkdir()
    (copied_profile / 'login-state').write_text('preserved', encoding='utf-8')

    with sqlite3.connect(db_path) as conn:
        conn.execute('''CREATE TABLE accounts (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            profile_dir TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, last_opened_at TEXT)''')
        conn.execute(
            'INSERT INTO accounts(id,name,profile_dir,created_at) VALUES(?,?,?,?)',
            (account_id, '旧版账号', str(tmp_path / 'old-version' / 'data' / 'profiles' / account_id),
             '2026-01-01T00:00:00+00:00'),
        )

    monkeypatch.setattr(application, 'DB_PATH', db_path)
    monkeypatch.setattr(application, 'BASE_DIR', tmp_path / 'new-version')
    monkeypatch.setattr(application, 'DATA_DIR', new_data)
    monkeypatch.setattr(application, 'PROFILES_DIR', new_profiles)
    application.init_db()

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(accounts)')}
        profile_dir = conn.execute(
            'SELECT profile_dir FROM accounts WHERE id=?', (account_id,),
        ).fetchone()[0]
        schema_version = conn.execute('PRAGMA user_version').fetchone()[0]
    assert {'is_member', 'priority', 'paused'} <= columns
    assert schema_version == application.DATA_SCHEMA_VERSION
    assert profile_dir == str(Path('data') / 'profiles' / account_id)
    assert application.portable_path(profile_dir).joinpath('login-state').read_text(encoding='utf-8') == 'preserved'


def test_persisted_paths_are_relative_and_confined_to_data(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    monkeypatch.setattr(application, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(application, 'DATA_DIR', data)

    stored = str(Path('data') / 'profiles' / 'acc_safe')
    assert application.portable_path(stored) == (data / 'profiles' / 'acc_safe').resolve()
    assert application.stored_data_path(data / 'videos' / 'result.mp4') == str(
        Path('data') / 'videos' / 'result.mp4'
    )
    with pytest.raises(ValueError):
        application.portable_path((data / 'profiles' / 'acc_absolute').resolve())
    with pytest.raises(ValueError):
        application.portable_path(Path('data') / '..' / 'outside.txt')
    with pytest.raises(ValueError):
        application.stored_data_path(tmp_path / 'outside.txt')


def test_newer_data_schema_is_not_modified_by_older_build(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / 'manager.sqlite3'
    with sqlite3.connect(db_path) as conn:
        conn.execute(f'PRAGMA user_version = {application.DATA_SCHEMA_VERSION + 1}')
    monkeypatch.setattr(application, 'DB_PATH', db_path)

    with pytest.raises(RuntimeError):
        application.init_db()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == application.DATA_SCHEMA_VERSION + 1
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='accounts'"
        ).fetchone()[0] == 0


def test_single_entry_uses_names(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    with TestClient(application.app) as client:
        account_id = client.post('/api/accounts', json={'name': 'name-fields'}).json()['id']
        response = client.post('/api/v1/videos', data={
            'job_id': 'job_8758',
            'account_id': account_id,
            'prompt': 'test prompt',
            'project_name': '\u98df\u8bf1\u5343\u91d1',
            'episode_name': '\u7b2c6\u96c6',
            'shot_name': '\u94b1\u5305\u5f39\u5f00\u7f8e\u91d1\u60ca\u73b0',
            'task_name': '\u98df\u8bf1\u5343\u91d1 / \u7b2c6\u96c6 / \u94b1\u5305\u5f39\u5f00\u7f8e\u91d1\u60ca\u73b0',
            'auto_start': 'false',
        })
        assert response.status_code == 202, response.text
        assert response.json()['job_id'] == 'job_8758'
        job = next(item for item in client.get('/api/jobs').json()
                   if item['id'] == response.json()['job_id'])
        assert job['project_id'] == '\u98df\u8bf1\u5343\u91d1'
        assert job['episode_id'] == '\u7b2c6\u96c6'
        assert job['shot_id'] == '\u94b1\u5305\u5f39\u5f00\u7f8e\u91d1\u60ca\u73b0'
        assert job['task_name'] == '\u98df\u8bf1\u5343\u91d1 / \u7b2c6\u96c6 / \u94b1\u5305\u5f39\u5f00\u7f8e\u91d1\u60ca\u73b0'


def test_bulk_delete_jobs_and_delete_videos_permanently(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    monkeypatch.setattr(application, 'DB_PATH', data / 'manager.sqlite3')
    monkeypatch.setattr(application, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(application, 'DATA_DIR', data)
    monkeypatch.setattr(application, 'PROFILES_DIR', data / 'profiles')
    monkeypatch.setattr(application, 'TRASH_DIR', data / 'trash')
    with TestClient(application.app) as client:
        plain = client.post('/api/jobs', json={'prompt': '普通分镜'}).json()['id']
        video = data / 'videos' / 'library' / 'result.mp4'
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b'video-data')
        relative = str(video.relative_to(tmp_path))
        with application.db() as conn:
            conn.execute("UPDATE jobs SET status='downloaded',video_path=? WHERE id=?", (relative, plain))

        account = client.post('/api/accounts', json={'name': '批量删除账号'}).json()['id']
        web = client.post('/api/v1/videos/generations', json={
            'account_id': account, 'prompt': '网页分镜', 'duration': 4, 'auto_start': False,
        }).json()['id']
        with application.db() as conn:
            assert conn.execute(
                'SELECT reserved_seconds FROM account_quota WHERE account_id=?', (account,)
            ).fetchone()[0] == 4
            conn.execute("UPDATE jobs SET status='needs_review' WHERE id=?", (web,))
            conn.execute(
                "UPDATE video_web_jobs SET quota_state='unknown' WHERE job_id=?", (web,)
            )

        response = client.post('/api/jobs/bulk-delete', json={'job_ids': [plain, web, plain]})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['deleted'] == 2 and result['deleted_files'] == 1
        assert not video.exists()
        assert not (data / 'trash').exists()
        with application.db() as conn:
            assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
            assert conn.execute('SELECT COUNT(*) FROM video_web_jobs').fetchone()[0] == 0
            assert conn.execute('SELECT COUNT(*) FROM video_web_events').fetchone()[0] == 0
            assert conn.execute(
                'SELECT reserved_seconds FROM account_quota WHERE account_id=?', (account,)
            ).fetchone()[0] == 0
            assert conn.execute(
                'SELECT consumed_seconds FROM account_quota WHERE account_id=?', (account,)
            ).fetchone()[0] == 0


def test_bulk_delete_rejects_running_jobs_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'manager.sqlite3')
    monkeypatch.setattr(application, 'TRASH_DIR', tmp_path / 'trash')
    with TestClient(application.app) as client:
        first = client.post('/api/jobs', json={'prompt': '可删除'}).json()['id']
        running = client.post('/api/jobs', json={'prompt': '执行中'}).json()['id']
        with application.db() as conn:
            conn.execute("UPDATE jobs SET status='generating' WHERE id=?", (running,))
        response = client.post('/api/jobs/bulk-delete', json={'job_ids': [first, running]})
        assert response.status_code == 409
        assert {item['id'] for item in client.get('/api/jobs').json()} == {first, running}


def test_account_default_naming_and_recoverable_delete(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    monkeypatch.setattr(application, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    monkeypatch.setattr(application, 'DATA_DIR', data)
    monkeypatch.setattr(application, 'PROFILES_DIR', data / 'profiles')
    monkeypatch.setattr(application, 'DOWNLOADS_DIR', data / 'videos')
    monkeypatch.setattr(application, 'TRASH_DIR', data / 'trash')
    with TestClient(application.app) as client:
        assert client.get('/api/accounts/suggested-name').json()['name'] == 'doubao01'
        first = client.post('/api/accounts', json={}).json()
        assert first['name'] == 'doubao01'
        assert client.post('/api/accounts', json={'name': None}).json()['name'] == 'doubao02'
        assert client.post('/api/accounts', json={'name': '   '}).json()['name'] == 'doubao03'
        assert client.post('/api/accounts', json={'name': '创作组 A'}).json()['name'] == '创作组 A'
        assert client.get('/api/accounts/suggested-name').json()['name'] == 'doubao04'

        job = client.post('/api/jobs', json={'prompt': '镜头01', 'account_id': first['id']}).json()['id']
        profile = tmp_path / first['profile_dir']
        (profile / 'Default').mkdir(parents=True)
        (profile / 'Default' / 'Login Data').write_text('登录态', encoding='utf-8')
        downloads = data / 'videos' / first['id']
        downloads.mkdir(parents=True)
        (downloads / 'stray.mp4').write_bytes(b'x')

        application.open_contexts[first['id']] = object()
        assert client.delete(f"/api/accounts/{first['id']}").status_code == 409
        application.open_contexts.pop(first['id'], None)

        result = client.delete(f"/api/accounts/{first['id']}").json()
        assert result['deleted'] and result['moved_to'] and result['unlinked_jobs'] == 1
        assert not profile.exists() and not downloads.exists()
        trash = data / 'trash' / result['moved_to'].split('/')[-1]
        assert (trash / 'profiles' / first['id'] / 'Default' / 'Login Data').read_text(encoding='utf-8') == '登录态'
        assert (trash / 'videos' / first['id'] / 'stray.mp4').read_bytes() == b'x'
        assert client.get('/api/jobs').json()[0]['account_id'] is None
        assert client.delete(f"/api/accounts/{first['id']}").status_code == 404

        # Naming continues after the highest remaining number, and purge deletes for good.
        assert client.get('/api/accounts/suggested-name').json()['name'] == 'doubao04'
        third = client.post('/api/accounts', json={'name': '临时'}).json()
        purged = client.delete(f"/api/accounts/{third['id']}?purge_data=true").json()
        assert purged['data_purged'] and purged['moved_to'] is None
        assert not (tmp_path / third['profile_dir']).exists()
        assert list((data / 'trash').iterdir()) == [trash]  # purge created no extra trash copy


def test_clear_account_caches_preserves_login_and_skips_open_account(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    monkeypatch.setattr(application, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(application, 'DB_PATH', data / 'manager.sqlite3')
    monkeypatch.setattr(application, 'DATA_DIR', data)
    monkeypatch.setattr(application, 'PROFILES_DIR', data / 'profiles')
    monkeypatch.setattr(application, 'DOWNLOADS_DIR', data / 'videos')
    monkeypatch.setattr(application, 'TRASH_DIR', data / 'trash')
    with TestClient(application.app) as client:
        first = client.post('/api/accounts', json={'name': '可清理账号'}).json()
        second = client.post('/api/accounts', json={'name': '浏览器打开账号'}).json()
        first_profile = tmp_path / first['profile_dir']
        second_profile = tmp_path / second['profile_dir']

        login_data = first_profile / 'Default' / 'Login Data'
        login_data.parent.mkdir(parents=True, exist_ok=True)
        login_data.write_bytes(b'login-state')
        cache_files = {
            first_profile / 'Default' / 'Cache' / 'cache.bin': b'a' * 3,
            first_profile / 'Default' / 'Code Cache' / 'code.bin': b'b' * 5,
            first_profile / 'Default' / 'GPUCache' / 'gpu.bin': b'c' * 7,
            first_profile / 'Default' / 'Shared Dictionary' / 'cache' / 'shared.bin': b'd' * 11,
        }
        for path, content in cache_files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        open_cache = second_profile / 'Default' / 'Cache' / 'cache.bin'
        open_cache.parent.mkdir(parents=True, exist_ok=True)
        open_cache.write_bytes(b'keep')
        application.open_contexts[second['id']] = object()
        try:
            response = client.post('/api/accounts/clear-cache')
        finally:
            application.open_contexts.pop(second['id'], None)

        assert response.status_code == 200, response.text
        result = response.json()
        assert result['freed_bytes'] == 26
        assert result['cleaned_accounts'] == 1
        assert result['removed_directories'] == 4
        assert result['skipped'] == [{
            'id': second['id'],
            'name': '浏览器打开账号',
            'reason': '浏览器窗口已打开或任务正在执行',
        }]
        assert login_data.read_bytes() == b'login-state'
        assert all(not path.exists() for path in cache_files)
        assert open_cache.read_bytes() == b'keep'


def test_safe_cleanup_removes_only_disposable_data(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    monkeypatch.setattr(application, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(application, 'DB_PATH', data / 'manager.sqlite3')
    monkeypatch.setattr(application, 'DATA_DIR', data)
    monkeypatch.setattr(application, 'PROFILES_DIR', data / 'profiles')
    monkeypatch.setattr(application, 'DOWNLOADS_DIR', data / 'videos')
    monkeypatch.setattr(application, 'TRASH_DIR', data / 'trash')
    monkeypatch.setattr(application, 'ACCEPTANCE_REPORTS_DIR', data / 'acceptance_reports')
    with TestClient(application.app) as client:
        closed = client.post('/api/accounts', json={'name': '已关闭账号'}).json()
        opened = client.post('/api/accounts', json={'name': '浏览器打开账号'}).json()
        closed_profile = tmp_path / closed['profile_dir']
        opened_profile = tmp_path / opened['profile_dir']

        login_data = closed_profile / 'Default' / 'Login Data'
        login_data.parent.mkdir(parents=True, exist_ok=True)
        login_data.write_bytes(b'login')
        closed_cache = closed_profile / 'Default' / 'Cache' / 'cache.bin'
        closed_cache.parent.mkdir(parents=True, exist_ok=True)
        closed_cache.write_bytes(b'cache')
        open_cache = opened_profile / 'Default' / 'Cache' / 'cache.bin'
        open_cache.parent.mkdir(parents=True, exist_ok=True)
        open_cache.write_bytes(b'keep')

        trash_file = data / 'trash' / 'old' / 'profile.bin'
        trash_file.parent.mkdir(parents=True)
        trash_file.write_bytes(b'trash-data')
        report_file = data / 'acceptance_reports' / 'old.json'
        report_file.parent.mkdir(parents=True)
        report_file.write_bytes(b'report-data')
        archived = data / 'videos' / 'library' / 'keep.mp4'
        archived.parent.mkdir(parents=True)
        archived.write_bytes(b'archived')
        asset = data / 'web_assets' / 'keep.png'
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b'asset')

        application.open_contexts[opened['id']] = object()
        try:
            response = client.post('/api/cleanup/safe')
        finally:
            application.open_contexts.pop(opened['id'], None)

        assert response.status_code == 200, response.text
        result = response.json()
        expected_bytes = 5 + 10 + 11
        assert result['freed_bytes'] == expected_bytes
        assert result['areas']['browser_cache']['cleaned_accounts'] == 1
        assert result['areas']['trash']['freed_bytes'] == 10
        assert result['areas']['acceptance_reports']['freed_bytes'] == 11
        assert login_data.read_bytes() == b'login'
        assert open_cache.read_bytes() == b'keep'
        assert archived.read_bytes() == b'archived'
        assert asset.read_bytes() == b'asset'
        assert list((data / 'trash').iterdir()) == []
        assert list((data / 'acceptance_reports').iterdir()) == []

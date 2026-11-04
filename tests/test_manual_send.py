"""人工发送（manual submit_mode）端到端回归。

流程：H3 / API 建单 → 本机点击开始 → 脚本配置页面并填入提示词（不点发送）
→ 操作者在页面上亲自发送 → 出片后点击关联下载（recall）→ 归档。

真实 Chromium + 本地合成 DOM；视频下载被 mock，不访问豆包官网，也不需要 ??????。
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.async_api import Page, APIRequestContext, async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
import web_api
from test_v3 import launch_chromium

MP4 = b"\x00\x00\x00\x20" + b"ftyp" + b"isom" + b"\x00" * 4 + b"\x00" * 4064

HTML = r'''<!doctype html><html lang="zh"><head><meta charset="utf-8"/></head><body>
<button type="button" data-testid="chat_input_action_model" onclick="document.getElementById('model-menu').hidden=false">Seedance 2.0 Mini</button>
<div id="model-menu" hidden>
<button type="button" role="menuitem">Seedance 2.5</button>
<button type="button" role="menuitem">Seedance 2.0</button>
<button type="button" role="menuitem">Seedance 2.0 Fast</button>
<button type="button" role="menuitem">Seedance 2.0 Mini</button>
</div>
<select data-testid="video-mode" aria-label="生成模式"><option value="text2video">文生视频</option></select>
<select data-testid="video-ratio" aria-label="画幅比例"><option>16:9</option><option>9:16</option></select>
<select data-testid="video-duration" aria-label="时长"><option value="1">1秒</option><option value="4">4秒</option></select>
<div data-testid="chat_input"><textarea id="prompt"></textarea><button type="submit">发送</button></div>
<div id="thread"><div data-testid="receive_message">你好，我是豆包。</div></div>
<script>
window.__sends = 0;
const thread=document.getElementById('thread'),editor=document.getElementById('prompt');
function say(html){const m=document.createElement('div');m.setAttribute('data-testid','receive_message');
  m.innerHTML=html;thread.appendChild(m);return m;}
document.querySelector('[data-testid="chat_input"] button').addEventListener('click',()=>{
  window.__sends += 1;
  editor.value='';
  say('本次使用 Seedance 2.0 Mini 生成，预计等待 5 分钟。');
  setTimeout(()=>say('视频生成完成。<div class="block-video-x"><video src="https://www.doubao.com/mock.mp4"></video></div>'),300);
});
</script></body></html>'''


def test_task_start_does_not_force_task_page_to_front():
    source = (
        Path(__file__).resolve().parents[1]
        / "src" / "doubao_manager" / "web" / "automation.py"
    ).read_text(encoding="utf-8")
    perform_body = source[source.index("async def perform("):]
    assert "bring_to_front" not in perform_body


@pytest.mark.parametrize('submit_mode', ['manual', 'auto'])
def test_manual_send_then_link_download(tmp_path, monkeypatch, submit_mode):
    monkeypatch.setenv('DOUBAO_TEST_MODE', '1')
    monkeypatch.setenv('DOUBAO_TEST_PAGE', 'http://localhost:7999/manual')
    monkeypatch.setenv('DOUBAO_POLL_SECONDS', '0.08')
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'manual.sqlite3')
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    with application.db() as conn:
        conn.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                     ('account_1', '账号一', '', str(tmp_path / 'p1'), '2026-01-01T00:00:00+00:00'))

    async def goto_mock(self, *args, **kwargs):
        await self.set_content(HTML)

    monkeypatch.setattr(Page, 'goto', goto_mock)

    class Response:
        ok = True
        status = 200
        headers = {'content-length': str(len(MP4))}

        async def body(self):
            return MP4

    async def get_mock(self, *args, **kwargs):
        return Response()

    monkeypatch.setattr(APIRequestContext, 'get', get_mock)

    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            ctx = await browser.new_context()

            async def fake_browser(account_id):
                assert account_id == 'account_1'
                return ctx

            bridge = FastAPI()
            web_api.mount_video_api(
                bridge, db=application.db, get_account=application.get_account,
                get_job=application.get_job, browser=fake_browser, base_dir=tmp_path,
                downloads_dir=tmp_path / 'videos', serialize=application.job_row)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge),
                                         base_url='http://local') as client:
                created = await client.post('/api/v1/videos/generations', json={
                    'account_id': 'account_1', 'project_id': '人工发送测试', 'episode_id': '1',
                    'shot_id': '1', 'prompt': '一只猫跳上窗台', 'mode': 'text2video',
                    'ratio': '16:9', 'duration': 1, 'submit_mode': submit_mode, 'auto_start': False,
                })
                assert created.status_code == 202, created.text
                body = created.json()
                jobid = body['id']
                assert body['status'] == 'queued'
                assert body['web']['submit_mode'] == submit_mode

                started = await client.post(f'/api/v1/videos/generations/{jobid}/prepare-manual')
                assert started.status_code == 200, started.text

                state = None
                for _ in range(120):
                    state = (await client.get(f'/api/v1/videos/generations/{jobid}')).json()
                    if state['status'] in ('awaiting_manual_send', 'needs_review', 'downloaded'):
                        break
                    await asyncio.sleep(0.1)
                assert state['status'] == 'awaiting_manual_send', state

                page = ctx.pages[-1]
                # 人工发送的核心保证：脚本绝不替操作者点发送。
                assert await page.evaluate('window.__sends') == 0
                assert await page.locator('#prompt').input_value() == '一只猫跳上窗台'
                assert await page.locator('.block-video-x').count() == 0
                state = (await client.get(f'/api/v1/videos/generations/{jobid}')).json()
                assert state['web']['submit_mode'] == submit_mode
                assert state['web']['result_card_index'] == 0

                # 无论任务原始模式为何，都可以通过管理台人工确认发送。
                sent = await client.post(
                    f'/api/v1/videos/generations/{jobid}/send?confirm_send=true')
                assert sent.status_code == 200, sent.text
                for _ in range(80):
                    if await page.locator('.block-video-x').count():
                        break
                    await asyncio.sleep(0.1)
                assert await page.evaluate('window.__sends') == 1

                linked = await client.post(f'/api/v1/videos/generations/{jobid}/recall')
                assert linked.status_code == 200, linked.text

                for _ in range(150):
                    state = (await client.get(f'/api/v1/videos/generations/{jobid}')).json()
                    if state['status'] in ('downloaded', 'needs_review', 'failed'):
                        break
                    await asyncio.sleep(0.1)
                assert state['status'] == 'downloaded', state
                assert state['web']['result_source'] == 'manual_web_link'
                archived = tmp_path / state['video_path']
                assert archived.read_bytes() == MP4
                for _ in range(30):
                    if page.is_closed():
                        break
                    await asyncio.sleep(0.05)
                assert page.is_closed()

            await browser.close()

    asyncio.run(run())


def test_resend_same_job_links_latest_page_and_keeps_job_id(tmp_path, monkeypatch):
    """原任务重新发送：job_id 不变，旧页面/旧结果作废，关联下载只认新页面。"""
    monkeypatch.setenv('DOUBAO_TEST_MODE', '1')
    monkeypatch.setenv('DOUBAO_TEST_PAGE', 'http://localhost:7999/manual')
    monkeypatch.setenv('DOUBAO_POLL_SECONDS', '0.08')
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'manual-resend.sqlite3')
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    with application.db() as conn:
        conn.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                     ('account_1', '账号一', '', str(tmp_path / 'p1'), '2026-01-01T00:00:00+00:00'))

    page_seq = 0

    async def goto_mock(self, *args, **kwargs):
        nonlocal page_seq
        page_seq += 1
        await self.set_content(HTML.replace(
            'https://www.doubao.com/mock.mp4',
            f'https://www.doubao.com/mock-{page_seq}.mp4'))

    monkeypatch.setattr(Page, 'goto', goto_mock)

    class Response:
        ok = True
        status = 200
        headers = {'content-length': str(len(MP4))}

        async def body(self):
            return MP4

    async def get_mock(self, *args, **kwargs):
        return Response()

    monkeypatch.setattr(APIRequestContext, 'get', get_mock)

    async def wait_status(client, jobid, allowed):
        state = None
        for _ in range(150):
            state = (await client.get(f'/api/v1/videos/generations/{jobid}')).json()
            if state['status'] in allowed:
                return state
            await asyncio.sleep(0.1)
        return state

    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            ctx = await browser.new_context()

            async def fake_browser(account_id):
                assert account_id == 'account_1'
                return ctx

            bridge = FastAPI()
            web_api.mount_video_api(
                bridge, db=application.db, get_account=application.get_account,
                get_job=application.get_job, browser=fake_browser, base_dir=tmp_path,
                downloads_dir=tmp_path / 'videos', serialize=application.job_row)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge),
                                         base_url='http://local') as client:
                created = await client.post('/api/v1/videos/generations', json={
                    'account_id': 'account_1', 'project_id': '重发测试', 'episode_id': '1',
                    'shot_id': '1', 'prompt': '一只猫跳上窗台', 'mode': 'text2video',
                    'ratio': '16:9', 'duration': 1, 'submit_mode': 'manual', 'auto_start': True,
                })
                assert created.status_code == 202, created.text
                jobid = created.json()['id']
                await client.post(f'/api/v1/videos/generations/{jobid}/start')
                state = await wait_status(client, jobid, ('awaiting_manual_send', 'needs_review', 'downloaded'))
                assert state['status'] == 'awaiting_manual_send', state

                # 第一次人工发送，产生旧页面结果；先不关联下载。
                page_a = ctx.pages[-1]
                await page_a.click('[data-testid="chat_input"] button')
                for _ in range(80):
                    if await page_a.locator('.block-video-x').count():
                        break
                    await asyncio.sleep(0.1)
                assert await page_a.evaluate('window.__sends') == 1
                assert await page_a.locator('.block-video-x video').get_attribute('src') == \
                    'https://www.doubao.com/mock-1.mp4'

                # 原任务重新发送：不新建 job。
                resent = await client.post(
                    f'/api/v1/videos/generations/{jobid}/resend?confirm_resend=true')
                assert resent.status_code == 200, resent.text
                state = await wait_status(client, jobid, ('awaiting_manual_send', 'needs_review', 'downloaded'))
                assert state['status'] == 'awaiting_manual_send', state
                assert state['id'] == jobid
                assert page_a.is_closed()

                page_b = ctx.pages[-1]
                assert page_b != page_a
                assert await page_b.evaluate('window.__sends') == 0
                assert await page_b.locator('#prompt').input_value() == '一只猫跳上窗台'
                assert await page_b.locator('.block-video-x').count() == 0

                # 操作者在重新准备的页面上发送并出片。
                await page_b.click('[data-testid="chat_input"] button')
                for _ in range(80):
                    if await page_b.locator('.block-video-x').count():
                        break
                    await asyncio.sleep(0.1)
                assert await page_b.evaluate('window.__sends') == 1
                assert await page_b.locator('.block-video-x video').get_attribute('src') == \
                    'https://www.doubao.com/mock-2.mp4'

                linked = await client.post(f'/api/v1/videos/generations/{jobid}/recall')
                assert linked.status_code == 200, linked.text
                state = await wait_status(client, jobid, ('downloaded', 'needs_review', 'failed'))
                assert state['status'] == 'downloaded', state
                assert state['id'] == jobid
                assert state['web']['video_url'] == 'https://www.doubao.com/mock-2.mp4'
                assert state['web']['result_source'] == 'manual_web_link'
                assert (tmp_path / state['video_path']).read_bytes() == MP4
                for _ in range(30):
                    if page_b.is_closed():
                        break
                    await asyncio.sleep(0.05)
                assert page_b.is_closed()
                # 不按发送次数扣费；最终视频成功下载后只结算一次。
                assert state['quota']['consumed_seconds'] == 1
                assert state['quota']['reserved_seconds'] == 0

                again = await client.post(
                    f'/api/v1/videos/generations/{jobid}/resend?confirm_resend=true')
                assert again.status_code == 409

            await browser.close()

    asyncio.run(run())


def test_manual_fail_verdict_is_visible_to_h3_and_releases_quota(tmp_path, monkeypatch):
    """人工判定失败：H3 看到 failed（不是 processing），额度释放。"""
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'manual-fail.sqlite3')
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    with application.db() as conn:
        conn.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                     ('account_1', '账号一', '', str(tmp_path / 'p1'), '2026-01-01T00:00:00+00:00'))

    async def fake_browser(account_id):
        raise AssertionError('人工判定失败不应打开浏览器')

    async def run():
        bridge = FastAPI()
        web_api.mount_video_api(
            bridge, db=application.db, get_account=application.get_account,
            get_job=application.get_job, browser=fake_browser, base_dir=tmp_path,
            downloads_dir=tmp_path / 'videos', serialize=application.job_row)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge),
                                     base_url='http://local') as client:
            created = await client.post('/api/v1/videos/generations', json={
                'account_id': 'account_1', 'prompt': '人工判定失败测试', 'mode': 'text2video',
                'duration': 1, 'submit_mode': 'manual', 'auto_start': True,
            })
            assert created.status_code == 202, created.text
            jobid = created.json()['id']
            assert created.json()['status'] == 'queued'

            no_confirm = await client.post(f'/api/v1/videos/generations/{jobid}/mark-failed')
            assert no_confirm.status_code == 422

            marked = await client.post(
                f'/api/v1/videos/generations/{jobid}/mark-failed?confirm_failed=true')
            assert marked.status_code == 200, marked.text
            body = marked.json()
            assert body['status'] == 'failed'
            assert body['web']['error_code'] == 'MANUAL_CONFIRMED_FAILED'
            assert '人工判定失败' in body['error_message']
            assert body['quota']['reserved_seconds'] == 0
            assert body['quota']['consumed_seconds'] == 0

            # H3 轮询用的单入口状态也带明确错误码，适配器据此返回 failed。
            status = await client.get(f'/api/v1/videos/{jobid}')
            assert status.status_code == 200
            assert status.json()['status'] == 'failed'
            assert status.json()['submit_mode'] == 'manual'
            assert status.json()['error_code'] == 'MANUAL_CONFIRMED_FAILED'

            # 幂等：重复判定不会重复结算。
            again = await client.post(
                f'/api/v1/videos/generations/{jobid}/mark-failed?confirm_failed=true')
            assert again.status_code == 200
            assert again.json()['status'] == 'failed'
            assert again.json()['quota']['reserved_seconds'] == 0

            # 自动任务不走人工判定。
            auto = await client.post('/api/v1/videos/generations', json={
                'account_id': 'account_1', 'prompt': '自动任务', 'mode': 'text2video',
                'duration': 1, 'submit_mode': 'auto', 'auto_start': False,
            })
            assert auto.status_code == 202, auto.text
            rejected = await client.post(
                f"/api/v1/videos/generations/{auto.json()['id']}/mark-failed?confirm_failed=true")
            assert rejected.status_code == 409

    asyncio.run(run())


def test_cancel_queued_task_releases_quota_and_allows_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'cancel.sqlite3')
    with TestClient(application.app) as client:
        account_id = client.post('/api/accounts', json={'name': 'cancel-account'}).json()['id']
        created = client.post('/api/v1/videos/generations', json={
            'account_id': account_id,
            'prompt': '取消任务测试',
            'duration': 4,
            'submit_mode': 'auto',
            'auto_start': False,
        })
        assert created.status_code == 202, created.text
        jobid = created.json()['id']

        assert client.post(f'/api/v1/videos/generations/{jobid}/cancel').status_code == 422
        canceled = client.post(
            f'/api/v1/videos/generations/{jobid}/cancel?confirm_cancel=true'
        )
        assert canceled.status_code == 200, canceled.text
        body = canceled.json()
        assert body['status'] == 'failed'
        assert body['web']['error_code'] == 'USER_CANCELED'
        assert body['error_message'] == '任务已由操作者取消'
        assert body['quota']['reserved_seconds'] == 0

        repeated = client.post(
            f'/api/v1/videos/generations/{jobid}/cancel?confirm_cancel=true'
        )
        assert repeated.status_code == 200
        deleted = client.post('/api/jobs/bulk-delete', json={'job_ids': [jobid]})
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()['deleted'] == 1


def test_cancel_task_pushes_canceled_status_to_h3(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'cancel-push.sqlite3')
    captured = {}

    class FakeResponse:
        is_error = False

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, data):
            captured.update(url=url, token=headers['X-Doubao-Push-Token'], data=data)
            return FakeResponse()

    monkeypatch.setattr(web_api.httpx, 'AsyncClient', FakeClient)
    token = 'cancel-push-token-1234567890'
    with TestClient(application.app) as client:
        created = client.post('/api/v1/videos/generations', json={
            'prompt': '取消并同步 H3',
            'auto_start': False,
            'result_push_url': 'http://127.0.0.1:3013/api/v1/doubao-results/8758',
            'result_push_token': token,
        })
        assert created.status_code == 202, created.text
        jobid = created.json()['id']

        canceled = client.post(
            f'/api/v1/videos/generations/{jobid}/cancel?confirm_cancel=true'
        )
        assert canceled.status_code == 200, canceled.text
        assert canceled.json()['web']['delivery_state'] == 'delivered'
        assert captured == {
            'url': 'http://127.0.0.1:3013/api/v1/doubao-results/8758',
            'token': token,
            'data': {'status': 'canceled', 'error_code': 'USER_CANCELED'},
        }


def test_restart_during_manual_send_releases_quota_until_download(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'manual-restart.sqlite3')
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    with application.db() as conn:
        conn.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                     ('account_1', '账号一', '', str(tmp_path / 'p1'), '2026-01-01T00:00:00+00:00'))
        conn.execute("INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)"
                     " VALUES(?,?,?,?,?,?,?,?,?)",
                     ('job_manual', 'p', 'e', 's', '提示词', 'account_1',
                      web_api.MANUAL_WAIT, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'))
        conn.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,mode,assets_json,quota_day,quota_state,submit_mode)"
                     " VALUES(?,?,?,?,?,?,?,?)",
                     ('job_manual', '16:9', 4, 'text2video', '{}', web_api.local_day(), 'reserved', 'manual'))
        web_api.reserve_quota(conn, 'account_1', 4)

    web_api.recover_interrupted_jobs(application.db)
    with application.db() as conn:
        job = conn.execute("SELECT status,error_message FROM jobs WHERE id='job_manual'").fetchone()
        web = conn.execute("SELECT quota_state,error_code FROM video_web_jobs WHERE job_id='job_manual'").fetchone()
        quota = web_api.quota_summary(conn, 'account_1')
    assert job['status'] == 'needs_review'
    assert '人工发送' in job['error_message']
    assert web['error_code'] == 'MANUAL_SEND_INTERRUPTED'
    # 未成功下载不能扣费；中断转人工复查时释放预占。
    assert web['quota_state'] == 'released'
    assert quota['reserved_seconds'] == 0
    assert quota['consumed_seconds'] == 0

"""Negative / safety tests. Only synthetic HTML in a local Chromium process.

No live Doubao browsing, account session, or generation costs are used here.
"""
import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import Page, async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_v3 import HTML, launch_chromium
from media_fixture import sample_mp4_bytes
import app as application
import web_api
from web_api import (ReviewNeeded, VideoRequest, perform, is_safe_media,
                     quota_summary, recover_interrupted_jobs, settle_quota)


def test_nuisance_dialogs_are_closed_but_business_dialogs_remain():
    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            page = await browser.new_page()
            try:
                await page.set_content("""
                  <div id="nuisance" role="dialog">
                    <h2>新功能介绍</h2>
                    <button aria-label="关闭" onclick="this.parentElement.remove()">×</button>
                  </div>
                  <div id="banner" style="position:fixed;inset:10px auto auto 10px;width:200px;height:80px">
                    <span>活动公告</span>
                    <button title="关闭" onclick="this.parentElement.remove()">×</button>
                  </div>
                  <div id="verification" role="dialog">
                    <h2>安全验证</h2><button aria-label="关闭">×</button>
                  </div>
                  <div id="confirmation" role="dialog">
                    <h2>确认生成，将消耗额度</h2><button aria-label="关闭">×</button>
                  </div>
                  <div id="detail" role="dialog">
                    <h2>视频详情</h2><video></video><button aria-label="关闭">×</button>
                  </div>
                """)
                assert await web_api.dismiss_blocking_dialogs(page) == 2
                assert await page.locator('#nuisance').count() == 0
                assert await page.locator('#banner').count() == 0
                assert await page.locator('#verification').count() == 1
                assert await page.locator('#confirmation').count() == 1
                assert await page.locator('#detail').count() == 1
                assert await web_api.dismiss_blocking_dialogs(page) == 0
            finally:
                await browser.close()

    asyncio.run(run())


def test_uncertain_quota_remains_reserved_until_recalled(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    application.init_db()
    recover_interrupted_jobs(application.db)
    with application.db() as c:
        c.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                  ('acc_unknown', 'unknown', '', str(tmp_path / 'profile'), '2026-10-28T00:00:00+00:00'))
        c.execute("""INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?)""",
                  ('job_unknown', '', '', '', 'prompt', 'acc_unknown', 'generating',
                   '2026-10-28T00:00:00+00:00', '2026-10-28T00:00:00+00:00'))
        c.execute("""INSERT INTO video_web_jobs(job_id,ratio,duration,quota_day,quota_state)
                     VALUES(?,?,?,?,?)""", ('job_unknown', '16:9', 15, '2026-10-28', 'reserved'))
        c.execute("""INSERT INTO account_quota(account_id,day,reserved_seconds,consumed_seconds)
                     VALUES(?,?,?,?)""", ('acc_unknown', '2026-10-28', 15, 0))
        settle_quota(c, 'job_unknown', 'needs_review', prior_status='generating', error_code='WAIT_TIMEOUT')
        summary = quota_summary(c, 'acc_unknown', '2026-10-28')
        assert summary['reserved_seconds'] == 15
        assert summary['unknown_seconds'] == 15
        assert c.execute("SELECT quota_state FROM video_web_jobs WHERE job_id='job_unknown'").fetchone()['quota_state'] == 'unknown'
        settle_quota(c, 'job_unknown', 'downloaded')
        summary = quota_summary(c, 'acc_unknown', '2026-10-28')
        assert summary['reserved_seconds'] == 0
        assert summary['consumed_seconds'] == 15
        assert summary['unknown_seconds'] == 0


def test_recall_uses_persistent_database_lock(tmp_path, monkeypatch):
    from fastapi import FastAPI
    import web_api

    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    application.init_db()
    recover_interrupted_jobs(application.db)
    with application.db() as c:
        c.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                  ('acc_lock', 'lock', '', str(tmp_path / 'profile'), '2026-10-28T00:00:00+00:00'))
        c.execute("""INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?)""",
                  ('job_lock', '', '', '', 'prompt', 'acc_lock', 'needs_review',
                   '2026-10-28T00:00:00+00:00', '2026-10-28T00:00:00+00:00'))
        c.execute("""INSERT INTO video_web_jobs(job_id,ratio,duration,web_page_url,recall_state,recall_token,recall_started_at)
                     VALUES(?,?,?,?,?,?,?)""",
                  ('job_lock', '16:9', 15, 'https://www.doubao.com/chat/locked',
                   'recalling', 'other-process', '9999-01-01T00:00:00+00:00'))

    async def unused_browser(_account_id):
        raise AssertionError('数据库锁冲突时不应打开浏览器')

    app2 = FastAPI()
    web_api.mount_video_api(app2, db=application.db, get_account=application.get_account,
                            get_job=application.get_job, browser=unused_browser,
                            base_dir=tmp_path, downloads_dir=tmp_path / 'videos',
                            serialize=application.job_row)
    with TestClient(app2) as client:
        result = client.post('/api/v1/videos/generations/job_lock/recall')
        assert result.status_code == 409
        assert '召回操作正在执行' in result.text


def test_force_redownload_keeps_archived_status_while_recalling(tmp_path, monkeypatch):
    from fastapi import FastAPI

    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    application.init_db()
    recover_interrupted_jobs(application.db)
    with application.db() as c:
        c.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                  ('acc_arch', 'arch', '', str(tmp_path / 'profile'), '2026-10-28T00:00:00+00:00'))
        c.execute("""INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?)""",
                  ('job_arch', '', '', '', 'prompt', 'acc_arch', 'downloaded',
                   '2026-10-28T00:00:00+00:00', '2026-10-28T00:00:00+00:00'))
        c.execute("""INSERT INTO video_web_jobs(job_id,ratio,duration,web_page_url)
                     VALUES(?,?,?,?)""",
                  ('job_arch', '16:9', 4, 'https://www.doubao.com/chat/archived'))

    entered = threading.Event()
    release = threading.Event()

    async def blocking_browser(_account_id):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.02)
        raise RuntimeError('forced recall failure')

    app2 = FastAPI()
    web_api.mount_video_api(app2, db=application.db, get_account=application.get_account,
                            get_job=application.get_job, browser=blocking_browser,
                            base_dir=tmp_path, downloads_dir=tmp_path / 'videos',
                            serialize=application.job_row)
    with TestClient(app2) as client:
        started = client.post('/api/v1/videos/generations/job_arch/recall?force_redownload=true')
        assert started.status_code == 200, started.text
        assert entered.wait(2)

        # The archived bucket is stable while the artifact is re-downloaded.
        during = client.get('/api/v1/videos/generations/job_arch').json()
        assert during['status'] == 'downloaded'
        assert during['web']['recall_state'] == 'recalling'

        release.set()
        state = None
        for _ in range(100):
            state = client.get('/api/v1/videos/generations/job_arch').json()
            if state['web']['recall_state'] == '':
                break
            time.sleep(0.02)
        assert state['status'] == 'downloaded'
        assert state['web']['recall_state'] == ''


@pytest.mark.parametrize('case,expected', [
    ('mode_absent', 'SETTING_NOT_FOUND'),
    ('duration_absent', 'SETTING_NOT_FOUND'),
    ('upload_slot_absent', 'UPLOAD_SLOT_NOT_FOUND'),
    ('upload_not_confirmed', 'UPLOAD_UNVERIFIED'),
    ('captcha', 'VERIFICATION_REQUIRED'),
    ('quota', 'ACCOUNT_LIMIT'),
    ('login', 'LOGIN_REQUIRED'),
    ('submit_absent', 'SUBMIT_NOT_FOUND'),
    ('never_finishes', 'WAIT_TIMEOUT'),
    ('historical_video_only', 'WAIT_TIMEOUT'),
])
def test_fail_closed_no_false_success(case, expected, tmp_path, monkeypatch):
    monkeypatch.setenv('DOUBAO_TEST_MODE', '1')
    monkeypatch.setenv('DOUBAO_TEST_PAGE', 'http://localhost:8001/test')
    monkeypatch.setenv('DOUBAO_UPLOAD_VERIFY_TIMEOUT_MS', '200')
    monkeypatch.setenv('DOUBAO_POLL_SECONDS', '0.04')
    monkeypatch.setenv('DOUBAO_RESULT_TIMEOUT', '0.7')
    html = HTML
    if case == 'mode_absent': html = html.replace('data-testid="video-mode"', 'data-testid="no-mode"')
    if case == 'duration_absent': html = html.replace('data-testid="video-duration"', 'data-testid="no-duration"')
    if case == 'upload_slot_absent': html = html.replace('data-slot="first" type="file"', 'data-slot="unknown" type="file"')
    if case == 'upload_not_confirmed':
        html = html.replace("status.setAttribute('data-asset-status','uploaded')", "status.setAttribute('data-asset-status','pending')")
    if case == 'captcha': html = html.replace('<div id="message"></div>', '<div id="message">请输入验证码</div>')
    if case == 'quota': html = html.replace('<div id="message"></div>', '<div id="message">次数已用完</div>')
    if case == 'login': html = html.replace('<div id="message"></div>', '<div id="message">请先登录</div>')
    if case == 'submit_absent': html = html.replace('type="submit" id="generate"', 'type="button" id="generate"')
    if case == 'never_finishes': html = html.replace('setTimeout(complete,250);', '// never completes\n')
    if case == 'historical_video_only':
        html = html.replace('<div id="message"></div>', '<div id="message">生成完成</div><video src="https://www.doubao.com/old.mp4"></video>')
        html = html.replace('setTimeout(complete,250);', 'setTimeout(()=>{msg.innerText="生成完成";result.dataset.generationStatus="completed";},250);')
    async def goto_fake(self, *args, **kwargs): await self.set_content(html)
    monkeypatch.setattr(Page, 'goto', goto_fake)
    mode = 'image2video' if case.startswith('upload_') else 'text2video'
    req = VideoRequest(account_id='fixture', mode=mode, prompt='测试防误报',
                       first_frame_asset_id='image1' if mode == 'image2video' else None)
    img = tmp_path / 'picture.png'
    if mode != 'text2video':
        from test_v3 import png_bytes
        img.write_bytes(png_bytes())
    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            context = await browser.new_context()
            statuses = []
            review_pages = []
            async def on_status(s, msg): statuses.append(s)
            async def on_page(url): pass
            async def on_review_page(page): review_pages.append(page)
            try:
                with pytest.raises(ReviewNeeded) as err:
                    await perform(context, req, {'image1': img}, tmp_path / 'must_not_exist.mp4',
                                  on_status, on_page, on_review_page=on_review_page)
                assert err.value.code == expected
                # Every task is bound to its dedicated page from the start, not only
                # after login/captcha failures require manual review.
                assert review_pages == [context.pages[-1]]
                assert 'downloading' not in statuses
                assert not (tmp_path / 'must_not_exist.mp4').exists()
                assert not (tmp_path / 'must_not_exist.webm').exists()
                if case in ('captcha','quota','login','mode_absent','duration_absent','upload_slot_absent','upload_not_confirmed','submit_absent'):
                    assert await context.pages[-1].locator('[data-generation-status="running"]').count() == 0
            finally:
                await browser.close()
    asyncio.run(run())


def test_accepted_video_hosts():
    assert is_safe_media('https://foo.douyinvod.com/a.mp4')
    assert is_safe_media('https://www.doubao.com/a.mp4')
    assert not is_safe_media('http://foo.doubao.com/a.mp4')
    assert not is_safe_media('https://doubao.com.evil.example/a.mp4')
    assert not is_safe_media('file:///etc/hosts')
    assert not is_safe_media('https://127.0.0.1:8000/a.mp4')


def test_review_manual_import_and_no_false_reconcile(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    video = tmp_path / 'valid.mp4'
    video.write_bytes(sample_mp4_bytes())
    with TestClient(application.app) as client:
        acc = client.post('/api/accounts', json={'name':'人审账号'}).json()['id']
        resp = client.post('/api/v1/videos/generations',json={'account_id':acc,'prompt':'镜头01','auto_start':False})
        assert resp.status_code == 202, resp.text
        job = resp.json()['id']
        with application.db() as c:
            c.execute("UPDATE jobs SET status='generating' WHERE id=?", (job,))
            c.execute("UPDATE video_web_jobs SET web_page_url='https://www.doubao.com/' WHERE job_id=?", (job,))
        recover_interrupted_jobs(application.db)
        # A generic completed-history page must not be attributed to an interrupted job.
        reconciliation = client.post(f'/api/v1/videos/generations/{job}/reconcile')
        assert reconciliation.status_code == 409
        endpoint = f'/api/v1/videos/generations/{job}/manual-result'
        def upload(b, filename='valid.mp4', confirmation='true'):
            return client.post(endpoint,data={'confirm_review':confirmation},files={'file':(filename,b,'video/mp4')})
        assert upload(video.read_bytes(),confirmation='false').status_code == 422
        assert upload(b'not really a video').status_code == 422
        assert upload(video.read_bytes(),filename='bad.exe').status_code == 415
        success = upload(video.read_bytes())
        assert success.status_code == 200,success.text
        assert success.json()['status'] == 'downloaded'
        assert success.json()['web']['result_source'] == 'human_verified_upload'
        assert client.get(f'/api/jobs/{job}/video').content == video.read_bytes()
        assert upload(video.read_bytes()).status_code == 409
        events = client.get(f'/api/v1/videos/generations/{job}/events').json()
        assert any('sha256=' in e['message'] for e in events)
        jobs = client.get('/api/jobs').json()
        assert any(x['id'] == job and x['is_web_generation'] for x in jobs)


def test_web_job_prepare_rejected_and_ready_recovered(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    with TestClient(application.app) as client:
        acc = client.post('/api/accounts', json={'name': '状态机账号'}).json()['id']
        resp = client.post('/api/v1/videos/generations',
                           json={'account_id': acc, 'prompt': '镜头', 'auto_start': False})
        assert resp.status_code == 202, resp.text
        job = resp.json()['id']

        # The legacy manual-prepare endpoint must not touch a web job: it used to
        # overwrite needs_review with ready and hide the 复查归档 action.
        assert client.post(f'/api/jobs/{job}/prepare').status_code == 409

        with application.db() as c:
            c.execute("UPDATE jobs SET status='ready' WHERE id=?", (job,))
        recover_interrupted_jobs(application.db)
        row = next(x for x in client.get('/api/jobs').json() if x['id'] == job)
        assert row['status'] == 'needs_review'
        events = client.get(f'/api/v1/videos/generations/{job}/events').json()
        assert any('人工准备' in e['message'] for e in events)

        # The confirmation gate is a live worker state too: a restart there must never
        # leave the job stranded in "confirming" with no task polling in the process.
        with application.db() as c:
            c.execute("UPDATE jobs SET status='confirming' WHERE id=?", (job,))
        recover_interrupted_jobs(application.db)
        row = next(x for x in client.get('/api/jobs').json() if x['id'] == job)
        assert row['status'] == 'needs_review'


def test_jobs_expose_task_page_and_open_page_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    with TestClient(application.app) as client:
        acc = client.post('/api/accounts', json={'name': '跳转账号'}).json()['id']
        resp = client.post('/api/v1/videos/generations',
                           json={'account_id': acc, 'prompt': '镜头', 'auto_start': False})
        job = resp.json()['id']

        row = next(x for x in client.get('/api/jobs').json() if x['id'] == job)
        assert row['is_web_generation'] and row['web_page_url'] == '' and not row['has_task_page']
        assert client.post(f'/api/v1/videos/generations/{job}/open-page').status_code == 409

        # A generic /chat/ page cannot identify this job's conversation.
        with application.db() as c:
            c.execute("UPDATE video_web_jobs SET web_page_url='https://www.doubao.com/chat/' WHERE job_id=?", (job,))
        assert client.post(f'/api/v1/videos/generations/{job}/open-page').status_code == 409

        with application.db() as c:
            c.execute("UPDATE video_web_jobs SET web_page_url='https://www.doubao.com/chat/38449850653386498' WHERE job_id=?", (job,))
        row = next(x for x in client.get('/api/jobs').json() if x['id'] == job)
        assert row['has_task_page']

        # A temporary /chat/local_<id> page still identifies this job's conversation.
        with application.db() as c:
            c.execute("UPDATE video_web_jobs SET web_page_url='https://www.doubao.com/chat/local_2001034190155126' WHERE job_id=?", (job,))
        row = next(x for x in client.get('/api/jobs').json() if x['id'] == job)
        assert row['has_task_page']

        # Untrusted hosts are rejected before any browser is launched.
        with application.db() as c:
            c.execute("UPDATE video_web_jobs SET web_page_url='https://evil.example/chat/123' WHERE job_id=?", (job,))
        assert client.post(f'/api/v1/videos/generations/{job}/open-page').status_code == 422

        # A reserved needs_review job is flagged as already manually confirmed.
        with application.db() as c:
            c.execute("UPDATE jobs SET status='needs_review' WHERE id=?", (job,))
            c.execute("UPDATE video_web_jobs SET web_page_url=?, quota_state='reserved' WHERE job_id=?",
                      ('https://www.doubao.com/chat/38449850653386498', job))
        row = next(x for x in client.get('/api/jobs').json() if x['id'] == job)
        assert row['manual_confirm_sent'] is True


def test_open_page_reuses_or_opens_managed_tab(tmp_path, monkeypatch):
    from fastapi import FastAPI
    import web_api

    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    target = 'https://www.doubao.com/chat/38449850653386498'
    with application.db() as c:
        c.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                  ('acc_open', 'jump', '', str(tmp_path / 'profile'), '2026-10-28T00:00:00+00:00'))
        c.execute("INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  ('job_open', 'p', 'e', 's', 'prompt', 'acc_open', 'needs_review',
                   '2026-10-28T00:00:00+00:00', '2026-10-28T00:00:00+00:00'))
        c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,web_page_url) VALUES(?,?,?,?)",
                  ('job_open', '16:9', 4, target))

    class Page:
        def __init__(self, url):
            self.url = url
            self.front = False
            self.closed = False

        async def bring_to_front(self):
            self.front = True

        async def goto(self, url, **_kwargs):
            self.url = url

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    class Ctx:
        def __init__(self, pages):
            self.pages = list(pages)
            self.created = []
            self.closed = False

        async def new_page(self):
            page = Page('about:blank')
            self.created.append(page)
            self.pages.append(page)
            return page

        async def close(self):
            self.closed = True

    def build(ctx):
        async def fake_browser(account_id):
            assert account_id == 'acc_open'
            return ctx
        app2 = FastAPI()
        web_api.mount_video_api(app2, db=application.db, get_account=application.get_account,
                                get_job=application.get_job, browser=fake_browser,
                                base_dir=application.BASE_DIR, downloads_dir=application.DOWNLOADS_DIR,
                                serialize=application.job_row)
        return app2

    existing = Page(target)
    ctx = Ctx([existing])
    app_existing = build(ctx)
    with TestClient(app_existing) as client:
        r = client.post('/api/v1/videos/generations/job_open/open-page')
        assert r.status_code == 200, r.text
        assert r.json()['reused'] is True and existing.front and not ctx.created
        assert app_existing.state.video_prepared_pages['job_open'] is existing

    ctx2 = Ctx([])
    app_created = build(ctx2)
    with TestClient(app_created) as client:
        r = client.post('/api/v1/videos/generations/job_open/open-page')
        assert r.status_code == 200, r.text
        assert r.json()['reused'] is False
        assert ctx2.created and ctx2.created[0].url == target and ctx2.created[0].front
        assert app_created.state.video_prepared_pages['job_open'] is ctx2.created[0]


def test_open_window_targets_only_the_bound_task_card(tmp_path, monkeypatch):
    from fastapi import FastAPI
    import web_api

    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    target = 'https://www.doubao.com/chat/38449850653386498'
    other = 'https://www.doubao.com/chat/38449850653386499'
    with application.db() as c:
        c.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                  ('acc_window', 'window', '', str(tmp_path / 'profile'),
                   '2026-10-28T00:00:00+00:00'))
        c.execute("INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  ('job_window', 'GB-上端', '第1集', '9', 'prompt', 'acc_window',
                   'downloaded', '2026-10-28T00:00:00+00:00',
                   '2026-10-28T00:00:00+00:00'))
        c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,web_page_url,result_card_index)"
                  " VALUES(?,?,?,?,?)",
                  ('job_window', '16:9', 4, target, 1))

    class Page:
        def __init__(self, url):
            self.url = url
            self.front = False
            self.closed = False

        async def goto(self, url, **_kwargs):
            self.url = url

        async def wait_for_timeout(self, _ms):
            return None

        async def bring_to_front(self):
            self.front = True

        def is_closed(self):
            return self.closed

    class Ctx:
        def __init__(self):
            self.pages = [Page(other)]
            self.created = []

        async def new_page(self):
            page = Page('about:blank')
            self.pages.append(page)
            self.created.append(page)
            return page

    ctx = Ctx()
    clicks = []

    async def fake_browser(account_id):
        assert account_id == 'acc_window'
        return ctx

    async def fake_read_state(_page):
        return {'url': target, 'videoCards': 3, 'videoUrls': []}

    async def fake_click(_page, card_index):
        clicks.append(card_index)
        return True

    monkeypatch.setattr(web_api, 'read_state', fake_read_state)
    monkeypatch.setattr(web_api, 'click_result_card', fake_click)
    app2 = FastAPI()
    web_api.mount_video_api(
        app2, db=application.db, get_account=application.get_account,
        get_job=application.get_job, browser=fake_browser,
        existing_browser=lambda _account_id: ctx,
        base_dir=application.BASE_DIR, downloads_dir=application.DOWNLOADS_DIR,
        serialize=application.job_row,
    )

    with TestClient(app2) as client:
        r = client.post('/api/v1/videos/generations/job_window/open-window')
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['window_reused'] is True
        assert body['page_reused'] is False
        assert body['video_card_index'] == 1
        assert body['card_clicked'] is True
        assert body['session_id'] == '38449850653386498'
        assert clicks == [1]
        assert ctx.created and ctx.created[0].front
        assert ctx.pages[0].url == other

    with application.db() as c:
        row = c.execute(
            "SELECT session_url,session_id FROM video_web_jobs WHERE job_id='job_window'"
        ).fetchone()
    assert row['session_url'] == target
    assert row['session_id'] == '38449850653386498'


def test_open_window_refuses_to_guess_ambiguous_video_card(tmp_path, monkeypatch):
    from fastapi import FastAPI
    import web_api

    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    monkeypatch.setenv('DOUBAO_TASK_LOCATE_TIMEOUT', '0')
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    target = 'https://www.doubao.com/chat/38449850653386498'
    with application.db() as c:
        c.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                  ('acc_ambiguous', 'ambiguous', '', str(tmp_path / 'profile'),
                   '2026-10-28T00:00:00+00:00'))
        c.execute("INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  ('job_ambiguous', 'p', 'e', 's', 'prompt', 'acc_ambiguous',
                   'needs_review', '2026-10-28T00:00:00+00:00',
                   '2026-10-28T00:00:00+00:00'))
        c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,web_page_url)"
                  " VALUES(?,?,?,?)",
                  ('job_ambiguous', '16:9', 4, target))

    class Page:
        url = target

        async def wait_for_timeout(self, _ms):
            return None

        async def bring_to_front(self):
            return None

    class Ctx:
        pages = [Page()]

        async def new_page(self):
            raise AssertionError('an existing task tab should be reused')

    async def fake_browser(_account_id):
        return Ctx()

    clicked = []

    async def fake_read_state(_page):
        return {'url': target, 'videoCards': 2, 'videoUrls': []}

    async def fake_click(_page, card_index):
        clicked.append(card_index)
        return True

    monkeypatch.setattr(web_api, 'read_state', fake_read_state)
    monkeypatch.setattr(web_api, 'click_result_card', fake_click)
    app2 = FastAPI()
    web_api.mount_video_api(
        app2, db=application.db, get_account=application.get_account,
        get_job=application.get_job, browser=fake_browser,
        existing_browser=lambda _account_id: Ctx(),
        base_dir=application.BASE_DIR, downloads_dir=application.DOWNLOADS_DIR,
        serialize=application.job_row,
    )

    with TestClient(app2) as client:
        r = client.post('/api/v1/videos/generations/job_ambiguous/open-window')
        assert r.status_code == 409
        assert '缺少视频卡片绑定' in r.json()['detail']
        assert '不会重新生成' in r.json()['detail']
    assert clicked == []
    with application.db() as c:
        row = c.execute(
            "SELECT status FROM jobs WHERE id='job_ambiguous'"
        ).fetchone()
    assert row['status'] == 'needs_review'


def test_open_page_and_download_can_supersede_stalled_generation(tmp_path, monkeypatch):
    from fastapi import FastAPI
    import web_api

    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    target_page = 'https://www.doubao.com/chat/38449850653386498'
    with application.db() as c:
        c.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                  ('acc_active', 'active', '', str(tmp_path / 'profile'), '2026-10-28T00:00:00+00:00'))
        c.execute("INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  ('job_active', 'p', 'e', 's', 'prompt', 'acc_active', 'generating',
                   '2026-10-28T00:00:00+00:00', '2026-10-28T00:00:00+00:00'))
        c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,web_page_url,result_card_index)"
                  " VALUES(?,?,?,?,?)",
                  ('job_active', '16:9', 4, target_page, 0))

    class Page:
        def __init__(self, url='about:blank'):
            self.url = url
            self.front = False
            self.closed = False

        async def goto(self, url, **_kwargs):
            self.url = url

        async def wait_for_timeout(self, _ms):
            return None

        async def bring_to_front(self):
            self.front = True

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    class Ctx:
        def __init__(self):
            self.pages = []
            self.created = []

        async def new_page(self):
            page = Page()
            self.pages.append(page)
            self.created.append(page)
            return page

    ctx = Ctx()

    async def fake_browser(_account_id):
        return ctx

    async def fake_read_state(_page):
        return {'failed': False, 'runningLast': False, 'awaitingConfirm': False,
                'videoUrls': [], 'videoCards': 1, 'text': ''}

    async def fake_download_result(_page, _known, target, **_kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'\x00\x00\x00\x20ftyp' + b'\x00' * 512)
        return {'video_url': 'https://v26-vdl.doubao.com/mock.mp4', 'output': str(target)}

    monkeypatch.setattr(web_api, 'read_state', fake_read_state)
    monkeypatch.setattr(web_api, 'download_result_video', fake_download_result)
    monkeypatch.setattr(web_api, 'media_duration_seconds', lambda _path: 4.0)

    app2 = FastAPI()
    web_api.mount_video_api(app2, db=application.db, get_account=application.get_account,
                            get_job=application.get_job, browser=fake_browser,
                            base_dir=tmp_path, downloads_dir=tmp_path / 'videos',
                            serialize=application.job_row)
    canceled = threading.Event()

    async def stalled_generation():
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            canceled.set()
            raise

    task_holder = {}

    def create_stalled_generation():
        task_holder['task'] = asyncio.create_task(stalled_generation())

    with TestClient(app2) as client:
        client.portal.call(create_stalled_generation)
        app2.state.video_pending['job_active'] = task_holder['task']

        opened = client.post('/api/v1/videos/generations/job_active/open-page')
        assert opened.status_code == 200, opened.text
        assert opened.json()['reused'] is False
        assert ctx.created[0].front

        downloaded = client.post('/api/v1/videos/generations/job_active/recall?force_redownload=true')
        assert downloaded.status_code == 200, downloaded.text
        assert canceled.wait(2)
        state = None
        for _ in range(100):
            state = client.get('/api/v1/videos/generations/job_active').json()
            if state['status'] == 'downloaded':
                break
            time.sleep(0.02)
        assert state['status'] == 'downloaded'
        assert state['web']['recall_state'] == ''


def test_close_page_only_closes_matching_tab_without_launching_browser(tmp_path, monkeypatch):
    from fastapi import FastAPI
    import web_api

    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    target = 'https://www.doubao.com/chat/38449850653386498'
    other = 'https://www.doubao.com/chat/38449850653386499'
    with application.db() as c:
        for account_id in ('acc_close', 'acc_other'):
            c.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                      (account_id, account_id, '', str(tmp_path / account_id), '2026-10-28T00:00:00+00:00'))
        for job_id, account_id, page_url in (
            ('job_close', 'acc_close', target),
            ('job_sibling', 'acc_close', other),
            ('job_other_account', 'acc_other', target),
        ):
            c.execute("INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)"
                      " VALUES(?,?,?,?,?,?,?,?,?)",
                      (job_id, 'p', 'e', job_id, 'prompt', account_id, 'downloaded',
                       '2026-10-28T00:00:00+00:00', '2026-10-28T00:00:00+00:00'))
            c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,web_page_url) VALUES(?,?,?,?)",
                      (job_id, '16:9', 4, page_url))

    class Page:
        def __init__(self, url):
            self.url = url
            self.closed = False

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    class Ctx:
        def __init__(self, pages):
            self.pages = list(pages)
            self.closed = False
            self.new_page_calls = 0

        async def new_page(self):
            self.new_page_calls += 1
            raise AssertionError('close-page must not create a tab')

        async def close(self):
            self.closed = True

    target_page = Page(target)
    sibling_page = Page(other)
    other_account_page = Page(target)
    contexts = {
        'acc_close': Ctx([target_page, sibling_page]),
        'acc_other': Ctx([other_account_page]),
    }
    requested_accounts = []

    async def fake_browser(_account_id):
        raise AssertionError('close-page must not launch Chrome')

    def existing_browser(account_id):
        requested_accounts.append(account_id)
        return contexts.get(account_id)

    app2 = FastAPI()
    web_api.mount_video_api(app2, db=application.db, get_account=application.get_account,
                            get_job=application.get_job, browser=fake_browser,
                            existing_browser=existing_browser,
                            base_dir=application.BASE_DIR, downloads_dir=application.DOWNLOADS_DIR,
                            serialize=application.job_row)
    with TestClient(app2) as client:
        r = client.post('/api/v1/videos/generations/job_close/close-page')
        assert r.status_code == 200, r.text
        assert r.json()['closed'] is True
        assert target_page.closed is True
        assert sibling_page.closed is False
        assert other_account_page.closed is False
        assert contexts['acc_close'].closed is False
        assert contexts['acc_close'].new_page_calls == 0
        assert requested_accounts == ['acc_close']

        prepared_page = Page(target)
        app2.state.video_prepared_pages['job_close'] = prepared_page
        r = client.post('/api/v1/videos/generations/job_close/close-page')
        assert r.status_code == 200, r.text
        assert r.json()['closed'] is True
        assert prepared_page.closed is True
        assert contexts['acc_close'].closed is False
        assert requested_accounts == ['acc_close']

        r = client.post('/api/v1/videos/generations/job_close/close-page')
        assert r.status_code == 200, r.text
        assert r.json()['closed'] is False and r.json()['already_closed'] is True
        assert sibling_page.closed is False


def test_close_window_only_closes_the_job_account_without_changing_job(tmp_path, monkeypatch):
    from fastapi import FastAPI
    import web_api

    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    with application.db() as c:
        for account_id in ('acc_close_window', 'acc_keep_window'):
            c.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                      (account_id, account_id, '', str(tmp_path / account_id),
                       '2026-10-28T00:00:00+00:00'))
        for job_id, account_id in (
            ('job_close_window', 'acc_close_window'),
            ('job_keep_window', 'acc_keep_window'),
        ):
            c.execute("INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)"
                      " VALUES(?,?,?,?,?,?,?,?,?)",
                      (job_id, 'p', 'e', job_id, 'prompt', account_id, 'downloaded',
                       '2026-10-28T00:00:00+00:00',
                       '2026-10-28T00:00:00+00:00'))
            c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration) VALUES(?,?,?)",
                      (job_id, '16:9', 4))

    contexts = {
        'acc_close_window': object(),
        'acc_keep_window': object(),
    }
    close_calls = []

    async def fake_browser(_account_id):
        raise AssertionError('close-window must not launch or activate Chrome')

    async def fake_close_browser(account_id):
        close_calls.append(account_id)

    app2 = FastAPI()
    web_api.mount_video_api(
        app2, db=application.db, get_account=application.get_account,
        get_job=application.get_job, browser=fake_browser,
        existing_browser=contexts.get, close_browser=fake_close_browser,
        base_dir=application.BASE_DIR, downloads_dir=application.DOWNLOADS_DIR,
        serialize=application.job_row,
    )

    with TestClient(app2) as client:
        r = client.post('/api/v1/videos/generations/job_close_window/close-window')
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['closed'] is True
        assert body['browser_open'] is False
        assert body['status'] == 'downloaded'
        assert close_calls == ['acc_close_window']

    with application.db() as c:
        job = c.execute(
            "SELECT status FROM jobs WHERE id='job_close_window'"
        ).fetchone()
        result = c.execute(
            "SELECT video_url,result_source FROM video_web_jobs "
            "WHERE job_id='job_close_window'"
        ).fetchone()
    assert job['status'] == 'downloaded'
    assert result['video_url'] == ''
    assert result['result_source'] == ''


def test_download_result_requires_review_and_specific_page(tmp_path, monkeypatch):
    import web_api
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    with TestClient(application.app) as client:
        acc = client.post('/api/accounts', json={'name': '抓取账号'}).json()['id']
        resp = client.post('/api/v1/videos/generations',
                           json={'account_id': acc, 'prompt': '镜头', 'auto_start': False})
        job = resp.json()['id']
        endpoint = f'/api/v1/videos/generations/{job}/download-result'

        # Still queued: only needs_review jobs may be recovered.
        assert client.post(endpoint + '?confirm_review=true').status_code == 409
        with application.db() as c:
            c.execute("UPDATE jobs SET status='needs_review' WHERE id=?", (job,))
        assert client.post(endpoint).status_code == 422
        # No saved conversation page.
        assert client.post(endpoint + '?confirm_review=true').status_code == 409
        # Generic /chat/ page cannot identify this job's result.
        with application.db() as c:
            c.execute("UPDATE video_web_jobs SET web_page_url='https://www.doubao.com/chat/' WHERE job_id=?", (job,))
        assert client.post(endpoint + '?confirm_review=true').status_code == 409
        # Untrusted host is rejected before any browser is launched.
        with application.db() as c:
            c.execute("UPDATE video_web_jobs SET web_page_url='https://evil.example/chat/123' WHERE job_id=?", (job,))
        assert client.post(endpoint + '?confirm_review=true').status_code == 422


def test_download_result_archives_bound_video_without_duration_gate(tmp_path, monkeypatch):
    from fastapi import FastAPI
    import web_api

    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    target_page = 'https://www.doubao.com/chat/38449850653386498'
    with application.db() as c:
        c.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                  ('acc_pull', 'pull', '', str(tmp_path / 'profile'), '2026-10-28T00:00:00+00:00'))
        c.execute("INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  ('job_pull', 'p', 'e', 's', 'prompt', 'acc_pull', 'needs_review',
                   '2026-10-28T00:00:00+00:00', '2026-10-28T00:00:00+00:00'))
        c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,web_page_url) VALUES(?,?,?,?)",
                  ('job_pull', '16:9', 4, target_page))
        c.execute("INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  ('job_busy', 'p', 'e', 'busy', 'prompt', 'acc_pull', 'opening',
                   '2026-10-28T00:00:00+00:00', '2026-10-28T00:00:00+00:00'))
        c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration) VALUES(?,?,?)",
                  ('job_busy', '16:9', 4))

    class Page:
        def __init__(self):
            self.closed = False

        async def goto(self, *_args, **_kwargs):
            return None

        async def wait_for_timeout(self, _ms):
            return None

        async def close(self):
            self.closed = True

    page = Page()

    class Ctx:
        async def new_page(self):
            return page

    async def fake_browser(_account_id):
        return Ctx()

    async def fake_read_state(_page):
        return {'failed': False, 'runningLast': False, 'awaitingConfirm': False,
                'videoUrls': [], 'videoCards': 1, 'text': ''}

    async def fake_download_result(_page, _known, target, **_kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'\x00\x00\x00\x20ftyp' + b'\x00' * 512)
        return {'video_url': 'https://v26-vdl.doubao.com/mock.mp4', 'output': str(target)}

    monkeypatch.setattr(web_api, 'read_state', fake_read_state)
    monkeypatch.setattr(web_api, 'download_result_video', fake_download_result)
    # Duration is deliberately different from the requested 4 seconds. The
    # persisted card binding is authoritative; archival must not reject it.
    monkeypatch.setattr(web_api, 'media_duration_seconds', lambda _path: 10.1)

    app2 = FastAPI()
    web_api.mount_video_api(app2, db=application.db, get_account=application.get_account,
                            get_job=application.get_job, browser=fake_browser,
                            base_dir=tmp_path, downloads_dir=tmp_path / 'videos',
                            serialize=application.job_row)
    with TestClient(app2) as client:
        r = client.post('/api/v1/videos/generations/job_pull/download-result?confirm_review=true')
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['status'] == 'downloaded'
        assert body['web']['result_source'] == 'bound_web_recall'
        assert body['video_path'] and (tmp_path / body['video_path']).is_file()
        assert page.closed


def test_send_confirmation_requires_explicit_confirmation_and_trusted_page(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    with TestClient(application.app) as client:
        acc = client.post('/api/accounts', json={'name': '补发账号'}).json()['id']
        resp = client.post('/api/v1/videos/generations',
                           json={'account_id': acc, 'prompt': '镜头', 'auto_start': False})
        job = resp.json()['id']
        endpoint = f'/api/v1/videos/generations/{job}/send-confirmation'

        with application.db() as c:
            c.execute("UPDATE jobs SET status='needs_review' WHERE id=?", (job,))
        assert client.post(endpoint).status_code == 422
        assert client.post(endpoint + '?confirm_send=true').status_code == 409  # no page
        with application.db() as c:
            c.execute("UPDATE video_web_jobs SET web_page_url='https://www.doubao.com/chat/' WHERE job_id=?", (job,))
        assert client.post(endpoint + '?confirm_send=true').status_code == 409
        with application.db() as c:
            c.execute("UPDATE video_web_jobs SET web_page_url='https://evil.example/chat/123' WHERE job_id=?", (job,))
        assert client.post(endpoint + '?confirm_send=true').status_code == 422
def test_send_confirmation_sends_and_reserves_quota(tmp_path, monkeypatch):
    from fastapi import FastAPI
    import web_api

    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    target_page = 'https://www.doubao.com/chat/38449850653386498'
    with application.db() as c:
        c.execute("INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
                  ('acc_send', 'send', '', str(tmp_path / 'profile'), '2026-10-28T00:00:00+00:00'))
        c.execute("INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  ('job_send', 'p', 'e', 's', 'prompt', 'acc_send', 'needs_review',
                   '2026-10-28T00:00:00+00:00', '2026-10-28T00:00:00+00:00'))
        c.execute("INSERT INTO video_web_jobs(job_id,ratio,duration,web_page_url,quota_state) VALUES(?,?,?,?,?)",
                  ('job_send', '16:9', 4, target_page, 'released'))

    class Page:
        def __init__(self):
            self.closed = False

        async def goto(self, *_args, **_kwargs):
            return None

        async def wait_for_timeout(self, _ms):
            return None

        async def bring_to_front(self):
            return None

        async def close(self):
            self.closed = True

    page = Page()

    class Ctx:
        async def new_page(self):
            return page

    async def fake_browser(_account_id):
        return Ctx()

    state = {'failed': False, 'runningLast': False, 'generationStarted': False,
             'awaitingConfirm': True, 'videoCards': 0, 'text': ''}

    async def fake_read_state(_page):
        return dict(state)

    send_calls = []

    async def fake_send(_page, text):
        assert text == '确认采用当前参数并开始生成。'
        send_calls.append(text)

    async def fake_wait_sent(_page, _before, _baseline, _timeout):
        return 'sent', dict(state)

    monkeypatch.setattr(web_api, 'read_state', fake_read_state)
    monkeypatch.setattr(web_api, 'send_composer_text', fake_send)
    monkeypatch.setattr(web_api, 'wait_confirm_send', fake_wait_sent)

    app2 = FastAPI()
    web_api.mount_video_api(app2, db=application.db, get_account=application.get_account,
                            get_job=application.get_job, browser=fake_browser,
                            base_dir=tmp_path, downloads_dir=tmp_path / 'videos',
                            serialize=application.job_row)
    with TestClient(app2) as client:
        preset = client.post('/api/v1/videos/confirmation-presets', json={
            'name': '当前参数', 'text': '确认采用当前参数并开始生成。',
        }).json()
        r = client.post('/api/v1/videos/generations/job_send/send-confirmation?confirm_send=true',
                        json={'preset_id': preset['id']})
        assert r.status_code == 200, r.text
        assert r.json()['how'] == 'composer:确认采用当前参数并开始生成。'
        assert r.json()['preset_id'] == preset['id']
        assert not page.closed
        duplicate = client.post(
            '/api/v1/videos/generations/job_send/send-confirmation?confirm_send=true',
            json={'preset_id': preset['id']},
        )
        assert duplicate.status_code == 409
        assert len(send_calls) == 1

    with application.db() as c:
        w = c.execute("SELECT quota_state FROM video_web_jobs WHERE job_id='job_send'").fetchone()
        assert w['quota_state'] == 'reserved'
        q = c.execute("SELECT reserved_seconds FROM account_quota WHERE account_id='acc_send'").fetchone()
        assert q['reserved_seconds'] == 4
        ev = c.execute("SELECT message FROM video_web_events WHERE job_id='job_send'").fetchall()
        assert any('人工发送确认语预设' in e['message'] for e in ev)


def test_confirmation_presets_can_be_added_and_edited(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    with TestClient(application.app) as client:
        presets = client.get('/api/v1/videos/confirmation-presets')
        assert presets.status_code == 200
        assert presets.json()[0]['text'] == web_api.CONFIRM_ANSWER

        created = client.post('/api/v1/videos/confirmation-presets', json={
            'name': '快速确认', 'text': '请按上述参数开始生成。',
        })
        assert created.status_code == 201, created.text
        preset_id = created.json()['id']

        updated = client.put(f'/api/v1/videos/confirmation-presets/{preset_id}', json={
            'name': '快速确认（新版）', 'text': '参数无误，请立即生成。',
        })
        assert updated.status_code == 200
        assert updated.json()['name'] == '快速确认（新版）'
        assert updated.json()['text'] == '参数无误，请立即生成。'

        default_text = '默认预设已修改，自动流程也应使用这句话。'
        default_updated = client.put('/api/v1/videos/confirmation-presets/preset_default', json={
            'name': '默认确认语', 'text': default_text,
        })
        assert default_updated.status_code == 200
        assert client.get('/api/v1/videos/confirm-answer').json() == {'text': default_text}


def test_reject_untrusted_redirect_before_following(tmp_path):
    from web_api import download_media
    class Redirect:
        ok=False
        status=302
        headers={'location':'http://127.0.0.1:31337/secret'}
    class Request:
        def __init__(self): self.calls=[]
        async def get(self, url, **kwargs):
            self.calls.append((url,kwargs))
            return Redirect()
    class PageFake:
        request=Request()
    page=PageFake()
    async def run():
        with pytest.raises(ReviewNeeded) as e:
            await download_media(page,'https://www.doubao.com/video.mp4',tmp_path/'video.mp4')
        assert e.value.code=='UNTRUSTED_REDIRECT'
        assert len(page.request.calls)==1
        assert page.request.calls[0][1]['max_redirects']==0
    asyncio.run(run())


def test_live_report_media_validation(tmp_path):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    from live_acceptance import check_media, media_probe
    assert check_media(b'\x00'*4 + b'ftyp' + b'\x00'*300)=='.mp4'
    with pytest.raises(ValueError): check_media(b'X'*300)
    f=tmp_path/'video.mp4'
    f.write_bytes(sample_mp4_bytes())
    probe=media_probe(f)
    assert probe['container']=='pass'
    assert probe['format']=='.mp4'


def test_v2_data_migration_does_not_overwrite(tmp_path):
    import sqlite3
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    from migrate_from_v2 import migrate
    src=tmp_path/'v2'; dst=tmp_path/'v3'
    data=src/'data'; data.mkdir(parents=True)
    acc='acc_12345'
    with sqlite3.connect(data/'manager.sqlite3') as c:
        c.execute('CREATE TABLE accounts(id TEXT PRIMARY KEY,profile_dir TEXT)')
        c.execute('INSERT INTO accounts VALUES(?,?)',(acc,str(data/'profiles'/acc)))
    original=data/'profiles'/acc
    original.mkdir(parents=True)
    (original/'test_state.txt').write_text('browser state')
    assert migrate(src,dst)==1
    assert (dst/'data'/'profiles'/acc/'test_state.txt').read_text()=='browser state'
    with sqlite3.connect(dst/'data'/'manager.sqlite3') as c:
        assert c.execute('SELECT profile_dir FROM accounts').fetchone()[0]==str(Path('data')/'profiles'/acc)
    with pytest.raises(ValueError): migrate(src,dst)


@pytest.mark.parametrize('source,expected', [('web_automated', True),('human_verified_upload', False)])
def test_live_acceptance_distinguishes_automated_from_manual(tmp_path,monkeypatch,source,expected):
    import json
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    import live_acceptance as live
    video=tmp_path/'fixture.mp4'
    video.write_bytes(sample_mp4_bytes())
    monkeypatch.setattr(live,'RESULTS',tmp_path/'reports')
    def fake_api(base,path,method='GET',body=None,key=''):
        if path=='/api/health': return {'ok':True}
        if path.endswith('/job_test'):
            return {'id':'job_test','status':'downloaded','web':{'result_source':source},'error_message':''}
        raise AssertionError(path)
    def fake_download(base,path,method='GET',body=None,content_type=None,key='',timeout=40):
        assert path=='/api/jobs/job_test/video'
        return video.read_bytes(),200
    monkeypatch.setattr(live,'api',fake_api)
    monkeypatch.setattr(live,'request',fake_download)
    assert live.main(['--job-id','job_test','--poll','0.01'])==0
    report=json.loads((tmp_path/'reports'/'job_test.json').read_text(encoding='utf-8'))
    assert report['live_website_verified'] is expected
    assert report['result_source']==source
    assert report['probe']['container']=='pass'


def test_archived_video_can_be_pushed_back_to_h3(tmp_path, monkeypatch):
    monkeypatch.setattr(application, 'DB_PATH', tmp_path / 'test.sqlite3')
    application.init_db()
    recover_interrupted_jobs(application.db)
    token = 'a' * 32
    captured = {}

    class FakeResponse:
        is_error = False

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, *, headers, data, files):
            captured.update(url=url, token=headers['X-Doubao-Push-Token'], duration=data['duration'])
            captured['video'] = files['file'][1].read()
            return FakeResponse()

    monkeypatch.setattr(web_api.httpx, 'AsyncClient', FakeClient)
    with TestClient(application.app) as client:
        account_id = client.post('/api/accounts', json={'name': 'push-test'}).json()['id']
        created = client.post('/api/v1/videos/generations', json={
            'account_id': account_id,
            'prompt': '回推测试',
            'duration': 4,
            'result_push_url': 'http://127.0.0.1:3013/api/v1/doubao-results/42',
            'result_push_token': token,
            'auto_start': False,
        })
        assert created.status_code == 202, created.text
        jobid = created.json()['id']
        video = application.BASE_DIR / '.tmp' / f'{tmp_path.name}-push-result.mp4'
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b'fake-mp4')
        relative = str(video.resolve().relative_to(application.BASE_DIR.resolve()))
        with application.db() as conn:
            conn.execute("UPDATE jobs SET video_path=?,status='downloaded' WHERE id=?", (relative, jobid))

        pushed = client.post(f'/api/v1/videos/generations/{jobid}/push-result')
        assert pushed.status_code == 200, pushed.text
        assert captured == {
            'url': 'http://127.0.0.1:3013/api/v1/doubao-results/42',
            'token': token,
            'duration': '4',
            'video': b'fake-mp4',
        }
        listed = next(item for item in client.get('/api/jobs').json() if item['id'] == jobid)
        assert listed['can_push_result'] is True
        assert listed['result_pushed_at']
        video.unlink(missing_ok=True)

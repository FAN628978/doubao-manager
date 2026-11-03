import sys
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
import web_api


def test_auto_restart_after_verification_reuses_original_job(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "verification-restart.sqlite3")
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    with application.db() as conn:
        conn.execute(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            ("account_1", "账号一", "", str(tmp_path / "p1"), "2026-01-01T00:00:00+00:00"),
        )

    class ReviewPage:
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    review_page = ReviewPage()
    calls = []

    async def fake_browser(account_id):
        assert account_id == "account_1"
        return account_id

    async def fake_perform(_ctx, req, _assets, target, _status, _page_url, _binding,
                           on_manual_ready=None, confirmation_text="", close_on_success=False,
                           on_review_page=None):
        calls.append(req.submit_mode)
        if len(calls) == 1:
            await on_review_page(review_page)
            raise web_api.ReviewNeeded("VERIFICATION_REQUIRED", "页面要求人工验证；未提交任务")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"verification-restart-video")
        return {
            "output": str(target),
            "video_url": "https://example.com/restarted.mp4",
            "web_page_url": "https://www.doubao.com/chat/restarted",
        }

    monkeypatch.setattr(web_api, "perform", fake_perform)
    bridge = FastAPI()
    web_api.mount_video_api(
        bridge, db=application.db, get_account=application.get_account,
        get_job=application.get_job, browser=fake_browser, base_dir=tmp_path,
        downloads_dir=tmp_path / "videos", serialize=application.job_row,
    )

    with TestClient(bridge) as client:
        created = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "验证后重跑", "duration": 4,
            "submit_mode": "auto", "auto_start": False,
        })
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        started = client.post(f"/api/v1/videos/generations/{job_id}/restart-auto")
        assert started.status_code == 200, started.text
        state = started.json()
        for _ in range(100):
            state = client.get(f"/api/v1/videos/generations/{job_id}").json()
            if state["status"] == "needs_review":
                break
            time.sleep(0.02)
        assert state["status"] == "needs_review"
        assert state["web"]["error_code"] == "VERIFICATION_REQUIRED"
        assert bridge.state.video_prepared_pages[job_id] is review_page

        # 重新自动执行由用户决定，不按异常类型限制。
        with application.db() as conn:
            conn.execute(
                "UPDATE video_web_jobs SET error_code='SETTING_NOT_FOUND' WHERE job_id=?",
                (job_id,),
            )

        restarted = client.post(f"/api/v1/videos/generations/{job_id}/restart-auto")
        assert restarted.status_code == 200, restarted.text
        for _ in range(100):
            state = client.get(f"/api/v1/videos/generations/{job_id}").json()
            if state["status"] == "downloaded":
                break
            time.sleep(0.02)
        assert state["id"] == job_id
        assert state["status"] == "downloaded"
        assert review_page.closed
        assert calls == ["auto", "auto"]


def test_auto_restart_rematches_to_fresh_account(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "restart-rematch.sqlite3")
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    with application.db() as conn:
        conn.executemany(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            [
                ("account_1", "first", "", str(tmp_path / "p1"), "2026-01-01T00:00:00+00:00"),
                ("account_2", "second", "", str(tmp_path / "p2"), "2026-01-02T00:00:00+00:00"),
            ],
        )

    browser_actions = []

    async def fake_browser(account_id):
        browser_actions.append(("open", account_id))
        return account_id

    async def fake_close_browser(account_id):
        browser_actions.append(("close", account_id))

    async def fake_perform(
        _ctx, _req, _assets, target, _status, _page_url, _binding,
        on_manual_ready=None, confirmation_text="", close_on_success=False,
        on_review_page=None,
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"restart-rematch-video")
        return {
            "output": str(target),
            "video_url": "https://example.com/restart-rematch.mp4",
            "web_page_url": "https://www.doubao.com/chat/restart-rematch",
        }

    monkeypatch.setattr(web_api, "perform", fake_perform)
    bridge = FastAPI()
    web_api.mount_video_api(
        bridge,
        db=application.db,
        get_account=application.get_account,
        get_job=application.get_job,
        browser=fake_browser,
        base_dir=tmp_path,
        downloads_dir=tmp_path / "videos",
        serialize=application.job_row,
        close_browser=fake_close_browser,
    )

    with TestClient(bridge) as client:
        created = client.post(
            "/api/v1/videos/generations",
            json={
                "account_id": "account_1",
                "prompt": "restart with another account",
                "duration": 4,
                "submit_mode": "auto",
                "auto_start": False,
            },
        )
        assert created.status_code == 202, created.text
        jobid = created.json()["id"]
        with application.db() as conn:
            conn.execute(
                "UPDATE jobs SET status='needs_review', error_message='page failed' "
                "WHERE id=?",
                (jobid,),
            )
            conn.execute(
                "UPDATE video_web_jobs SET error_code='GENERATION_FAILED',"
                "error_class='GENERATION_FAILED',phase='needs_review',"
                "quota_state='unknown' WHERE job_id=?",
                (jobid,),
            )

        restarted = client.post(
            f"/api/v1/videos/generations/{jobid}/restart-auto"
        )
        assert restarted.status_code == 200, restarted.text
        state = restarted.json()
        for _ in range(200):
            state = client.get(
                f"/api/v1/videos/generations/{jobid}"
            ).json()
            if state["status"] in ("downloaded", "needs_review", "failed"):
                break
            time.sleep(0.02)

        assert state["id"] == jobid
        assert state["status"] == "downloaded", state
        assert state["account_id"] == "account_2"
        assert browser_actions[:2] == [
            ("close", "account_1"),
            ("open", "account_2"),
        ]

    with application.db() as conn:
        old_quota = web_api.quota_summary(conn, "account_1")
        new_quota = web_api.quota_summary(conn, "account_2")
    assert old_quota["consumed_seconds"] == 0
    assert old_quota["reserved_seconds"] == 0
    assert new_quota["consumed_seconds"] == 4


def test_retry_clones_unfinished_job_and_selects_another_account(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "retry.sqlite3")
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    with application.db() as conn:
        conn.executemany(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            [
                ("account_1", "账号一", "", str(tmp_path / "p1"), "2026-01-01T00:00:00+00:00"),
                ("account_2", "账号二", "", str(tmp_path / "p2"), "2026-01-02T00:00:00+00:00"),
            ],
        )

    async def fake_browser(account_id):
        return account_id

    async def fake_perform(_ctx, _req, _assets, target, _status, _page_url, _binding,
                           on_manual_ready=None, confirmation_text="", close_on_success=False,
                           on_review_page=None):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"retry-video")
        return {
            "output": str(target),
            "video_url": "https://example.com/retry.mp4",
            "web_page_url": "https://www.doubao.com/chat/38449995337935618",
        }

    monkeypatch.setattr(web_api, "perform", fake_perform)
    bridge = FastAPI()
    web_api.mount_video_api(
        bridge,
        db=application.db,
        get_account=application.get_account,
        get_job=application.get_job,
        browser=fake_browser,
        base_dir=tmp_path,
        downloads_dir=tmp_path / "videos",
        serialize=application.job_row,
    )

    with TestClient(bridge) as client:
        original = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1",
            "prompt": "保留原提示词",
            "ratio": "16:9",
            "duration": 4,
            "task_name": "原任务",
            "auto_start": False,
        })
        assert original.status_code == 202, original.text
        original_id = original.json()["id"]
        with application.db() as conn:
            conn.execute("UPDATE jobs SET status='needs_review' WHERE id=?", (original_id,))
            conn.execute("UPDATE video_web_jobs SET quota_state='released' WHERE job_id=?", (original_id,))
            conn.execute("UPDATE account_quota SET reserved_seconds=0 WHERE account_id='account_1'")

        endpoint = f"/api/v1/videos/generations/{original_id}/retry"
        assert client.post(endpoint).status_code == 422
        retried = client.post(endpoint + "?confirm_retry=true")
        assert retried.status_code == 202, retried.text
        body = retried.json()
        assert body["id"] != original_id
        # 重试任务同样先进入待处理、不绑定账号；分配时会避开原任务的 account_1。
        assert body["account_id"] is None
        assert body["prompt"] == "保留原提示词"
        assert body["task_name"] == "原任务 · 重试"
        assert body["web"]["retry_of_job_id"] == original_id

        duplicate = client.post(endpoint + "?confirm_retry=true")
        assert duplicate.status_code == 409


def test_copy_recreates_initial_server_submission_as_queued_job(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "copy.sqlite3")
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    with application.db() as conn:
        conn.executemany(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            [
                ("account_1", "账号一", "", str(tmp_path / "p1"), "2026-01-01T00:00:00+00:00"),
                ("account_2", "账号二", "", str(tmp_path / "p2"), "2026-01-02T00:00:00+00:00"),
            ],
        )

    bridge = FastAPI()
    web_api.mount_video_api(
        bridge,
        db=application.db,
        get_account=application.get_account,
        get_job=application.get_job,
        browser=lambda _account_id: None,
        base_dir=tmp_path,
        downloads_dir=tmp_path / "videos",
        serialize=application.job_row,
    )

    push_url = "http://127.0.0.1:3013/api/v1/doubao-results/42"
    push_token = "copy-push-token-123456"
    captured_push = {}

    class FakeResponse:
        is_error = False

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, data, files):
            captured_push.update(
                url=url,
                token=headers["X-Doubao-Push-Token"],
                duration=data["duration"],
                video=files["file"][1].read(),
            )
            return FakeResponse()

    monkeypatch.setattr(web_api.httpx, "AsyncClient", FakeClient)
    with TestClient(bridge) as client:
        original = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1",
            "project_id": "项目一",
            "episode_id": "第一集",
            "shot_id": "镜头一",
            "task_name": "服务器任务",
            "prompt": "保留原提示词",
            "ratio": "9:16",
            "duration": 4,
            "submit_mode": "manual",
            "result_push_url": push_url,
            "result_push_token": push_token,
            "auto_start": False,
        })
        assert original.status_code == 202, original.text
        original_id = original.json()["id"]
        with application.db() as conn:
            conn.execute("UPDATE jobs SET status='failed',error_message='运行失败' WHERE id=?", (original_id,))
            conn.execute(
                "UPDATE video_web_jobs SET web_page_url='https://www.doubao.com/chat/old',"
                "video_url='https://example.com/old.mp4',error_code='OLD_ERROR' WHERE job_id=?",
                (original_id,),
            )

        endpoint = f"/api/v1/videos/generations/{original_id}/copy"
        assert client.post(endpoint).status_code == 422
        copied = client.post(endpoint + "?confirm_copy=true")
        assert copied.status_code == 202, copied.text
        body = copied.json()
        assert body["id"] != original_id
        assert body["status"] == "queued"
        # 复制出的任务进入待处理，不提前绑定账号；进入处理前才分配。
        assert body["account_id"] is None
        assert body["web"]["quota_state"] == ""
        assert body["project_id"] == "项目一"
        assert body["episode_id"] == "第一集"
        assert body["shot_id"] == "镜头一"
        assert body["task_name"] == "服务器任务"
        assert body["prompt"] == "保留原提示词"
        assert body["web"]["ratio"] == "9:16"
        assert body["web"]["submit_mode"] == "manual"
        assert body["web"]["web_page_url"] == ""
        assert body["web"]["video_url"] == ""
        assert body["web"]["error_code"] == ""

        with application.db() as conn:
            stored = conn.execute(
                "SELECT result_push_url,result_push_token FROM video_web_jobs WHERE job_id=?",
                (body["id"],),
            ).fetchone()
        assert stored["result_push_url"] == push_url
        assert stored["result_push_token"] == push_token
        assert client.get(f"/api/v1/videos/generations/{original_id}").json()["status"] == "failed"

        video_path = tmp_path / "videos" / "library" / "copied.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.write_bytes(b"copied-video")
        with application.db() as conn:
            conn.execute(
                "UPDATE jobs SET status='downloaded',video_path=? WHERE id=?",
                (str(video_path.relative_to(tmp_path)), body["id"]),
            )
        pushed = client.post(f"/api/v1/videos/generations/{body['id']}/push-result")
        assert pushed.status_code == 200, pushed.text
        assert captured_push == {
            "url": push_url,
            "token": push_token,
            "duration": "4",
            "video": b"copied-video",
        }


def test_auto_submission_defaults_to_execution_and_pushes_result(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setenv("DOUBAO_BROWSER_IDLE_CLOSE_SECONDS", "0")
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "auto-push.sqlite3")
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    with application.db() as conn:
        conn.executemany(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)", [
                ("account_1", "账号一", "", str(tmp_path / "p1"), "2026-01-01T00:00:00+00:00"),
                ("account_2", "账号二", "", str(tmp_path / "p2"), "2026-01-02T00:00:00+00:00"),
            ],
        )

    async def fake_browser(account_id):
        return account_id

    closed_accounts = []

    async def fake_close_browser(account_id):
        closed_accounts.append(account_id)

    confirmation_texts = []
    close_flags = []

    async def fake_perform(_ctx, _req, _assets, target, _status, _page_url, _binding,
                           on_manual_ready=None, confirmation_text="", close_on_success=False,
                           on_review_page=None):
        confirmation_texts.append(confirmation_text)
        close_flags.append(close_on_success)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"auto-video")
        return {
            "output": str(target),
            "video_url": "https://example.com/auto.mp4",
            "web_page_url": "https://www.doubao.com/chat/auto-task",
        }

    pushed = []

    push_should_fail = {"value": False}

    class FakeResponse:
        def __init__(self, is_error=False):
            self.is_error = is_error
            self.status_code = 503 if is_error else 200
            self.text = "temporary push failure" if is_error else ""

        def json(self):
            return {"detail": self.text}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, data, files):
            pushed.append({
                "url": url,
                "token": headers["X-Doubao-Push-Token"],
                "duration": data["duration"],
                "video": files["file"][1].read(),
            })
            return FakeResponse(push_should_fail["value"])

    monkeypatch.setattr(web_api, "perform", fake_perform)
    monkeypatch.setattr(web_api.httpx, "AsyncClient", FakeClient)
    bridge = FastAPI()
    web_api.mount_video_api(
        bridge,
        db=application.db,
        get_account=application.get_account,
        get_job=application.get_job,
        browser=fake_browser,
        base_dir=tmp_path,
        downloads_dir=tmp_path / "videos",
        serialize=application.job_row,
        close_browser=fake_close_browser,
    )

    push_url = "http://127.0.0.1:3013/api/v1/doubao-results/99"
    push_token = "auto-push-token-123456"
    with TestClient(bridge) as client:
        # 本地默认处理方式是手动；本用例验证 auto 路径，显式切回自动。
        assert client.get("/api/settings/video-submit-mode").json() == {"submit_mode": "manual"}
        assert client.put("/api/settings/video-submit-mode",
                          json={"submit_mode": "auto"}).status_code == 200
        default_confirmation = "参数无误，请按照默认预设开始生成。"
        updated_preset = client.put(
            "/api/v1/videos/confirmation-presets/preset_default",
            json={"name": "默认确认语", "text": default_confirmation},
        )
        assert updated_preset.status_code == 200, updated_preset.text
        created = client.post("/api/v1/videos/generations", json={
            "prompt": "默认自动执行并回推",
            "duration": 4,
            "result_push_url": push_url,
            "result_push_token": push_token,
        })
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        state = created.json()
        for _ in range(100):
            state = client.get(f"/api/v1/videos/generations/{job_id}").json()
            if state["status"] in ("downloaded", "needs_review", "failed"):
                break
            time.sleep(0.01)

        assert state["status"] == "downloaded", state
        assert state["account_id"] == "account_1"
        assert state["web"]["submit_mode"] == "auto"
        # 回推已改成不阻塞 worker 的后台任务：下载完成后再等回推落地。
        for _ in range(200):
            state = client.get(f"/api/v1/videos/generations/{job_id}").json()
            if state["web"]["result_pushed_at"]:
                break
            time.sleep(0.01)
        assert state["web"]["result_pushed_at"]
        assert confirmation_texts == [default_confirmation]
        assert close_flags == [False]
        for _ in range(50):
            if closed_accounts:
                break
            time.sleep(0.01)
        assert closed_accounts == ["account_1"]
        events = client.get(f"/api/v1/videos/generations/{job_id}/events").json()
        assert any("账号没有其他任务，已关闭浏览器窗口" in event["message"]
                   for event in events)
        assert pushed == [{
            "url": push_url,
            "token": push_token,
            "duration": "4",
            "video": b"auto-video",
        }]

        push_should_fail["value"] = True
        copied = client.post(
            f"/api/v1/videos/generations/{job_id}/copy?confirm_copy=true")
        assert copied.status_code == 202, copied.text
        copied_id = copied.json()["id"]
        copied_state = copied.json()
        assert copied_state["status"] == "queued", copied_state
        # 复制任务先进入待处理、不带账号；分配时才会挑一个额度足够的空闲账号。
        assert copied_state["account_id"] is None
        assert confirmation_texts == [default_confirmation]
        assert close_flags == [False]
        assert closed_accounts == ["account_1"]
        assert len(pushed) == 1

        started = client.post(f"/api/v1/videos/generations/{copied_id}/restart-auto")
        assert started.status_code == 200, started.text
        for _ in range(600):
            copied_state = client.get(f"/api/v1/videos/generations/{copied_id}").json()
            if copied_state["status"] in ("downloaded", "needs_review", "failed"):
                break
            time.sleep(0.01)

        assert copied_state["status"] == "downloaded", copied_state
        # 复制任务的账号在进入处理前才分配，且会避开原任务用过的 account_1。
        assert copied_state["account_id"] == "account_2"
        assert copied_state["video_path"]
        assert copied_state["web"]["error_code"] == ""
        assert copied_state["web"]["delivery_state"] == "failed"
        assert copied_state["web"]["delivery_attempts"] == 1
        assert copied_state["web"]["delivery_next_retry_at"]
        assert confirmation_texts == [default_confirmation, default_confirmation]
        assert close_flags == [False, False]
        assert closed_accounts == ["account_1", "account_2"]
        assert len(pushed) == 2

        push_should_fail["value"] = False
        retried_push = client.post(
            f"/api/v1/videos/generations/{copied_id}/push-result")
        assert retried_push.status_code == 200, retried_push.text
        recovered = client.get(f"/api/v1/videos/generations/{copied_id}").json()
        assert recovered["status"] == "downloaded"
        assert recovered["web"]["error_code"] == ""
        assert recovered["web"]["delivery_state"] == "delivered"
        assert recovered["web"]["delivery_attempts"] == 2
        assert recovered["web"]["delivery_next_retry_at"] == ""
        assert len(pushed) == 3

        setting = client.put(
            "/api/settings/video-submit-mode", json={"submit_mode": "manual"})
        assert setting.status_code == 200, setting.text
        manual = client.post("/api/v1/videos/generations", json={
            "prompt": "本地设置决定人工处理",
            "duration": 4,
        })
        assert manual.status_code == 202, manual.text
        assert manual.json()["status"] == "queued"
        assert manual.json()["web"]["submit_mode"] == "manual"


def test_explicit_account_quota_is_checked_on_create_and_start(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "quota-check.sqlite3")
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    with application.db() as conn:
        conn.execute(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            ("account_1", "账号一", "", str(tmp_path / "p1"), "2026-01-01T00:00:00+00:00"),
        )
        web_api.reserve_quota(conn, "account_1", 25)

    browser_calls = []

    async def fake_browser(account_id):
        browser_calls.append(account_id)
        return account_id

    bridge = FastAPI()
    web_api.mount_video_api(
        bridge, db=application.db, get_account=application.get_account,
        get_job=application.get_job, browser=fake_browser, base_dir=tmp_path,
        downloads_dir=tmp_path / "videos", serialize=application.job_row,
    )

    with TestClient(bridge) as client:
        rejected = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "额度不足", "duration": 6,
            "auto_start": False,
        })
        assert rejected.status_code == 409
        assert "需 6 秒，剩余 5 秒" in rejected.json()["detail"]

        with application.db() as conn:
            conn.execute("UPDATE account_quota SET reserved_seconds=0 WHERE account_id='account_1'")
        created = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "跨天启动前复核", "duration": 4,
            "auto_start": False,
        })
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        with application.db() as conn:
            conn.execute(
                "UPDATE video_web_jobs SET quota_day='2000-01-01' WHERE job_id=?", (job_id,)
            )
            conn.execute(
                "UPDATE account_quota SET reserved_seconds=27 WHERE account_id='account_1' AND day=?",
                (web_api.local_day(),),
            )

        start = client.post(f"/api/v1/videos/generations/{job_id}/start")
        assert start.status_code == 409
        assert "需 4 秒，剩余 3 秒" in start.json()["detail"]
        assert client.get(f"/api/v1/videos/generations/{job_id}").json()["status"] == "queued"
        assert browser_calls == []

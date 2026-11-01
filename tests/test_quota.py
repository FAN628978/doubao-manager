import sys
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
import web_api


def _prepare_account(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "quota.sqlite3")
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    with application.db() as conn:
        conn.execute(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) "
            "VALUES(?,?,?,?,?)",
            (
                "account_1",
                "quota",
                "",
                str(tmp_path / "profile"),
                "2026-01-01T00:00:00+00:00",
            ),
        )


def _mount(bridge, tmp_path, browser):
    web_api.mount_video_api(
        bridge,
        db=application.db,
        get_account=application.get_account,
        get_job=application.get_job,
        browser=browser,
        base_dir=tmp_path,
        downloads_dir=tmp_path / "videos",
        serialize=application.job_row,
    )


def _wait_for(client, jobid, *, status=None, error_code=None):
    state = None
    for _ in range(150):
        state = client.get(f"/api/v1/videos/generations/{jobid}").json()
        if status and state["status"] != status:
            time.sleep(0.02)
            continue
        if error_code and state["web"]["error_code"] != error_code:
            time.sleep(0.02)
            continue
        return state
    return state


def test_cross_day_start_moves_stale_reservation_to_today(tmp_path, monkeypatch):
    _prepare_account(tmp_path, monkeypatch)

    async def fake_browser(_account_id):
        return "browser"

    async def fake_perform(
        _ctx, _req, _assets, target, _status, _page_url, _binding, **_kwargs
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"quota-cross-day-video")
        return {
            "output": str(target),
            "video_url": "https://example.com/cross-day.mp4",
            "web_page_url": "https://www.doubao.com/chat/cross-day",
        }

    monkeypatch.setattr(web_api, "perform", fake_perform)
    bridge = FastAPI()
    _mount(bridge, tmp_path, fake_browser)
    with TestClient(bridge) as client:
        created = client.post(
            "/api/v1/videos/generations",
            json={
                "account_id": "account_1",
                "prompt": "跨天额度",
                "duration": 4,
                "auto_start": False,
            },
        )
        assert created.status_code == 202, created.text
        jobid = created.json()["id"]
        today = web_api.local_day()
        old_day = "2000-01-01"
        with application.db() as conn:
            conn.execute(
                "UPDATE video_web_jobs SET quota_day=?, quota_state='reserved' "
                "WHERE job_id=?",
                (old_day, jobid),
            )
            conn.execute(
                "UPDATE account_quota SET reserved_seconds=0 "
                "WHERE account_id='account_1' AND day=?",
                (today,),
            )
            conn.execute(
                "INSERT INTO account_quota"
                "(account_id,day,reserved_seconds,consumed_seconds) VALUES(?,?,?,?)",
                ("account_1", old_day, 4, 0),
            )

        started = client.post(f"/api/v1/videos/generations/{jobid}/start")
        assert started.status_code == 200, started.text
        state = _wait_for(client, jobid, status="downloaded")
        assert state["status"] == "downloaded"

    with application.db() as conn:
        old = conn.execute(
            "SELECT reserved_seconds,consumed_seconds FROM account_quota "
            "WHERE account_id='account_1' AND day=?",
            (old_day,),
        ).fetchone()
        today_row = web_api.quota_summary(conn, "account_1", today)
    assert old["reserved_seconds"] == 0
    assert old["consumed_seconds"] == 0
    assert today_row["reserved_seconds"] == 0
    assert today_row["consumed_seconds"] == 4


def test_resend_after_unknown_submission_charges_both_attempts(
    tmp_path, monkeypatch
):
    _prepare_account(tmp_path, monkeypatch)
    perform_calls = []

    async def fake_browser(_account_id):
        return "browser"

    async def fake_perform(*_args, **_kwargs):
        perform_calls.append(True)
        raise web_api.ReviewNeeded("UPLOAD_UNVERIFIED", "new attempt failed before submit")

    monkeypatch.setattr(web_api, "perform", fake_perform)
    bridge = FastAPI()
    _mount(bridge, tmp_path, fake_browser)
    with TestClient(bridge) as client:
        created = client.post(
            "/api/v1/videos/generations",
            json={
                "account_id": "account_1",
                "prompt": "可能已扣费",
                "duration": 4,
                "auto_start": False,
            },
        )
        assert created.status_code == 202, created.text
        jobid = created.json()["id"]
        today = web_api.local_day()
        with application.db() as conn:
            conn.execute(
                "UPDATE jobs SET status='needs_review',error_message='提交状态不确定' "
                "WHERE id=?",
                (jobid,),
            )
            conn.execute(
                "UPDATE video_web_jobs SET quota_day=?,quota_state='unknown',"
                "error_code='SUBMISSION_UNCONFIRMED' WHERE job_id=?",
                (today, jobid),
            )
            conn.execute(
                "UPDATE account_quota SET reserved_seconds=4,consumed_seconds=0 "
                "WHERE account_id='account_1' AND day=?",
                (today,),
            )

        resent = client.post(
            f"/api/v1/videos/generations/{jobid}/resend?confirm_resend=true"
        )
        assert resent.status_code == 200, resent.text
        state = _wait_for(client, jobid, status="needs_review", error_code="UPLOAD_UNVERIFIED")
        assert state["web"]["quota_state"] == "released"
        assert perform_calls == [True]

    with application.db() as conn:
        quota = web_api.quota_summary(conn, "account_1", today)
    assert quota["consumed_seconds"] == 4
    assert quota["reserved_seconds"] == 0


def test_confirmation_after_unknown_submission_counts_new_attempt(
    tmp_path, monkeypatch
):
    _prepare_account(tmp_path, monkeypatch)
    target_page = "https://www.doubao.com/chat/38449850653386498"
    send_calls = []

    class Page:
        url = target_page

        async def goto(self, *_args, **_kwargs):
            return None

        async def bring_to_front(self):
            return None

        async def wait_for_timeout(self, _milliseconds):
            return None

        async def close(self):
            return None

    page = Page()

    class Ctx:
        pages = []

        async def new_page(self):
            return page

    async def fake_browser(_account_id):
        return Ctx()

    async def fake_read_state(_page):
        return {
            "failed": False,
            "runningLast": False,
            "generationStarted": False,
            "awaitingConfirm": True,
            "videoCards": 0,
            "text": "",
            "url": target_page,
        }

    async def fake_send(_page, text):
        send_calls.append(text)

    async def fake_wait_sent(_page, before, _baseline, _timeout):
        return "sent", dict(before)

    monkeypatch.setattr(web_api, "read_state", fake_read_state)
    monkeypatch.setattr(web_api, "send_composer_text", fake_send)
    monkeypatch.setattr(web_api, "wait_confirm_send", fake_wait_sent)

    bridge = FastAPI()
    _mount(bridge, tmp_path, fake_browser)
    with TestClient(bridge) as client:
        created = client.post(
            "/api/v1/videos/generations",
            json={
                "account_id": "account_1",
                "prompt": "再次确认",
                "duration": 4,
                "submit_mode": "manual",
                "auto_start": False,
            },
        )
        assert created.status_code == 202, created.text
        jobid = created.json()["id"]
        today = web_api.local_day()
        with application.db() as conn:
            conn.execute(
                "UPDATE jobs SET status='needs_review',error_message='提交状态不确定' "
                "WHERE id=?",
                (jobid,),
            )
            conn.execute(
                "UPDATE video_web_jobs SET web_page_url=?,quota_day=?,"
                "quota_state='unknown',error_code='SUBMISSION_UNCONFIRMED' "
                "WHERE job_id=?",
                (target_page, today, jobid),
            )
            conn.execute(
                "UPDATE account_quota SET reserved_seconds=4,consumed_seconds=0 "
                "WHERE account_id='account_1' AND day=?",
                (today,),
            )

        response = client.post(
            f"/api/v1/videos/generations/{jobid}/send-confirmation"
            "?confirm_send=true"
        )
        assert response.status_code == 200, response.text
        assert len(send_calls) == 1

    with application.db() as conn:
        quota = web_api.quota_summary(conn, "account_1", today)
        state = conn.execute(
            "SELECT quota_state FROM video_web_jobs WHERE job_id=?", (jobid,)
        ).fetchone()
    assert quota["consumed_seconds"] == 4
    assert quota["reserved_seconds"] == 4
    assert state["quota_state"] == "reserved"

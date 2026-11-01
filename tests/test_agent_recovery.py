import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app as application
import web_api
from agent_recovery.client import AgentConfig, ClaudeAPIError
from agent_recovery.context import build_exception_context, error_code_from_exception
from agent_recovery.policy import RecoveryPolicy
from agent_recovery.service import ExceptionRecoveryService
from agent_recovery.tools import RecoveryBackend
from doubao_manager.web import api as api_impl


class StubRecoveryService:
    def __init__(self):
        self.calls = []

    def notify_exception(self, **kwargs):
        self.calls.append(kwargs)
        return True


class FakeClaudeClient:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    async def create_message(self, *, system, messages, tools=None):
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


class RecordingBackend:
    def __init__(self, fail_first: bool = False):
        self.calls = {
            "restart_browser": [],
            "refresh_page": [],
            "recall_existing_result": [],
            "retry_h3_push": [],
            "restart_auto": [],
            "regenerate": [],
        }
        self.fail_first = fail_first

    def _callback(self, name):
        async def run(jobid):
            self.calls[name].append(jobid)
            success = not (self.fail_first and name == "refresh_page")
            return {
                "success": success,
                "verified": success,
                "detail": f"{name} completed",
            }

        return run

    def as_backend(self):
        return RecoveryBackend(
            restart_browser=self._callback("restart_browser"),
            refresh_page=self._callback("refresh_page"),
            recall_existing_result=self._callback("recall_existing_result"),
            retry_h3_push=self._callback("retry_h3_push"),
            restart_auto=self._callback("restart_auto"),
            regenerate=self._callback("regenerate"),
        )


def _text_response(payload):
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False),
            }
        ]
    }


def _tool_response(name, tool_id="tool_1"):
    return {
        "content": [
            {
                "type": "tool_use",
                "id": tool_id,
                "name": name,
                "input": {},
            }
        ]
    }


def _service(tmp_path, client, backend=None, state=None):
    backend = backend or RecordingBackend().as_backend()
    job_state = {
        "id": "job_agent",
        "account_id": "account_1",
        "status": "needs_review",
        "phase": "needs_review",
        "error_code": "RECALL_EXCEPTION",
        "web_page_url": "https://www.doubao.com/chat/example",
        "quota_state": "reserved",
        "submit_mode": "auto",
        "video_path": "",
        "result_push_url": "",
    }
    job_state.update(state or {})
    return ExceptionRecoveryService(
        backend=backend,
        context_loader=lambda _jobid: {
            "job_state": job_state,
            "recent_events": [
                {"status": "downloading", "message": "download failed", "timestamp": "t1"},
                {"status": "needs_review", "message": "failed", "timestamp": "t2"},
            ],
        },
        log_dir=tmp_path / "agent_logs",
        client_factory=lambda _config: client,
    )


def _configure_agent(monkeypatch, mode="diagnose_only"):
    monkeypatch.setenv("DOUBAO_AGENT_ENABLED", "true")
    monkeypatch.setenv("DOUBAO_AGENT_MODE", mode)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-model")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:60007")
    monkeypatch.setenv("DOUBAO_AGENT_DEDUPE_SECONDS", "0")


def _prepare_db(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "manager.sqlite3")
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    with application.db() as conn:
        conn.execute(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            (
                "account_1",
                "test",
                "",
                str(tmp_path / "profile"),
                "2026-01-01T00:00:00+00:00",
            ),
        )


def _mount_bridge(tmp_path, recovery_service):
    bridge = FastAPI()

    async def fake_browser(_account_id):
        return SimpleNamespace(pages=[])

    web_api.mount_video_api(
        bridge,
        db=application.db,
        get_account=application.get_account,
        get_job=application.get_job,
        browser=fake_browser,
        base_dir=tmp_path,
        downloads_dir=tmp_path / "data" / "videos",
        serialize=application.job_row,
        recovery_service=recovery_service,
    )
    return bridge


def _create_job(client):
    response = client.post(
        "/api/v1/videos/generations",
        json={
            "account_id": "account_1",
            "prompt": "agent test",
            "duration": 4,
            # 本地默认已是手动，这里要的是“建单即自动执行”的 auto 任务。
            "submit_mode": "auto",
            "auto_start": True,
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    state = None
    for _ in range(100):
        state = client.get(f"/api/v1/videos/generations/{job_id}").json()
        if state["status"] in {"downloaded", "needs_review", "failed"}:
            break
        time.sleep(0.02)
    return job_id, state


def _insert_review_job(
    job_id,
    error_code,
    *,
    quota_state="reserved",
    web_page_url="https://www.doubao.com/chat/existing",
):
    with application.db() as conn:
        conn.execute(
            "INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,"
            "status,error_message,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                "",
                "",
                "",
                "prompt",
                "account_1",
                "needs_review",
                f"{error_code} failed",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO video_web_jobs(job_id,ratio,duration,web_page_url,"
            "submit_mode,quota_state,error_code) VALUES(?,?,?,?,?,?,?)",
            (
                job_id,
                "16:9",
                4,
                web_page_url,
                "auto",
                quota_state,
                error_code,
            ),
        )


def _insert_queued_job(job_id, submit_mode):
    with application.db() as conn:
        conn.execute(
            "INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,"
            "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                "",
                "",
                "",
                "prompt",
                "account_1",
                "queued",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO video_web_jobs(job_id,ratio,duration,quota_day,"
            "quota_state,submit_mode) VALUES(?,?,?,?,?,?)",
            (job_id, "16:9", 4, web_api.local_day(), "reserved", submit_mode),
        )


@pytest.mark.parametrize("error_code", ["LOGIN_REQUIRED", "VERIFICATION_REQUIRED"])
def test_policy_forces_login_and_captcha_to_manual(error_code):
    policy = RecoveryPolicy("auto_safe")
    context = {
        "error_code": error_code,
        "error_message": "page requires human action",
        "quota_state": "reserved",
        "dom_state": {},
    }
    assert policy.manual_reason(context)
    for action in (
        "restart_browser",
        "refresh_page",
        "recall_existing_result",
        "retry_h3_push",
    ):
        assert not policy.decide(action, context).allowed


@pytest.mark.parametrize(
    "error_code,message",
    [
        ("SUBMISSION_UNCONFIRMED", "点击后无法确认是否发送成功"),
        ("CONFIRM_NOT_SENT", "确认生成的消息没有发出去"),
        ("UNKNOWN", "是否已经扣费不确定"),
    ],
)
def test_uncertain_submit_confirm_or_quota_never_regenerates(error_code, message):
    """提交/确认/扣费不确定时可以继续救（比如召回），但绝不能再花一次钱。"""
    policy = RecoveryPolicy("auto_safe")
    context = {
        "error_code": error_code,
        "error_message": message,
        "quota_state": "unknown" if error_code == "UNKNOWN" else "reserved",
        "account_id": "account_1",
        "dom_state": {},
    }
    # 花钱类动作一律禁止。
    assert not policy.decide("restart_auto", context).allowed
    assert not policy.decide("regenerate", context).allowed
    # 但不需要再花额度的动作仍然允许，交给策略继续救。
    assert policy.decide("restart_browser", context).allowed
    assert policy.decide("refresh_page", context).allowed


def test_exception_context_has_upload_state_and_redacts_secrets():
    context = build_exception_context(
        job_id="job_1",
        account_id="account_1",
        error_type="ReviewNeeded",
        error_code="UPLOAD_UNVERIFIED",
        error_message=(
            "Authorization: Bearer secret-token Cookie: session=secret "
            "sk-ant-secret"
        ),
        job_state={
            "status": "needs_review",
            "phase": "needs_review",
            "web_page_url": "https://www.doubao.com/chat/1?token=secret",
        },
        recent_events=[{"status": "uploading", "message": "upload", "timestamp": "t"}],
        page_snapshot={
            "available": True,
            "url": "https://www.doubao.com/chat/1?token=secret",
            "uploads": {"first": {"status": "failed", "count": 0}},
            "network_errors": [{"status": 500, "path": "/upload"}],
        },
    )
    serialized = json.dumps(context, ensure_ascii=False)
    assert "secret-token" not in serialized
    assert "session=secret" not in serialized
    assert "sk-ant-secret" not in serialized
    assert context["page_url"] == "https://www.doubao.com/chat/1"
    assert context["dom_state"]["uploads"]["first"]["status"] == "failed"
    assert context["recent_network_errors"][0]["status"] == 500


@pytest.mark.parametrize(
    "exc,expected",
    [
        (
            HTTPException(
                status_code=409,
                detail="Target page, context or browser has been closed",
            ),
            "WEB_AUTOMATION_ERROR",
        ),
        (
            RuntimeError("page.goto: net::ERR_CONNECTION_RESET"),
            "PAGE_LOAD_FAILED",
        ),
        (
            RuntimeError("new_page: browser launch failed"),
            "BROWSER_START_FAILED",
        ),
        (
            HTTPException(status_code=409, detail="需要先在该账号浏览器登录豆包"),
            "LOGIN_REQUIRED",
        ),
    ],
)
def test_error_code_classifier_uses_http_exception_detail(exc, expected):
    assert error_code_from_exception(exc) == expected


def test_account_pending_tracks_agent_until_recovery_finishes(tmp_path, monkeypatch):
    _configure_agent(monkeypatch)
    started = None
    release = None

    class BlockingClient:
        async def create_message(self, **_kwargs):
            started.set()
            await release.wait()
            return _text_response(
                {
                    "summary": "browser issue",
                    "root_cause": "page closed",
                    "confidence": 0.8,
                    "recovery_suggestion": "refresh page",
                    "needs_human": True,
                    "manual_reason": "verify manually",
                }
            )

    service = _service(tmp_path, BlockingClient())
    monkeypatch.setattr(
        application.app.state,
        "video_recovery_service",
        service,
        raising=False,
    )

    async def run():
        nonlocal started, release
        started = asyncio.Event()
        release = asyncio.Event()
        assert service.notify_exception(
            job_id="job_pending",
            account_id="account_1",
            error_type="RuntimeError",
            error_code="WEB_AUTOMATION_ERROR",
            error_message="Target page has been closed",
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        assert service.account_pending("account_1") is True
        assert application.account_busy("account_1") is True
        release.set()
        await service.wait_for_idle()
        await asyncio.sleep(0)
        assert service.account_pending("account_1") is False
        assert application.account_busy("account_1") is False

    asyncio.run(run())


def test_manual_send_failure_persists_review_and_notifies_agent(
    tmp_path, monkeypatch
):
    _prepare_db(tmp_path, monkeypatch)
    service = StubRecoveryService()

    async def broken_read_state(_page):
        raise RuntimeError("Target page, context or browser has been closed")

    monkeypatch.setattr(web_api, "read_state", broken_read_state)
    bridge = _mount_bridge(tmp_path, service)

    class FakePage:
        def is_closed(self):
            return False

        async def bring_to_front(self):
            return None

    with TestClient(bridge, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/videos/generations",
            json={
                "account_id": "account_1",
                "prompt": "manual send",
                "duration": 4,
                "auto_start": False,
                "submit_mode": "manual",
            },
        )
        assert response.status_code == 202, response.text
        jobid = response.json()["id"]
        bridge.state.video_prepared_pages[jobid] = FakePage()
        failed = client.post(
            f"/api/v1/videos/generations/{jobid}/send?confirm_send=true"
        )
        assert failed.status_code == 500

    state = application.get_job(jobid)
    assert state["status"] == "needs_review"
    with application.db() as conn:
        web = conn.execute(
            "SELECT error_code FROM video_web_jobs WHERE job_id=?", (jobid,)
        ).fetchone()
    assert web["error_code"] == "WEB_AUTOMATION_ERROR"
    assert len(service.calls) == 1
    assert service.calls[0]["job_id"] == jobid
    assert service.calls[0]["error_code"] == "WEB_AUTOMATION_ERROR"


def test_confirmation_browser_start_failure_is_handed_to_agent(
    tmp_path, monkeypatch
):
    _prepare_db(tmp_path, monkeypatch)
    service = StubRecoveryService()

    async def broken_browser(_account_id):
        raise RuntimeError("浏览器启动失败")

    bridge = FastAPI()
    web_api.mount_video_api(
        bridge,
        db=application.db,
        get_account=application.get_account,
        get_job=application.get_job,
        browser=broken_browser,
        base_dir=tmp_path,
        downloads_dir=tmp_path / "data" / "videos",
        serialize=application.job_row,
        recovery_service=service,
    )
    with TestClient(bridge, raise_server_exceptions=False) as client:
        created = client.post(
            "/api/v1/videos/generations",
            json={
                "account_id": "account_1",
                "prompt": "confirmation",
                "duration": 4,
                "auto_start": False,
                "submit_mode": "manual",
            },
        )
        assert created.status_code == 202, created.text
        jobid = created.json()["id"]
        with application.db() as conn:
            conn.execute(
                "UPDATE jobs SET status='needs_review' WHERE id=?", (jobid,)
            )
            conn.execute(
                "UPDATE video_web_jobs SET web_page_url=? WHERE job_id=?",
                ("https://www.doubao.com/chat/123456", jobid),
            )
        preset = client.post(
            "/api/v1/videos/confirmation-presets",
            json={"name": "test", "text": "确认生成"},
        )
        assert preset.status_code == 201, preset.text
        failed = client.post(
            f"/api/v1/videos/generations/{jobid}/send-confirmation"
            "?confirm_send=true",
            json={"preset_id": preset.json()["id"]},
        )
        assert failed.status_code == 500

    state = application.get_job(jobid)
    assert state["status"] == "needs_review"
    with application.db() as conn:
        web = conn.execute(
            "SELECT error_code FROM video_web_jobs WHERE job_id=?", (jobid,)
        ).fetchone()
    assert web["error_code"] == "BROWSER_START_FAILED"
    assert len(service.calls) == 1
    assert service.calls[0]["error_code"] == "BROWSER_START_FAILED"


def test_confirmation_not_sent_persists_review_without_auto_resend(
    tmp_path, monkeypatch
):
    _prepare_db(tmp_path, monkeypatch)
    service = StubRecoveryService()

    class FakePage:
        closed = False
        url = "https://www.doubao.com/chat/123456"

        def is_closed(self):
            return self.closed

        async def goto(self, *_args, **_kwargs):
            return None

        async def bring_to_front(self):
            return None

        async def wait_for_timeout(self, *_args, **_kwargs):
            return None

        async def close(self):
            self.closed = True

        async def evaluate(self, _script, **_kwargs):
            return {
                "url": self.url,
                "title": "test",
                "last_assistant_message": "",
                "buttons": {},
                "input": {},
                "uploads": {},
                "attachment_count": 0,
                "video_card_count": 0,
                "running": False,
                "completed": False,
                "login_required": False,
                "verification_required": False,
                "account_limit": False,
                "network_errors": [],
            }

    page = FakePage()

    class FakeContext:
        pages = []

        async def new_page(self):
            return page

    async def fake_browser(_account_id):
        return FakeContext()

    async def fake_read_state(_page):
        return {
            "url": page.url,
            "text": "",
            "videoCards": 0,
            "awaitingConfirm": False,
            "running": False,
            "done": False,
            "messageCount": 0,
        }

    async def no_dialog(_page):
        return False

    async def fake_send_text(_page, _text):
        return None

    async def fake_wait_confirm(*_args, **_kwargs):
        return "not_sent", {}

    monkeypatch.setattr(web_api, "read_state", fake_read_state)
    monkeypatch.setattr(web_api, "dismiss_blocking_dialogs", no_dialog)
    monkeypatch.setattr(web_api, "send_composer_text", fake_send_text)
    monkeypatch.setattr(web_api, "wait_confirm_send", fake_wait_confirm)
    bridge = FastAPI()
    web_api.mount_video_api(
        bridge,
        db=application.db,
        get_account=application.get_account,
        get_job=application.get_job,
        browser=fake_browser,
        base_dir=tmp_path,
        downloads_dir=tmp_path / "data" / "videos",
        serialize=application.job_row,
        recovery_service=service,
    )
    with TestClient(bridge, raise_server_exceptions=False) as client:
        created = client.post(
            "/api/v1/videos/generations",
            json={
                "account_id": "account_1",
                "prompt": "confirmation",
                "duration": 4,
                "auto_start": False,
                "submit_mode": "manual",
            },
        )
        assert created.status_code == 202, created.text
        jobid = created.json()["id"]
        with application.db() as conn:
            conn.execute(
                "UPDATE jobs SET status='needs_review' WHERE id=?", (jobid,)
            )
            conn.execute(
                "UPDATE video_web_jobs SET web_page_url=? WHERE job_id=?",
                (page.url, jobid),
            )
        preset = client.post(
            "/api/v1/videos/confirmation-presets",
            json={"name": "test", "text": "确认生成"},
        )
        failed = client.post(
            f"/api/v1/videos/generations/{jobid}/send-confirmation"
            "?confirm_send=true",
            json={"preset_id": preset.json()["id"]},
        )
        assert failed.status_code == 409

    assert page.closed is True
    state = application.get_job(jobid)
    assert state["status"] == "needs_review"
    with application.db() as conn:
        web = conn.execute(
            "SELECT error_code FROM video_web_jobs WHERE job_id=?", (jobid,)
        ).fetchone()
    assert web["error_code"] == "CONFIRM_NOT_SENT"
    assert len(service.calls) == 1
    assert service.calls[0]["error_code"] == "CONFIRM_NOT_SENT"


def test_startup_resume_only_hands_off_safe_existing_failures(
    tmp_path, monkeypatch
):
    _prepare_db(tmp_path, monkeypatch)
    _insert_review_job("job_recall", "RECALL_EXCEPTION")
    _insert_review_job(
        "job_confirm",
        "CONFIRM_NOT_SENT",
        quota_state="released",
    )
    _insert_review_job(
        "job_unknown",
        "WAIT_TIMEOUT",
        quota_state="unknown",
    )
    service = StubRecoveryService()
    bridge = _mount_bridge(tmp_path, service)

    scheduled = asyncio.run(bridge.state.video_resume_recovery())

    assert scheduled == 2
    assert sorted(call["job_id"] for call in service.calls) == ["job_confirm", "job_recall"]
    # 扣费状态不确定的任务仍然只诊断、不交给自动恢复。
    assert "job_unknown" not in [call["job_id"] for call in service.calls]


def test_startup_resume_restarts_persisted_automatic_jobs_only(
    tmp_path, monkeypatch
):
    _prepare_db(tmp_path, monkeypatch)
    with application.db() as conn:
        conn.execute("UPDATE accounts SET is_member=1 WHERE id='account_1'")
    _insert_queued_job("job_auto_resume", "auto")
    _insert_queued_job("job_manual_waiting", "manual")
    blocker = None

    async def fake_perform(*_args, **_kwargs):
        await blocker.wait()

    async def fake_browser(_account_id):
        return SimpleNamespace(pages=[])

    monkeypatch.setattr(web_api, "perform", fake_perform)
    bridge = FastAPI()
    web_api.mount_video_api(
        bridge,
        db=application.db,
        get_account=application.get_account,
        get_job=application.get_job,
        browser=fake_browser,
        base_dir=tmp_path,
        downloads_dir=tmp_path / "data" / "videos",
        serialize=application.job_row,
    )

    async def run():
        nonlocal blocker
        blocker = asyncio.Event()
        assert bridge.state.video_resume_queue() == 1
        assert "job_auto_resume" in bridge.state.video_pending
        assert "job_manual_waiting" not in bridge.state.video_pending
        with application.db() as conn:
            model = conn.execute(
                "SELECT model FROM video_web_jobs WHERE job_id='job_auto_resume'"
            ).fetchone()["model"]
        assert model == "Seedance 2.0"
        tasks = list(bridge.state.video_pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_refresh_recovery_never_guesses_last_browser_tab(tmp_path, monkeypatch):
    _prepare_db(tmp_path, monkeypatch)
    _insert_review_job(
        "job_refresh",
        "WEB_AUTOMATION_ERROR",
        web_page_url="https://www.doubao.com/chat/expected",
    )

    class OtherPage:
        url = "https://www.doubao.com/chat/other"
        reload_calls = 0

        def is_closed(self):
            return False

        async def reload(self, **_kwargs):
            self.reload_calls += 1

    page = OtherPage()

    class FakeContext:
        pages = [page]

    async def fake_browser(_account_id):
        return FakeContext()

    bridge = FastAPI()
    web_api.mount_video_api(
        bridge,
        db=application.db,
        get_account=application.get_account,
        get_job=application.get_job,
        browser=fake_browser,
        base_dir=tmp_path,
        downloads_dir=tmp_path / "data" / "videos",
        serialize=application.job_row,
    )
    callback = bridge.state.video_recovery_service.backend.refresh_page
    result = asyncio.run(callback("job_refresh"))

    assert result["success"] is False
    assert result["verified"] is False
    assert page.reload_calls == 0


def test_disabled_agent_does_not_call_claude_or_write_log(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_AGENT_ENABLED", "false")
    client = FakeClaudeClient(error=AssertionError("must not be called"))
    service = _service(tmp_path, client)
    assert not service.notify_exception(
        job_id="job_disabled",
        error_type="ReviewNeeded",
        error_code="UPLOAD_UNVERIFIED",
        error_message="upload failed",
    )
    asyncio.run(service.wait_for_idle())
    assert client.calls == []
    assert not service.log_path.exists()


def test_agent_config_reads_local_claude_settings(tmp_path, monkeypatch):
    for name in (
        "DOUBAO_AGENT_ENABLED",
        "DOUBAO_AGENT_MODE",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    settings_path = tmp_path / "settings.local.json"
    settings_path.write_text(
        json.dumps(
            {
                "env": {
                    "DOUBAO_AGENT_ENABLED": "true",
                    "DOUBAO_AGENT_MODE": "auto_safe",
                    "ANTHROPIC_API_KEY": "file-key",
                    "ANTHROPIC_MODEL": "file-model",
                    "ANTHROPIC_BASE_URL": "http://localhost:60007",
                }
            }
        ),
        encoding="utf-8",
    )

    config = AgentConfig.from_env(settings_path)
    assert config.enabled is True
    assert config.mode == "auto_safe"
    assert config.api_key == "file-key"
    assert config.model == "file-model"
    assert config.base_url == "http://localhost:60007"

    monkeypatch.setenv("ANTHROPIC_MODEL", "env-model")
    assert AgentConfig.from_env(settings_path).model == "env-model"


def test_agent_default_settings_path_targets_project_root():
    from agent_recovery import client

    expected = (
        Path(__file__).resolve().parents[1]
        / ".claude"
        / "settings.local.json"
    )
    assert client.LOCAL_SETTINGS_PATH == expected


def test_agent_timeout_is_logged_without_changing_business_state(
    tmp_path, monkeypatch
):
    _configure_agent(monkeypatch)
    client = FakeClaudeClient(error=ClaudeAPIError("Claude API 请求超时"))
    service = _service(tmp_path, client)
    async def run():
        assert service.notify_exception(
            job_id="job_timeout",
            error_type="ReviewNeeded",
            error_code="UPLOAD_UNVERIFIED",
            error_message="upload failed",
        )
        await service.wait_for_idle()

    asyncio.run(run())
    record = json.loads(service.log_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["job_id"] == "job_timeout"
    assert "超时" in record["agent_error"]
    assert record["needs_human"] is True


def test_agent_executes_only_the_policy_approved_existing_recall(tmp_path, monkeypatch):
    _configure_agent(monkeypatch, mode="auto_safe")
    backend = RecordingBackend()
    client = FakeClaudeClient(
        responses=[
            _tool_response("recall_existing_result"),
            _text_response(
                {
                    "summary": "download worker failed",
                    "root_cause": "original result was not archived",
                    "confidence": 0.9,
                    "recovery_suggestion": "use existing recall",
                    "needs_human": False,
                    "manual_reason": "",
                }
            ),
        ]
    )
    service = _service(tmp_path, client, backend=backend.as_backend())
    async def run():
        assert service.notify_exception(
            job_id="job_recall",
            error_type="ReviewNeeded",
            error_code="RECALL_EXCEPTION",
            error_message="download failed",
        )
        await service.wait_for_idle()

    asyncio.run(run())
    assert backend.calls["recall_existing_result"] == ["job_recall"]
    assert not backend.calls["restart_browser"]
    record = json.loads(service.log_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["recovery_success"] is True
    assert record["actions"][0]["name"] == "recall_existing_result"


def test_h3_retry_tool_only_calls_h3_delivery_backend(tmp_path, monkeypatch):
    _configure_agent(monkeypatch, mode="auto_safe")
    backend = RecordingBackend()
    client = FakeClaudeClient(
        responses=[
            _tool_response("retry_h3_push"),
            _text_response(
                {
                    "summary": "H3 delivery failed",
                    "root_cause": "temporary callback failure",
                    "confidence": 0.8,
                    "recovery_suggestion": "retry the existing outbox",
                    "needs_human": False,
                    "manual_reason": "",
                }
            ),
        ]
    )
    service = _service(
        tmp_path,
        client,
        backend=backend.as_backend(),
        state={
            "error_code": "RESULT_PUSH_FAILED",
            "video_path": "data/videos/job_h3.mp4",
            "result_push_url": "http://127.0.0.1:3013/push",
        },
    )
    async def run():
        assert service.notify_exception(
            job_id="job_h3",
            error_type="ConnectError",
            error_code="RESULT_PUSH_FAILED",
            error_message="H3 connection failed",
        )
        await service.wait_for_idle()

    asyncio.run(run())
    assert backend.calls["retry_h3_push"] == ["job_h3"]
    assert not backend.calls["recall_existing_result"]


def test_normal_worker_success_does_not_notify_agent(tmp_path, monkeypatch):
    _prepare_db(tmp_path, monkeypatch)
    service = StubRecoveryService()

    async def fake_perform(_ctx, _req, _assets, target, on_status, on_page, _binding, **_):
        await on_status("configuring", "configured")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"normal-video")
        return {
            "video_url": "https://example.com/video.mp4",
            "web_page_url": "https://www.doubao.com/chat/normal",
            "output": str(target),
        }

    monkeypatch.setattr(web_api, "perform", fake_perform)
    bridge = _mount_bridge(tmp_path, service)
    with TestClient(bridge) as client:
        _, state = _create_job(client)
        assert state["status"] == "downloaded"
        assert service.calls == []


def test_worker_exception_notifies_agent_after_original_state_update(
    tmp_path, monkeypatch
):
    _prepare_db(tmp_path, monkeypatch)
    service = StubRecoveryService()

    async def fake_perform(*_args, **_kwargs):
        raise web_api.ReviewNeeded("UPLOAD_UNVERIFIED", "original upload error")

    monkeypatch.setattr(web_api, "perform", fake_perform)
    bridge = _mount_bridge(tmp_path, service)
    with TestClient(bridge) as client:
        jobid, state = _create_job(client)
        assert state["status"] == "needs_review"
        assert state["web"]["error_code"] == "UPLOAD_UNVERIFIED"
        assert len(service.calls) == 1
        assert service.calls[0]["job_id"] == jobid
        assert service.calls[0]["error_code"] == "UPLOAD_UNVERIFIED"


def test_claude_timeout_cannot_overwrite_original_exception(tmp_path, monkeypatch):
    _prepare_db(tmp_path, monkeypatch)
    _configure_agent(monkeypatch)
    client = FakeClaudeClient(error=ClaudeAPIError("Claude API 请求超时"))
    backend = RecordingBackend().as_backend()
    service = ExceptionRecoveryService(
        backend=backend,
        context_loader=lambda jobid: {
            "job_state": dict(application.get_job(jobid)),
            "recent_events": [],
        },
        log_dir=tmp_path / "agent_logs",
        client_factory=lambda _config: client,
    )

    async def fake_perform(*_args, **_kwargs):
        raise web_api.ReviewNeeded("UPLOAD_UNVERIFIED", "original upload error")

    monkeypatch.setattr(web_api, "perform", fake_perform)
    bridge = _mount_bridge(tmp_path, service)
    with TestClient(bridge) as client_api:
        jobid, state = _create_job(client_api)
        assert state["status"] == "needs_review"
        for _ in range(100):
            if service.log_path.exists():
                break
            time.sleep(0.02)
        assert service.log_path.exists()
        row = application.get_job(jobid)
        assert row["status"] == "needs_review"
        assert row["error_message"] == "original upload error"
        with application.db() as conn:
            code = conn.execute(
                "SELECT error_code FROM video_web_jobs WHERE job_id=?", (jobid,)
            ).fetchone()["error_code"]
        assert code == "UPLOAD_UNVERIFIED"


def test_h3_push_failure_triggers_agent_without_changing_downloaded_job(
    tmp_path, monkeypatch
):
    _prepare_db(tmp_path, monkeypatch)
    video = tmp_path / "data" / "videos" / "job_h3.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"archived-video")
    with application.db() as conn:
        conn.execute(
            "INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,"
            "video_path,error_message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "job_h3",
                "",
                "",
                "",
                "prompt",
                "account_1",
                "downloaded",
                "data/videos/job_h3.mp4",
                "",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO video_web_jobs(job_id,ratio,duration,result_push_url,"
            "result_push_token,submit_mode,delivery_state) VALUES(?,?,?,?,?,?,?)",
            (
                "job_h3",
                "16:9",
                4,
                "http://127.0.0.1:9/push",
                "push-token-1234567890",
                "auto",
                "pending",
            ),
        )

    service = StubRecoveryService()
    calls = []

    async def fake_perform(*_args, **_kwargs):
        calls.append("perform")
        raise AssertionError("H3 retry must not call generation")

    monkeypatch.setattr(web_api, "perform", fake_perform)

    class FailingClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            request = httpx.Request("POST", url)
            raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(api_impl.httpx, "AsyncClient", FailingClient)
    bridge = _mount_bridge(tmp_path, service)
    with TestClient(bridge) as client:
        response = client.post("/api/v1/videos/generations/job_h3/push-result")
        assert response.status_code == 502

    assert calls == []
    assert len(service.calls) == 1
    assert service.calls[0]["error_code"] == "RESULT_PUSH_FAILED"
    assert application.get_job("job_h3")["status"] == "downloaded"
    assert video.read_bytes() == b"archived-video"


def test_agent_recovers_download_failure_using_existing_recall(
    tmp_path, monkeypatch
):
    _prepare_db(tmp_path, monkeypatch)
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    with application.db() as conn:
        conn.execute(
            "INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,"
            "error_message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "job_recall",
                "",
                "",
                "",
                "prompt",
                "account_1",
                "needs_review",
                "download failed",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO video_web_jobs(job_id,ratio,duration,web_page_url,"
            "result_card_index,submit_mode,quota_state) VALUES(?,?,?,?,?,?,?)",
            (
                "job_recall",
                "16:9",
                4,
                "http://localhost:6000/chat/existing",
                0,
                "auto",
                "reserved",
            ),
        )

    class FakePage:
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

        async def goto(self, *_args, **_kwargs):
            return None

        async def wait_for_timeout(self, *_args, **_kwargs):
            return None

        async def bring_to_front(self):
            return None

        async def close(self):
            self.closed = True

    page = FakePage()

    class FakeContext:
        pages = []

        async def new_page(self):
            return page

    async def fake_browser(_account_id):
        return FakeContext()

    async def fake_read_state(_page):
        return {
            "url": "http://localhost:6000/chat/existing",
            "text": "",
            "failed": False,
            "awaitingConfirm": False,
            "videoCards": 1,
            "runningLast": False,
            "videoUrls": ["http://localhost:6000/existing.mp4"],
            "running": False,
            "done": True,
            "messageCount": 1,
        }

    async def fake_download(_page, _urls, target, **_kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"existing-video")
        return {
            "video_url": "http://localhost:6000/existing.mp4",
            "output": str(target),
        }

    monkeypatch.setattr(web_api, "read_state", fake_read_state)
    monkeypatch.setattr(web_api, "download_result_video", fake_download)
    bridge = FastAPI()
    web_api.mount_video_api(
        bridge,
        db=application.db,
        get_account=application.get_account,
        get_job=application.get_job,
        browser=fake_browser,
        base_dir=tmp_path,
        downloads_dir=tmp_path / "data" / "videos",
        serialize=application.job_row,
    )
    service = bridge.state.video_recovery_service
    _configure_agent(monkeypatch, mode="auto_safe")
    service.client_factory = lambda _config: FakeClaudeClient(
        responses=[
            _tool_response("recall_existing_result"),
            _text_response(
                {
                    "summary": "原任务下载失败，已有会话和结果卡片",
                    "root_cause": "下载 worker 异常",
                    "confidence": 0.9,
                    "recovery_suggestion": "调用现有召回流程重新下载",
                    "needs_human": False,
                    "manual_reason": "",
                }
            ),
        ]
    )

    async def run_agent():
        assert service.notify_exception(
            job_id="job_recall",
            error_type="ReviewNeeded",
            error_code="RECALL_EXCEPTION",
            error_message="download failed",
        )
        await service.wait_for_idle()

    asyncio.run(run_agent())
    assert application.get_job("job_recall")["status"] == "downloaded"
    state = application.get_job("job_recall")
    assert Path(tmp_path / state["video_path"]).read_bytes() == b"existing-video"
    record = json.loads(service.log_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["actions"][0]["name"] == "recall_existing_result"
    assert record["recovery_success"] is True
    assert record["needs_human"] is False


# ---- 放开权限后的行为 ----

def _agent_for(context, responses, backend=None, mode="auto_safe"):
    from agent_recovery.agent import RecoveryAgent
    from agent_recovery.tools import RecoveryToolExecutor

    policy = RecoveryPolicy(mode)
    recorder = backend or RecordingBackend()
    executor = RecoveryToolExecutor(recorder.as_backend(), policy)
    client = FakeClaudeClient(responses)
    return RecoveryAgent(client, executor, policy), recorder, client


def _tool_use(name, tool_id="t1"):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def _text_block(text='{"summary":"ok","needs_human":false}'):
    return {"type": "text", "text": text}


def _pre_submit_context(**overrides):
    context = {
        "job_id": "job_1",
        "account_id": "account_1",
        "error_code": "WEB_AUTOMATION_ERROR",
        "error_message": "page.goto net::ERR_CONNECTION_RESET",
        "quota_state": "reserved",
        "last_operational_stage": "configuring",
        "has_archived_video": False,
        "has_result_push_target": False,
        "dom_state": {},
    }
    context.update(overrides)
    return context


def test_agent_can_chain_multiple_recovery_actions():
    """放开后：一个动作没救回来，代理可以继续尝试下一个。"""
    context = _pre_submit_context()
    responses = [
        {"content": [_tool_use("refresh_page", "t1")]},
        {"content": [_tool_use("restart_browser", "t2")]},
        {"content": [_text_block()]},
    ]
    agent, recorder, client = _agent_for(context, responses)

    result = asyncio.run(agent.diagnose(context, mode="auto_safe"))

    assert recorder.calls["refresh_page"] == ["job_1"]
    assert recorder.calls["restart_browser"] == ["job_1"]
    assert result["recovery_success"] is True
    assert len(result["actions"]) == 2
    # 成功后不再把工具传回去，避免代理继续折腾。
    assert client.calls[-1]["tools"] is None


def test_agent_stops_after_max_actions_and_never_repeats_one():
    context = _pre_submit_context()
    responses = [
        {"content": [_tool_use("refresh_page", "t1")]},
        {"content": [_tool_use("refresh_page", "t2")]},
        {"content": [_tool_use("restart_browser", "t3")]},
        {"content": [_tool_use("recall_existing_result", "t4")]},
        {"content": [_tool_use("restart_auto", "t5")]},
        {"content": [_tool_use("regenerate", "t6")]},
        {"content": [_text_block()]},
    ]
    recorder = RecordingBackend()
    recorder.fail_first = True  # 让 refresh_page 失败，逼迫继续尝试
    agent, recorder, _ = _agent_for(context, responses, backend=recorder)

    result = asyncio.run(agent.diagnose(context, mode="auto_safe"))

    # 同一个动作只执行一次。
    assert len(recorder.calls["refresh_page"]) == 1
    executed = [item["name"] for item in result["actions"]]
    assert executed.count("refresh_page") == 1
    # 总执行次数受上限约束。
    assert len(executed) <= RecoveryPolicy("auto_safe").max_actions()


def test_regenerate_allowed_before_spend_but_blocked_after_spend():
    policy = RecoveryPolicy("auto_safe")
    before = _pre_submit_context()
    assert policy.decide("regenerate", before).allowed
    assert policy.decide("restart_auto", before).allowed

    after = _pre_submit_context(last_operational_stage="generating")
    assert not policy.decide("regenerate", after).allowed
    assert not policy.decide("restart_auto", after).allowed
    assert "消耗额度" in policy.decide("regenerate", after).reason

    unknown = _pre_submit_context(quota_state="unknown")
    assert not policy.decide("regenerate", unknown).allowed


def test_login_verification_and_account_limit_still_require_human():
    policy = RecoveryPolicy("auto_safe")
    for code in ("LOGIN_REQUIRED", "VERIFICATION_REQUIRED", "ACCOUNT_LIMIT"):
        context = _pre_submit_context(error_code=code)
        assert policy.manual_reason(context)
        for action in ("restart_browser", "refresh_page", "regenerate"):
            assert not policy.decide(action, context).allowed


def test_regenerate_blocked_when_video_already_archived():
    policy = RecoveryPolicy("auto_safe")
    context = _pre_submit_context(has_archived_video=True)
    assert not policy.decide("regenerate", context).allowed


def test_tool_definitions_expose_all_actions():
    from agent_recovery.tools import TOOL_DEFINITIONS

    names = {item["name"] for item in TOOL_DEFINITIONS}
    assert names == {
        "restart_browser",
        "refresh_page",
        "recall_existing_result",
        "retry_h3_push",
        "restart_auto",
        "regenerate",
    }


def test_diagnose_only_mode_never_executes_actions():
    context = _pre_submit_context()
    responses = [{"content": [_text_block('{"summary":"look","needs_human":true}')]}]
    agent, recorder, client = _agent_for(context, responses, mode="diagnose_only")

    asyncio.run(agent.diagnose(context, mode="diagnose_only"))

    assert all(not calls for calls in recorder.calls.values())
    assert client.calls[0]["tools"] is None


# ---- 诊断为空（thinking 吃掉 token 预算）的回归 ----

def test_max_tokens_default_and_floor_leave_room_for_thinking():
    """推理模型先输出 thinking，预算太小会让正文为空，诊断变成“未解析”。"""
    from agent_recovery.client import (
        DEFAULT_MAX_TOKENS,
        MIN_MAX_TOKENS,
        AgentConfig,
    )

    assert DEFAULT_MAX_TOKENS >= 4096
    assert MIN_MAX_TOKENS >= 2048
    # 配置里写了很小值也要被抬到下限。
    config = AgentConfig(
        enabled=True, mode="auto_safe", api_key="k", model="m",
        max_tokens=max(MIN_MAX_TOKENS, 1),
    )
    assert config.max_tokens >= MIN_MAX_TOKENS


def test_budget_exhausted_detection():
    from agent_recovery.client import ClaudeClient

    thinking_only = {"stop_reason": "max_tokens",
                     "content": [{"type": "thinking", "thinking": "..."}]}
    assert ClaudeClient._budget_exhausted(thinking_only) is True

    with_text = {"stop_reason": "max_tokens",
                 "content": [{"type": "thinking", "thinking": "..."},
                             {"type": "text", "text": '{"summary":"x"}'}]}
    assert ClaudeClient._budget_exhausted(with_text) is False

    normal = {"stop_reason": "end_turn",
              "content": [{"type": "text", "text": "{}"}]}
    assert ClaudeClient._budget_exhausted(normal) is False


def test_client_retries_with_larger_budget_when_thinking_exhausts_tokens():
    """第一次预算被思考耗尽时，客户端自动加倍预算重试并拿到正文。"""
    import asyncio as _asyncio

    from agent_recovery.client import AgentConfig, ClaudeClient

    config = AgentConfig(enabled=True, mode="auto_safe", api_key="k",
                         model="m", max_tokens=2048)
    client = ClaudeClient(config)
    seen: list[int] = []

    async def fake_post(headers, timeout, payload):
        seen.append(payload["max_tokens"])
        if len(seen) == 1:
            return {"stop_reason": "max_tokens",
                    "content": [{"type": "thinking", "thinking": "long..."}]}
        return {"stop_reason": "end_turn",
                "content": [{"type": "text", "text": '{"summary":"ok"}'}]}

    client._post = fake_post
    body = _asyncio.run(client.create_message(system="s", messages=[]))

    assert seen == [2048, 4096], seen
    assert ClaudeClient._content_text(body) == '{"summary":"ok"}'


def test_thinking_only_response_no_longer_becomes_unparsable():
    """即使模型只回 thinking，最终应由重试拿到可解析诊断。"""
    import asyncio as _asyncio

    from agent_recovery.agent import RecoveryAgent
    from agent_recovery.client import AgentConfig, ClaudeClient
    from agent_recovery.policy import RecoveryPolicy
    from agent_recovery.tools import RecoveryToolExecutor
    from doubao_manager.web import api as api_impl  # noqa: F401

    config = AgentConfig(enabled=True, mode="auto_safe", api_key="k",
                         model="m", max_tokens=2048)
    client = ClaudeClient(config)
    calls = {"n": 0}

    async def fake_post(headers, timeout, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"stop_reason": "max_tokens",
                    "content": [{"type": "thinking", "thinking": "..."}]}
        return {"stop_reason": "end_turn",
                "content": [{"type": "text",
                             "text": '{"summary":"页面异常","root_cause":"网络","confidence":0.8,"recovery_suggestion":"刷新","needs_human":false}'}]}

    client._post = fake_post
    policy = RecoveryPolicy("auto_safe")
    agent = RecoveryAgent(client, RecoveryToolExecutor(RecordingBackend().as_backend(), policy), policy)
    context = _pre_submit_context()

    result = _asyncio.run(agent.diagnose(context, mode="auto_safe"))

    assert result["diagnosis"]["summary"] == "页面异常"
    assert result["diagnosis"]["summary"] != "Claude 未返回可解析诊断"

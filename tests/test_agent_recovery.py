import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app as application
import web_api
from agent_recovery.cli import CLIProcessResult
from agent_recovery.config import AgentConfig
from agent_recovery.context import build_exception_context, error_code_from_exception
from agent_recovery.policy import RecoveryPolicy
from agent_recovery.service import ExceptionRecoveryService
from agent_recovery.tools import (
    TOOL_DEFINITIONS,
    RecoveryBackend,
    RecoveryToolExecutor,
)
from doubao_manager.web import api as api_impl


class StubRecoveryService:
    def __init__(self):
        self.calls = []

    def notify_exception(self, **kwargs):
        self.calls.append(kwargs)
        return True


class RecordingBackend:
    def __init__(self, fail_first: bool = False):
        self.calls = {
            "restart_browser": [],
            "switch_page": [],
            "refresh_page": [],
            "inspect_dom": [],
            "dismiss_popup": [],
            "locate_task_page": [],
            "verify_task_status": [],
            "check_assets": [],
            "recall_existing_result": [],
            "download_video": [],
            "retry_h3_push": [],
            "restart_auto": [],
            "regenerate": [],
            "release_account": [],
        }
        self.arguments = {name: [] for name in self.calls}
        self.fail_first = fail_first
        self.results = {}

    def _callback(self, name):
        async def run(jobid, arguments=None):
            self.calls[name].append(jobid)
            self.arguments[name].append(dict(arguments or {}))
            if name in self.results:
                result = self.results[name]
                return result(jobid, arguments) if callable(result) else dict(result)
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
            switch_page=self._callback("switch_page"),
            refresh_page=self._callback("refresh_page"),
            inspect_dom=self._callback("inspect_dom"),
            dismiss_popup=self._callback("dismiss_popup"),
            locate_task_page=self._callback("locate_task_page"),
            verify_task_status=self._callback("verify_task_status"),
            check_assets=self._callback("check_assets"),
            recall_existing_result=self._callback("recall_existing_result"),
            download_video=self._callback("download_video"),
            retry_h3_push=self._callback("retry_h3_push"),
            restart_auto=self._callback("restart_auto"),
            regenerate=self._callback("regenerate"),
            release_account=self._callback("release_account"),
        )


def _state_for_service(**overrides):
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
    job_state.update(overrides)
    return job_state


def _service(tmp_path, runner, backend=None, state=None):
    backend = backend or RecordingBackend().as_backend()
    job_state = _state_for_service(**(state or {}))
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
        cli_runner_factory=lambda _config: runner,
    )


def _configure_cli(monkeypatch, tmp_path):
    shim = tmp_path / ("claude.cmd" if os.name == "nt" else "claude")
    shim.write_text("@echo off\n", encoding="utf-8")
    if os.name != "nt":
        shim.chmod(0o755)
    monkeypatch.setenv("DOUBAO_CLAUDE_CLI_PATH", str(shim))
    monkeypatch.setenv("DOUBAO_AGENT_DEDUPE_SECONDS", "0")
    return shim


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
    policy = RecoveryPolicy("autonomous")
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
        ("UNKNOWN", "是否已经扣费不确定"),
    ],
)
def test_uncertain_submit_confirm_or_quota_never_regenerates(error_code, message):
    """提交/确认/扣费不确定时可以继续救（比如召回），但绝不能再花一次钱。"""
    policy = RecoveryPolicy("autonomous")
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


def test_confirmed_unsubmitted_task_can_be_submitted_again():
    policy = RecoveryPolicy("autonomous", spend_budget=1)
    context = {
        "error_code": "CONFIRM_NOT_SENT",
        "error_message": "确认生成的消息没有发出去",
        "quota_state": "reserved",
        "account_id": "account_1",
        "last_operational_stage": "submitting",
        "dom_state": {},
    }
    assert policy.decide("restart_auto", context).allowed
    assert policy.decide("regenerate", context).allowed


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
    _configure_cli(monkeypatch, tmp_path)
    started = None
    release = None

    class BlockingRunner:
        async def run(self, *, job_id, token, prompt, on_started):
            on_started(501, token[:12], str(tmp_path / "agent_logs" / "cli.log"))
            started.set()
            await release.wait()
            return CLIProcessResult(
                exit_code=0,
                timed_out=False,
                diagnosis={
                    "summary": "browser issue",
                    "root_cause": "page closed",
                    "confidence": 0.8,
                    "recovery_suggestion": "refresh page",
                    "recovery_complete": False,
                    "needs_human": True,
                    "manual_reason": "verify manually",
                },
                process_id=501,
            )

    service = _service(tmp_path, BlockingRunner())
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

    class MustNotRun:
        async def run(self, **_kwargs):
            raise AssertionError("must not be called")

    service = _service(tmp_path, MustNotRun())
    assert not service.notify_exception(
        job_id="job_disabled",
        error_type="ReviewNeeded",
        error_code="UPLOAD_UNVERIFIED",
        error_message="upload failed",
    )
    asyncio.run(service.wait_for_idle())
    assert not service.log_path.exists()


def test_cli_config_reads_only_recovery_settings(tmp_path, monkeypatch):
    for name in (
        "DOUBAO_AGENT_ENABLED",
        "DOUBAO_CLAUDE_CLI_PATH",
        "DOUBAO_AGENT_SPEND_BUDGET",
        "DOUBAO_AGENT_CLI_TIMEOUT_SECONDS",
        "DOUBAO_AGENT_CLI_PERMISSION_MODE",
        "DOUBAO_AGENT_PYTHON_PATH",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    shim = tmp_path / ("claude.cmd" if os.name == "nt" else "claude")
    shim.write_text("@echo off\n", encoding="utf-8")
    settings_path = tmp_path / "settings.local.json"
    settings_path.write_text(
        json.dumps(
            {
                "env": {
                    "DOUBAO_AGENT_ENABLED": "true",
                    "DOUBAO_CLAUDE_CLI_PATH": str(shim),
                    "DOUBAO_AGENT_SPEND_BUDGET": "0",
                    "ANTHROPIC_API_KEY": "file-key",
                    "ANTHROPIC_MODEL": "deepseek-v4-flash",
                    "ANTHROPIC_BASE_URL": "http://localhost:60007",
                }
            }
        ),
        encoding="utf-8",
    )

    config = AgentConfig.from_env(settings_path)
    assert config.enabled is True
    assert config.mode == "autonomous"
    assert config.cli_path == str(shim.resolve())
    assert config.spend_budget == 0
    assert not hasattr(config, "api_key")
    assert not hasattr(config, "model")
    assert not hasattr(config, "base_url")

    monkeypatch.setenv("DOUBAO_AGENT_SPEND_BUDGET", "7")
    assert AgentConfig.from_env(settings_path).spend_budget == 7


def test_cli_default_settings_path_targets_project_root():
    from agent_recovery import config

    expected = (
        Path(__file__).resolve().parents[1]
        / ".claude"
        / "settings.local.json"
    )
    assert config.LOCAL_SETTINGS_PATH == expected


def test_cli_timeout_is_logged_without_changing_business_state(
    tmp_path, monkeypatch
):
    _configure_cli(monkeypatch, tmp_path)

    class TimeoutRunner:
        async def run(self, *, job_id, token, prompt, on_started):
            on_started(502, token[:12], str(tmp_path / "agent_logs" / "cli.log"))
            return CLIProcessResult(
                exit_code=1,
                timed_out=True,
                diagnosis={
                    "summary": "",
                    "root_cause": "",
                    "confidence": 0,
                    "recovery_suggestion": "",
                    "recovery_complete": False,
                    "needs_human": True,
                    "manual_reason": "CLI 超时",
                },
                error="Claude CLI 运行超过 1 秒，进程树已终止",
                process_id=502,
            )

    service = _service(tmp_path, TimeoutRunner())
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
    assert record["cli_timed_out"] is True
    assert record["needs_human"] is True


def test_cli_executes_only_the_policy_approved_existing_recall(tmp_path, monkeypatch):
    _configure_cli(monkeypatch, tmp_path)
    backend = RecordingBackend()
    state = _state_for_service()

    class RecallRunner:
        async def run(self, *, job_id, token, prompt, on_started):
            on_started(503, token[:12], str(tmp_path / "agent_logs" / "cli.log"))
            holder["service"]._tool_sessions[job_id].context["status"] = (
                "needs_review"
            )
            result = await holder["service"].execute_tool(
                job_id=job_id,
                token=token,
                tool="recall_existing_result",
                arguments={},
            )
            assert result["success"] is True
            state["status"] = "downloaded"
            return CLIProcessResult(
                exit_code=0,
                timed_out=False,
                diagnosis={
                    "summary": "download worker failed",
                    "root_cause": "original result was not archived",
                    "confidence": 0.9,
                    "recovery_suggestion": "use existing recall",
                    "recovery_complete": True,
                    "needs_human": False,
                    "manual_reason": "",
                },
                process_id=503,
            )

    holder = {}
    service = ExceptionRecoveryService(
        backend=backend.as_backend(),
        context_loader=lambda _jobid: {
            "job_state": state,
            "recent_events": [],
        },
        log_dir=tmp_path / "agent_logs",
        cli_runner_factory=lambda _config: RecallRunner(),
    )
    holder["service"] = service

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
    _configure_cli(monkeypatch, tmp_path)
    backend = RecordingBackend()
    state = _state_for_service(
        error_code="RESULT_PUSH_FAILED",
        video_path="data/videos/job_h3.mp4",
        result_push_url="http://127.0.0.1:3013/push",
    )

    class H3Runner:
        async def run(self, *, job_id, token, prompt, on_started):
            on_started(504, token[:12], str(tmp_path / "agent_logs" / "cli.log"))
            result = await holder["service"].execute_tool(
                job_id=job_id,
                token=token,
                tool="retry_h3_push",
                arguments={},
            )
            assert result["success"] is True
            state["status"] = "downloaded"
            return CLIProcessResult(
                exit_code=0,
                timed_out=False,
                diagnosis={
                    "summary": "H3 delivery failed",
                    "root_cause": "temporary callback failure",
                    "confidence": 0.8,
                    "recovery_suggestion": "retry the existing outbox",
                    "recovery_complete": True,
                    "needs_human": False,
                    "manual_reason": "",
                },
                process_id=504,
            )

    holder = {}
    service = ExceptionRecoveryService(
        backend=backend.as_backend(),
        context_loader=lambda _jobid: {
            "job_state": state,
            "recent_events": [],
        },
        log_dir=tmp_path / "agent_logs",
        cli_runner_factory=lambda _config: H3Runner(),
    )
    holder["service"] = service

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


def test_cli_timeout_cannot_overwrite_original_exception(tmp_path, monkeypatch):
    _prepare_db(tmp_path, monkeypatch)
    _configure_cli(monkeypatch, tmp_path)

    class TimeoutRunner:
        async def run(self, **_kwargs):
            raise RuntimeError("Claude CLI 运行超过 1 秒，进程树已终止")

    backend = RecordingBackend().as_backend()
    service = ExceptionRecoveryService(
        backend=backend,
        context_loader=lambda jobid: {
            "job_state": dict(application.get_job(jobid)),
            "recent_events": [],
        },
        log_dir=tmp_path / "agent_logs",
        cli_runner_factory=lambda _config: TimeoutRunner(),
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
    _configure_cli(monkeypatch, tmp_path)

    class RecallRunner:
        async def run(self, *, job_id, token, prompt, on_started):
            on_started(505, token[:12], str(service.log_dir / "cli.log"))
            result = await service.execute_tool(
                job_id=job_id,
                token=token,
                tool="recall_existing_result",
                arguments={},
            )
            assert result["success"] is True
            return CLIProcessResult(
                exit_code=0,
                timed_out=False,
                diagnosis={
                    "summary": "原任务下载失败，已有会话和结果卡片",
                    "root_cause": "下载 worker 异常",
                    "confidence": 0.9,
                    "recovery_suggestion": "调用现有召回流程重新下载",
                    "recovery_complete": True,
                    "needs_human": False,
                    "manual_reason": "",
                },
                process_id=505,
            )

    service.cli_runner_factory = lambda _config: RecallRunner()

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


# ---- Autonomous CLI decision boundaries ----

def _pre_submit_context(**overrides):
    context = {
        "job_id": "job_1",
        "account_id": "account_1",
        "error_code": "WEB_AUTOMATION_ERROR",
        "error_message": "page.goto net::ERR_CONNECTION_RESET",
        "quota_state": "reserved",
        "last_operational_stage": "configuring",
        "page_url": "https://www.doubao.com/chat/example",
        "has_archived_video": False,
        "has_result_push_target": False,
        "dom_state": {},
    }
    context.update(overrides)
    return context


def test_autonomous_executor_can_chain_multiple_recovery_actions():
    context = _pre_submit_context()
    recorder = RecordingBackend()
    executor = RecoveryToolExecutor(
        recorder.as_backend(), RecoveryPolicy("autonomous", spend_budget=1)
    )

    async def run():
        results = []
        for action in (
            "refresh_page",
            "restart_browser",
            "recall_existing_result",
        ):
            results.append(await executor.execute(action, context))
        return results

    results = asyncio.run(run())
    assert recorder.calls["refresh_page"] == ["job_1"]
    assert recorder.calls["restart_browser"] == ["job_1"]
    assert recorder.calls["recall_existing_result"] == ["job_1"]
    assert all(item["executed"] for item in results)


def test_autonomous_executor_allows_limited_repeats(monkeypatch):
    monkeypatch.setenv("DOUBAO_AGENT_MAX_ACTION_ATTEMPTS", "2")
    context = _pre_submit_context()
    recorder = RecordingBackend()
    recorder.results["refresh_page"] = {
        "success": True,
        "verified": True,
        "recovery_complete": False,
        "detail": "page refreshed but flow still needs a decision",
    }
    executor = RecoveryToolExecutor(
        recorder.as_backend(), RecoveryPolicy("autonomous", spend_budget=1)
    )

    async def run():
        return [
            await executor.execute("refresh_page", context)
            for _ in range(3)
        ]

    results = asyncio.run(run())
    assert len(recorder.calls["refresh_page"]) == 2
    assert [item["executed"] for item in results] == [True, True, False]
    assert results[-1]["detail"]


def test_spend_budget_limits_repeat_generation_attempts():
    context = _pre_submit_context()
    recorder = RecordingBackend()
    executor = RecoveryToolExecutor(
        recorder.as_backend(), RecoveryPolicy("autonomous", spend_budget=1)
    )

    async def run():
        first = await executor.execute("restart_auto", context)
        second = await executor.execute("regenerate", context)
        return first, second

    first, second = asyncio.run(run())
    assert first["executed"] is True
    assert second["executed"] is False
    assert second["detail"]
    assert len(recorder.calls["restart_auto"]) == 1
    assert recorder.calls["regenerate"] == []


def test_regenerate_allowed_before_spend_but_blocked_after_spend():
    policy = RecoveryPolicy("autonomous", spend_budget=1)
    before = _pre_submit_context()
    assert policy.decide("regenerate", before).allowed
    assert policy.decide("restart_auto", before).allowed

    after = _pre_submit_context(last_operational_stage="generating")
    assert not policy.decide("regenerate", after).allowed
    assert not policy.decide("restart_auto", after).allowed
    assert policy.decide("regenerate", after).reason

    unknown = _pre_submit_context(quota_state="unknown")
    assert not policy.decide("regenerate", unknown).allowed


def test_account_owned_by_another_job_blocks_browser_actions():
    policy = RecoveryPolicy("autonomous", spend_budget=1)
    context = _pre_submit_context(
        account_owned_by_job=False,
        account_owner_job_id="job_other",
    )
    assert "job_other" in policy.manual_reason(context)
    for action in ("restart_browser", "switch_page", "refresh_page"):
        assert not policy.decide(action, context).allowed


def test_login_verification_and_account_limit_still_require_human():
    policy = RecoveryPolicy("autonomous", spend_budget=1)
    for code in ("LOGIN_REQUIRED", "VERIFICATION_REQUIRED", "ACCOUNT_LIMIT"):
        context = _pre_submit_context(error_code=code)
        assert policy.manual_reason(context)
        for action in ("restart_browser", "refresh_page", "regenerate"):
            assert not policy.decide(action, context).allowed


def test_regenerate_blocked_when_video_already_archived():
    policy = RecoveryPolicy("autonomous", spend_budget=1)
    context = _pre_submit_context(has_archived_video=True)
    assert not policy.decide("regenerate", context).allowed
    assert not policy.decide("download_video", context).allowed


def test_page_actions_require_the_persisted_task_binding():
    policy = RecoveryPolicy("autonomous", spend_budget=1)
    context = _pre_submit_context(page_url="")
    assert not policy.decide("switch_page", context).allowed
    assert not policy.decide("locate_task_page", context).allowed
    assert policy.decide("switch_page", context).reason


def test_tool_definitions_expose_all_actions():
    names = {item["name"] for item in TOOL_DEFINITIONS}
    assert names == {
        "restart_browser",
        "switch_page",
        "refresh_page",
        "inspect_dom",
        "dismiss_popup",
        "locate_task_page",
        "verify_task_status",
        "check_assets",
        "recall_existing_result",
        "download_video",
        "retry_h3_push",
        "restart_auto",
        "regenerate",
        "release_account",
    }


def test_tool_arguments_cannot_carry_credentials():
    recorder = RecordingBackend()
    executor = RecoveryToolExecutor(
        recorder.as_backend(), RecoveryPolicy("autonomous", spend_budget=1)
    )
    result = asyncio.run(
        executor.execute(
            "switch_page",
            _pre_submit_context(),
            {"url": "https://www.doubao.com/chat/example", "token": "secret"},
        )
    )
    assert result["executed"] is False
    assert recorder.calls["switch_page"] == []

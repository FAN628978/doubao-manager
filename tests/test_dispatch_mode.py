"""新任务默认处理方式：手动（默认）先进待处理，点“进入调度”后转为自动、全程执行；自动则直接进调度。

只验证建单与放行语义，不启动浏览器：用假的 perform 模拟执行阶段。
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
import web_api


def _seed(tmp_path):
    with application.db() as conn:
        conn.execute(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            ("account_1", "账号一", "", str(tmp_path / "p1"), "2026-01-01T00:00:00+00:00"),
        )


class _Ctx:
    pages: list = []

    async def new_page(self):
        return None


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "dispatch.sqlite3")
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    _seed(tmp_path)

    started = []

    async def fake_perform(ctx, req, assets, target, status, page_url, binding,
                           on_manual_ready=None, confirmation_text="",
                           close_on_success=False, on_review_page=None):
        started.append(req.submit_mode)
        # 每个 TestClient 使用自己的事件循环，阻塞事件必须在当前循环里创建。
        await asyncio.Event().wait()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video")
        return {"output": str(target),
                "video_url": "https://example.com/v.mp4",
                "web_page_url": "https://www.doubao.com/chat/x"}

    async def fake_browser(account_id):
        return _Ctx()

    # facade 会在 mount 时同步 perform，所以先替换再挂载。
    monkeypatch.setattr(web_api, "perform", fake_perform)
    monkeypatch.setattr(web_api, "automatic_step_delay", lambda mode: asyncio.sleep(0))

    def session():
        """一个挂载 = 一个进程；用 with 才能拿到贯穿多次请求的持久事件循环。

        TestClient 不在上下文里时每个请求各用一个 portal，worker 任务会在请求结束
        后被取消，所以跨请求的“等名额/重启恢复”断言必须走这里。
        """
        app_ = FastAPI()
        web_api.mount_video_api(
            app_, db=application.db, get_account=application.get_account,
            get_job=application.get_job, browser=fake_browser,
            base_dir=tmp_path, downloads_dir=tmp_path / "videos",
            serialize=application.job_row,
        )
        return TestClient(app_)

    return {"client": session(), "session": session, "started": started}


def _resume_in_loop(client):
    """像 lifespan 那样在有事件循环处调用启动恢复（否则 create_task 会静默失败）。"""
    async def _call():
        return client.app.state.video_resume_queue()
    return client.portal.call(_call)


def _set_mode(client, mode):
    response = client.put("/api/settings/video-submit-mode", json={"submit_mode": mode})
    assert response.status_code == 200, response.text


def _wait_status(client, job_id, wanted, timeout=10.0):
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        state = client.get(f"/api/v1/videos/generations/{job_id}").json()
        if state["status"] in wanted:
            return state
        time.sleep(0.01)
    return state


def test_auto_mode_hands_new_job_to_scheduler_immediately(bridge):
    """自动：建单后直接进入调度，不需要人工点击。"""
    client = bridge["client"]
    _set_mode(client, "auto")

    created = client.post("/api/v1/videos/generations", json={
        "prompt": "自动任务", "duration": 4,
    })
    assert created.status_code == 202, created.text
    job = created.json()

    state = _wait_status(client, job["id"], set(web_api.ACTIVE))
    assert state["status"] in web_api.ACTIVE, state
    # 调度器已经把它交给执行，说明没有停留在待处理等人点。
    assert bridge["started"] == ["auto"]


def test_manual_mode_waits_in_pending_until_dispatched(bridge):
    """手动：建单只进待处理；点“进入调度”后转为自动并全程执行（含点击发送）。"""
    client = bridge["client"]
    _set_mode(client, "manual")

    created = client.post("/api/v1/videos/generations", json={
        "prompt": "手动任务", "duration": 4,
        # 即使调用方要求自动开始，手动模式也必须先停在待处理。
        "auto_start": True,
    })
    assert created.status_code == 202, created.text
    job = created.json()
    assert job["status"] == "queued"
    assert job["web"]["submit_mode"] == "manual"
    # 没有进入调度：执行阶段一次都没开始。
    time.sleep(0.2)
    assert bridge["started"] == []
    state = client.get(f"/api/v1/videos/generations/{job['id']}").json()
    assert state["status"] == "queued"

    # 操作者点击“进入调度”。
    dispatched = client.post(f"/api/v1/videos/generations/{job['id']}/dispatch")
    assert dispatched.status_code == 200, dispatched.text
    state = _wait_status(client, job["id"], set(web_api.ACTIVE))
    assert state["status"] in web_api.ACTIVE, state
    # 点击执行即授权自动执行：任务落库转为 auto，执行阶段也按 auto 全程跑（含发送）。
    assert state["web"]["submit_mode"] == "auto"
    assert bridge["started"] == ["auto"]


def test_dispatch_rejects_jobs_already_running(bridge):
    client = bridge["client"]
    _set_mode(client, "manual")
    job = client.post("/api/v1/videos/generations", json={
        "prompt": "重复放行", "duration": 4,
    }).json()

    assert client.post(f"/api/v1/videos/generations/{job['id']}/dispatch").status_code == 200
    _wait_status(client, job["id"], set(web_api.ACTIVE))
    again = client.post(f"/api/v1/videos/generations/{job['id']}/dispatch")
    assert again.status_code == 409


def test_setting_default_is_manual_and_persists(bridge):
    """新库默认处理方式为手动：建单只进待处理，点击执行后才进调度自动跑。"""
    client = bridge["client"]
    assert client.get("/api/settings/video-submit-mode").json()["submit_mode"] == "manual"
    _set_mode(client, "auto")
    assert client.get("/api/settings/video-submit-mode").json()["submit_mode"] == "auto"

def test_pending_job_row_offers_dispatch_button():
    """待处理任务的首按钮是“进入调度”，调用 dispatch 接口而不是直接准备页面。"""
    import re

    source = (Path(__file__).resolve().parents[1] / "src" / "static" / "app.js").read_text(encoding="utf-8")
    body = source[source.index("function fixedJobActions"):]
    body = body[:body.index("\n  function filteredJobs")]
    # 待处理走 dispatchJob，其它状态才走 prepareManualJob。
    assert "pending?`dispatchJob(" in body
    assert "▶ 进入调度" in body
    assert "async function dispatchJob" in source
    assert "/dispatch`" in source


def test_bulk_prepare_uses_dispatch_and_only_for_pending():
    """批量“进入调度”只对待处理任务生效，并调用 dispatch 接口。"""
    source = (Path(__file__).resolve().parents[1] / "src" / "static" / "app.js").read_text(encoding="utf-8")
    assert "if(action==='prepare')return web&&['queued','ready'].includes(job.status)&&!job.dispatched_at;" in source
    assert "else if(action==='prepare')await api(`/api/v1/videos/generations/${id}/dispatch`" in source


def test_successful_dispatch_moves_job_out_of_pending(bridge):
    """放行后任务不再留在待处理队列。"""
    client = bridge["client"]
    _set_mode(client, "manual")
    job = client.post("/api/v1/videos/generations", json={
        "prompt": "放行后离开待处理", "duration": 4,
    }).json()
    pending_before = client.get("/api/v1/videos/queue/status").json()["pending"]
    assert pending_before == 1

    client.post(f"/api/v1/videos/generations/{job['id']}/dispatch")
    state = _wait_status(client, job["id"], set(web_api.ACTIVE))
    assert state["status"] in web_api.ACTIVE
    pending_after = client.get("/api/v1/videos/queue/status").json()["pending"]
    assert pending_after == 0


def test_queued_auto_job_without_account_resumes_after_restart(bridge, tmp_path):
    """自动任务未指定账号时也必须恢复，账号由 worker 在拿到名额后分配。"""
    with bridge["session"]() as client:
        _set_mode(client, "auto")
        client.put("/api/settings/processing-limit", json={"processing_limit": 1})

        blocker = client.post("/api/v1/videos/generations", json={
            "prompt": "占住处理名额", "duration": 4,
        }).json()
        _wait_status(client, blocker["id"], set(web_api.ACTIVE))

        probe = client.post("/api/v1/videos/generations", json={
            "prompt": "重启后恢复", "duration": 4,
        }).json()
        state = client.get(f"/api/v1/videos/generations/{probe['id']}").json()
        assert state["status"] == "queued"
        assert state["account_id"] is None

    # 模拟真实进程重启：先恢复被中断的状态，再启动自动队列。
    web_api.recover_interrupted_jobs(application.db, base_dir=tmp_path)
    with bridge["session"]() as client:
        assert _resume_in_loop(client) >= 1
        state = _wait_status(client, probe["id"], set(web_api.ACTIVE))
        events = client.get(
            f"/api/v1/videos/generations/{probe['id']}/events"
        ).json()
        assert state["status"] in web_api.ACTIVE, (state, events)
        assert state["account_id"] == "account_1"


def test_manual_job_is_not_resumed_after_restart(bridge, tmp_path):
    """重启恢复不能替人工任务放行。"""
    with bridge["session"]() as client:
        _set_mode(client, "manual")
        job = client.post("/api/v1/videos/generations", json={
            "prompt": "等待人工放行", "duration": 4,
        }).json()
        assert job["web"]["submit_mode"] == "manual"

    web_api.recover_interrupted_jobs(application.db, base_dir=tmp_path)
    with bridge["session"]() as client:
        assert _resume_in_loop(client) == 0
        state = client.get(f"/api/v1/videos/generations/{job['id']}").json()
        assert state["status"] == "queued"
        assert state["web"]["dispatched_at"] == ""


def test_dispatched_job_reports_waiting_not_pending(bridge):
    """已放行但在等名额的任务仍为 queued，但不再计入“未放行待处理”。"""
    with bridge["session"]() as client:
        _set_mode(client, "manual")
        client.put("/api/settings/processing-limit", json={"processing_limit": 1})
        first = client.post("/api/v1/videos/generations", json={
            "prompt": "正在执行", "duration": 4,
        }).json()
        waiting = client.post("/api/v1/videos/generations", json={
            "prompt": "等待名额", "duration": 4,
        }).json()

        assert client.post(
            f"/api/v1/videos/generations/{first['id']}/dispatch"
        ).status_code == 200
        _wait_status(client, first["id"], set(web_api.ACTIVE))
        assert client.post(
            f"/api/v1/videos/generations/{waiting['id']}/dispatch"
        ).status_code == 200

        state = client.get(
            f"/api/v1/videos/generations/{waiting['id']}"
        ).json()
        assert state["status"] == "queued"
        assert state["web"]["dispatched_at"]

        queue = client.get("/api/v1/videos/queue/status").json()
        assert queue["pending"] == 1
        assert queue["pending_dispatched"] == 1
        assert queue["pending_undispatched"] == 0


def test_dispatch_button_shows_waiting_state():
    """已放行等名额时，首按钮显示排队状态并禁止重复点击。"""
    source = (
        Path(__file__).resolve().parents[1] / "src" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    body = source[source.index("function fixedJobActions"):]
    body = body[:body.index("\n  function filteredJobs")]
    assert "'⏳ 已放行 · 排队中'" in source
    assert "disabled:recovering||!canPrepare||dispatched" in body
    assert "已交给调度器，正在等处理名额或可用账号" in source


def test_reset_generation_clears_dispatched_at(bridge):
    """显式重置任务时清除“已放行”，让任务回到未放行状态。"""
    client = bridge["client"]
    _set_mode(client, "manual")
    job = client.post("/api/v1/videos/generations", json={
        "prompt": "重置放行标记", "duration": 4,
    }).json()

    with application.db() as conn:
        conn.execute(
            "UPDATE video_web_jobs SET dispatched_at=? WHERE job_id=?",
            ("2026-11-01T00:00:00+00:00", job["id"]),
        )

    reset = client.post(
        f"/api/v1/videos/generations/{job['id']}/reset?confirm_reset=true"
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["web"]["dispatched_at"] == ""

"""新任务的自动调度与自动执行设置可独立组合。

只验证建单与放行语义，不启动浏览器：用假的 perform 模拟执行阶段。
"""
import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
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
    execution_accounts = []
    failures = {"remaining": 0}

    async def fake_perform(ctx, req, assets, target, status, page_url, binding,
                           on_manual_ready=None, confirmation_text="",
                           close_on_success=False, on_review_page=None):
        started.append(req.submit_mode)
        execution_accounts.append(req.account_id)
        if failures["remaining"]:
            failures["remaining"] -= 1
            raise web_api.ReviewNeeded("WORKER_EXCEPTION", "模拟执行异常")
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

    return {
        "client": session(),
        "session": session,
        "started": started,
        "execution_accounts": execution_accounts,
        "failures": failures,
    }


@pytest.mark.parametrize("remaining", [0, 2, 30])
@pytest.mark.parametrize("legacy_override", [False, True])
def test_restart_selects_maximum_quota_without_priority_or_original_account_penalty(
    bridge, tmp_path, remaining, legacy_override
):
    with bridge["client"] as client:
        with application.db() as conn:
            conn.execute(
                "INSERT INTO accounts(id,name,note,profile_dir,created_at,priority) "
                "VALUES('account_2','second','',?,'2026-01-02T00:00:00+00:00',1000)",
                (str(tmp_path / "p2"),),
            )
            conn.executemany(
                "INSERT INTO account_quota(account_id,day,reserved_seconds,consumed_seconds) "
                "VALUES(?,?,0,?)",
                [("account_1", web_api.local_day(), 30 - remaining),
                 ("account_2", web_api.local_day(), 30)],
            )
        setting = client.put(
            "/api/settings/ignore-quota-check",
            json={"ignore_quota_check": legacy_override},
        )
        assert setting.status_code == 200, setting.text
        created = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "choose maximum remaining quota",
            "duration": 4, "auto_start": False,
        })
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        with application.db() as conn:
            conn.execute("UPDATE jobs SET status='needs_review' WHERE id=?", (job_id,))
        restarted = client.post(f"/api/v1/videos/generations/{job_id}/restart-auto")
        assert restarted.status_code == 200, restarted.text
        state = _wait_status(client, job_id, set(web_api.ACTIVE))
        assert state["account_id"] == "account_1", state
        assert state["web"]["quota_state"] == "reserved", state
        with application.db() as conn:
            quota = web_api.quota_summary(conn, "account_1")
        assert quota["consumed_seconds"] == 30 - remaining
        assert quota["reserved_seconds"] == 4


def _resume_in_loop(client):
    """像 lifespan 那样在有事件循环处调用启动恢复（否则 create_task 会静默失败）。"""
    async def _call():
        return client.app.state.video_resume_queue()
    return client.portal.call(_call)


def _set_mode(client, mode):
    response = client.put("/api/settings/video-submit-mode", json={"submit_mode": mode})
    assert response.status_code == 200, response.text


def _set_auto_dispatch(client, enabled):
    response = client.put(
        "/api/settings/auto-dispatch",
        json={"auto_dispatch_enabled": enabled},
    )
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
    """关闭自动调度时先待处理；放行后仍保留人工执行方式。"""
    client = bridge["client"]
    _set_mode(client, "manual")
    _set_auto_dispatch(client, False)

    created = client.post("/api/v1/videos/generations", json={
        "prompt": "手动任务", "duration": 4,
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
    # 调度与执行方式互不覆盖：放行后仍停在人工发送步骤。
    assert state["web"]["submit_mode"] == "manual"
    assert bridge["started"] == ["manual"]


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


def test_dispatch_next_starts_oldest_other_pending_job(bridge):
    """网页面板的“下一个任务”只放行最早待处理任务，并复用原调度流程。"""
    with bridge["session"]() as client:
        _set_mode(client, "manual")
        current = client.post("/api/v1/videos/generations", json={
            "prompt": "当前任务", "duration": 4,
        }).json()
        next_job = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "下一个任务", "duration": 4,
        }).json()

        response = client.post(
            f"/api/v1/videos/generations/{current['id']}/dispatch-next"
        )
        assert response.status_code == 200, response.text
        assert response.json()["job_id"] == next_job["id"]
        untouched = client.get(
            f"/api/v1/videos/generations/{current['id']}"
        ).json()
        started = _wait_status(client, next_job["id"], set(web_api.ACTIVE))
        assert untouched["status"] == "queued"
        assert not untouched["web"]["dispatched_at"]
        assert started["status"] in web_api.ACTIVE
        assert started["account_id"] == "account_1"
        assert started["web"]["submit_mode"] == "auto"
        assert bridge["started"] == ["auto"]
        assert untouched["web"]["submit_mode"] == "manual"


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_dispatch_next_bypasses_full_limit_and_ordinary_waiters(bridge, tmp_path, mode):
    with application.db() as conn:
        conn.executemany(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            [(f"account_{i}", f"账号{i}", "", str(tmp_path / f"p{i}"), f"2026-01-0{i}")
             for i in (2, 3)],
        )
    with bridge["session"]() as client:
        _set_mode(client, mode)
        _set_auto_dispatch(client, False)
        client.put("/api/settings/processing-limit", json={"processing_limit": 1})
        current = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "当前任务", "auto_start": True,
        }).json()
        assert _wait_status(client, current["id"], set(web_api.ACTIVE))["status"] in web_api.ACTIVE
        ordinary = client.post("/api/v1/videos/generations", json={
            "account_id": "account_2", "prompt": "普通调度仍等名额",
        }).json()
        assert client.post(
            f"/api/v1/videos/generations/{ordinary['id']}/dispatch"
        ).status_code == 200
        next_job = client.post("/api/v1/videos/generations", json={
            "prompt": "手动下一个自动分配账号",
        }).json()
        response = client.post(f"/api/v1/videos/generations/{current['id']}/dispatch-next")
        assert response.status_code == 200, response.text
        assert response.json()["job_id"] == ordinary["id"]
        state = _wait_status(client, ordinary["id"], set(web_api.ACTIVE))
        assert state["status"] in web_api.ACTIVE
        assert state["account_id"] == "account_2"
        assert bridge["started"] == [mode, "auto"]
        assert state["web"]["submit_mode"] == "auto"
        assert client.get(
            f"/api/v1/videos/generations/{next_job['id']}"
        ).json()["status"] == "queued"
        queue = client.get("/api/v1/videos/queue/status").json()
        assert queue["processing"] == 2
        assert queue["processing_limit"] == 1


def test_next_button_promotes_auto_dispatched_workers_at_full_limit(bridge, tmp_path):
    with application.db() as conn:
        conn.executemany(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            [(f"account_{i}", f"账号{i}", "", str(tmp_path / f"p{i}"), f"2026-01-0{i}")
             for i in (2, 3)],
        )
    with bridge["session"]() as client:
        _set_mode(client, "auto")
        _set_auto_dispatch(client, True)
        client.put("/api/settings/processing-limit", json={"processing_limit": 1})
        current = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "已满并发",
        }).json()
        assert _wait_status(client, current["id"], set(web_api.ACTIVE))["status"] in web_api.ACTIVE
        token = "full-limit-task-binding-" + "a" * 32
        with application.db() as conn:
            conn.execute(
                "UPDATE video_web_jobs SET extension_binding_token=? WHERE job_id=?",
                (token, current["id"]),
            )
        for i in (2, 3):
            waiting = client.post("/api/v1/videos/generations", json={
                "account_id": f"account_{i}", "prompt": f"已自动放行但等待名额{i}",
            }).json()
            worker = client.app.state.video_pending[waiting["id"]]
            state = client.get(f"/api/v1/videos/generations/{waiting['id']}").json()
            assert state["status"] == "queued"
            assert state["web"]["dispatched_at"]
            resolved = client.get(
                "/api/v1/extension/tasks/resolve", params={"binding_token": token}
            ).json()
            control = next(c for c in resolved["manual_controls"] if c["key"] == "next_task")
            assert control["enabled"] is True
            assert resolved["next_task_job_id"] == waiting["id"]
            response = client.post(f"/api/v1/videos/generations/{current['id']}/dispatch-next")
            assert response.status_code == 200, response.text
            assert response.json()["job_id"] == waiting["id"]
            state = _wait_status(client, waiting["id"], set(web_api.ACTIVE))
            assert state["status"] in web_api.ACTIVE
            assert client.app.state.video_pending[waiting["id"]] is worker
            assert client.get("/api/v1/videos/queue/status").json()["processing"] == i
        assert bridge["execution_accounts"] == ["account_1", "account_2", "account_3"]


def test_dispatch_next_waits_for_same_account_then_bypasses_limit(bridge, tmp_path):
    with application.db() as conn:
        conn.execute(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            ("account_2", "账号二", "", str(tmp_path / "p2"), "2026-01-02"),
        )
    with bridge["session"]() as client:
        _set_mode(client, "auto")
        _set_auto_dispatch(client, False)
        client.put("/api/settings/processing-limit", json={"processing_limit": 1})
        current = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "当前任务", "auto_start": True,
        }).json()
        assert _wait_status(client, current["id"], set(web_api.ACTIVE))["status"] in web_api.ACTIVE
        waiting = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "同账号等待",
        }).json()
        assert client.post(
            f"/api/v1/videos/generations/{current['id']}/dispatch-next"
        ).json()["job_id"] == waiting["id"]
        assert client.get(
            f"/api/v1/videos/generations/{waiting['id']}"
        ).json()["status"] == "queued"
        assert bridge["started"] == ["auto"]
        other = client.post("/api/v1/videos/generations", json={
            "account_id": "account_2", "prompt": "另一账号手动超出上限",
        }).json()
        assert client.post(
            f"/api/v1/videos/generations/{current['id']}/dispatch-next"
        ).json()["job_id"] == other["id"]
        assert _wait_status(client, other["id"], set(web_api.ACTIVE))["status"] in web_api.ACTIVE
        assert client.post(
            f"/api/v1/videos/generations/{current['id']}/cancel?confirm_cancel=true"
        ).status_code == 200
        state = _wait_status(client, waiting["id"], set(web_api.ACTIVE))
        assert state["status"] in web_api.ACTIVE
        assert state["account_id"] == "account_1"
        assert client.get("/api/v1/videos/queue/status").json()["processing"] == 2


@pytest.mark.parametrize("resume", [False, True], ids=["redispatch", "resume"])
def test_dispatch_next_keeps_bypass_after_allocation_deferral(bridge, tmp_path, resume):
    with application.db() as conn:
        conn.execute(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            ("account_2", "账号二", "", str(tmp_path / "p2"), "2026-01-02"),
        )
    with bridge["session"]() as client:
        _set_mode(client, "manual")
        _set_auto_dispatch(client, False)
        client.put("/api/settings/processing-limit", json={"processing_limit": 1})
        current = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "当前任务", "auto_start": True,
        }).json()
        assert _wait_status(client, current["id"], set(web_api.ACTIVE))["status"] in web_api.ACTIVE
        next_job = client.post("/api/v1/videos/generations", json={"prompt": "稍后自动重派"}).json()
        with application.db() as conn:
            conn.execute("UPDATE accounts SET paused=1")
        response = client.post(f"/api/v1/videos/generations/{current['id']}/dispatch-next")
        assert response.status_code == 200, response.text
        deadline = time.time() + 5
        while time.time() < deadline:
            state = client.get(f"/api/v1/videos/generations/{next_job['id']}").json()
            if state["web"]["next_retry_at"]:
                break
            time.sleep(0.01)
        assert state["web"]["next_retry_at"]
        assert state["account_id"] is None
        assert state["web"]["submit_mode"] == "auto"
        with application.db() as conn:
            conn.execute("UPDATE accounts SET paused=0")
            conn.execute("UPDATE video_web_jobs SET next_retry_at='' WHERE job_id=?", (next_job["id"],))

        async def redispatch():
            return client.app.state.video_redispatch()

        assert (_resume_in_loop(client) if resume else client.portal.call(redispatch)) == 1
        state = _wait_status(client, next_job["id"], set(web_api.ACTIVE))
        assert state["status"] in web_api.ACTIVE
        assert state["account_id"] == "account_2"
        assert state["web"]["submit_mode"] == "auto"
        assert bridge["started"] == ["manual", "auto"]
        assert client.get("/api/v1/videos/queue/status").json()["processing"] == 2
        assert client.post(
            f"/api/v1/videos/generations/{next_job['id']}/cancel?confirm_cancel=true"
        ).status_code == 200
        assert client.post(
            f"/api/v1/videos/generations/{next_job['id']}/reset?confirm_reset=true"
        ).status_code == 200
        with application.db() as conn:
            assert conn.execute(
                "SELECT bypass_processing_limit FROM video_web_jobs WHERE job_id=?",
                (next_job["id"],),
            ).fetchone()[0] == 0


def test_setting_default_is_manual_and_persists(bridge):
    """新库的自动调度与自动执行默认均关闭，且可分别持久化。"""
    client = bridge["client"]
    assert client.get("/api/settings/video-submit-mode").json()["submit_mode"] == "manual"
    assert client.get("/api/settings/auto-dispatch").json()["auto_dispatch_enabled"] is False
    _set_mode(client, "auto")
    _set_auto_dispatch(client, True)
    assert client.get("/api/settings/video-submit-mode").json()["submit_mode"] == "auto"
    assert client.get("/api/settings/auto-dispatch").json()["auto_dispatch_enabled"] is True


def test_auto_dispatch_interval_defaults_and_persists(bridge):
    client = bridge["client"]
    assert client.get("/api/settings/auto-dispatch-interval").json() == {
        "auto_dispatch_interval_seconds": 5,
    }

    updated = client.put(
        "/api/settings/auto-dispatch-interval",
        json={"auto_dispatch_interval_seconds": 12},
    )
    assert updated.status_code == 200
    assert updated.json() == {"auto_dispatch_interval_seconds": 12}
    assert client.get("/api/settings/auto-dispatch-interval").json() == {
        "auto_dispatch_interval_seconds": 12,
    }
    assert client.put(
        "/api/settings/auto-dispatch-interval",
        json={"auto_dispatch_interval_seconds": 0},
    ).status_code == 422


def test_scheduled_dispatch_setting_defaults_and_persists(bridge):
    client = bridge["client"]
    assert client.get("/api/settings/scheduled-dispatch").json() == {
        "scheduled_dispatch_enabled": False,
        "scheduled_dispatch_time": "02:00",
        "scheduled_dispatch_last_date": "",
    }

    updated = client.put(
        "/api/settings/scheduled-dispatch",
        json={
            "scheduled_dispatch_enabled": True,
            "scheduled_dispatch_time": "03:15",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["scheduled_dispatch_enabled"] is True
    assert updated.json()["scheduled_dispatch_time"] == "03:15"
    assert client.put(
        "/api/settings/scheduled-dispatch",
        json={
            "scheduled_dispatch_enabled": True,
            "scheduled_dispatch_time": "25:00",
        },
    ).status_code == 422


def test_scheduled_dispatch_releases_pending_jobs_once_per_local_day(bridge):
    with bridge["session"]() as client:
        _set_mode(client, "auto")
        _set_auto_dispatch(client, False)
        first = client.post("/api/v1/videos/generations", json={
            "prompt": "凌晨批量任务一", "duration": 4,
        }).json()
        second = client.post("/api/v1/videos/generations", json={
            "prompt": "凌晨批量任务二", "duration": 4,
        }).json()
        setting = client.put(
            "/api/settings/scheduled-dispatch",
            json={
                "scheduled_dispatch_enabled": True,
                "scheduled_dispatch_time": "02:00",
            },
        ).json()

        async def run_at(value):
            return client.app.state.video_run_scheduled_dispatch(value)

        zone = timezone(timedelta(hours=8))
        previous_date = setting["scheduled_dispatch_last_date"] or "2026-11-06"
        run_date = datetime.strptime(previous_date, "%Y-%m-%d").date() + timedelta(days=1)
        assert client.portal.call(
            run_at, datetime.combine(run_date, datetime.min.time(), zone).replace(
                hour=1, minute=59
            )
        ) == 0
        assert client.portal.call(
            run_at, datetime.combine(run_date, datetime.min.time(), zone).replace(
                hour=2
            )
        ) == 2
        for job_id in (first["id"], second["id"]):
            state = client.get(
                f"/api/v1/videos/generations/{job_id}"
            ).json()
            assert state["web"]["dispatched_at"]

        late = client.post("/api/v1/videos/generations", json={
            "prompt": "当天触发后新来的任务", "duration": 4,
        }).json()
        assert client.portal.call(
            run_at, datetime.combine(run_date, datetime.min.time(), zone).replace(
                hour=23
            )
        ) == 0
        assert not client.get(
            f"/api/v1/videos/generations/{late['id']}"
        ).json()["web"]["dispatched_at"]
        assert client.portal.call(
            run_at,
            datetime.combine(
                run_date + timedelta(days=1), datetime.min.time(), zone
            ).replace(hour=2, minute=1),
        ) == 1
        assert client.get(
            f"/api/v1/videos/generations/{late['id']}"
        ).json()["web"]["dispatched_at"]


def test_auto_switch_on_exception_setting_defaults_off_and_persists(bridge):
    client = bridge["client"]
    assert client.get("/api/settings/auto-switch-on-exception").json() == {
        "auto_switch_on_exception": False,
    }
    updated = client.put(
        "/api/settings/auto-switch-on-exception",
        json={"auto_switch_on_exception": True},
    )
    assert updated.status_code == 200
    assert updated.json() == {"auto_switch_on_exception": True}
    assert client.get("/api/settings/auto-switch-on-exception").json() == {
        "auto_switch_on_exception": True,
    }


def test_worker_failure_stays_for_human_review_with_legacy_claude_setting(bridge, monkeypatch):
    monkeypatch.setenv("DOUBAO_AGENT_ENABLED", "1")
    with application.db() as conn:
        conn.execute(
            "INSERT INTO local_settings(key,value) VALUES('claude_recovery_enabled','1')"
        )
    bridge["failures"]["remaining"] = 1
    with bridge["session"]() as client:
        assert client.get("/api/settings/claude-recovery").status_code == 404
        assert client.put(
            "/api/settings/claude-recovery", json={"claude_recovery_enabled": True}
        ).status_code == 404
        assert client.post("/api/internal/agent-recovery/tool", json={}).status_code == 404
        assert not hasattr(client.app.state, "video_recovery_service")
        assert not hasattr(client.app.state, "video_resume_recovery")
        _set_mode(client, "auto")
        _set_auto_dispatch(client, True)
        created = client.post("/api/v1/videos/generations", json={
            "prompt": "异常后等待人工复查", "duration": 4,
        })
        assert created.status_code == 202, created.text
        state = _wait_status(client, created.json()["id"], {"needs_review"})
        assert state["status"] == "needs_review", state
        assert state["web"]["error_code"] == "WORKER_EXCEPTION"
        assert state["web"]["quota_state"] == "released"
        assert "recovery" not in state
        assert bridge["execution_accounts"] == ["account_1"]


def test_worker_exception_reselects_largest_quota_account_and_restarts_once(bridge, tmp_path):
    with application.db() as conn:
        conn.execute(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            (
                "account_2",
                "账号二",
                "",
                str(tmp_path / "p2"),
                "2026-01-02T00:00:00+00:00",
            ),
        )

    bridge["failures"]["remaining"] = 1
    with bridge["session"]() as client:
        _set_mode(client, "auto")
        _set_auto_dispatch(client, True)
        client.put(
            "/api/settings/auto-switch-on-exception",
            json={"auto_switch_on_exception": True},
        )
        created = client.post(
            "/api/v1/videos/generations",
            json={"prompt": "异常后换号", "duration": 4},
        )
        assert created.status_code == 202, created.text

        state = _wait_status(
            client,
            created.json()["id"],
            set(web_api.ACTIVE),
        )
        deadline = time.time() + 10
        while len(bridge["execution_accounts"]) < 2 and time.time() < deadline:
            time.sleep(0.01)
        state = client.get(
            f"/api/v1/videos/generations/{created.json()['id']}"
        ).json()

        assert bridge["execution_accounts"][:2] == ["account_1", "account_1"]
        assert bridge["started"][:2] == ["auto", "auto"]
        assert state["account_id"] == "account_1"
        assert state["status"] in web_api.ACTIVE
        assert state["web"]["submit_mode"] == "auto"


def test_task_acceptance_switch_rejects_only_new_tasks(bridge):
    client = bridge["client"]
    assert client.get("/api/settings/task-acceptance").json() == {
        "accepting_tasks": True,
    }

    stopped = client.put(
        "/api/settings/task-acceptance",
        json={"accepting_tasks": False},
    )
    assert stopped.status_code == 200
    rejected = client.post("/api/v1/videos/generations", json={
        "prompt": "不应接收的新任务", "duration": 4,
    })
    assert rejected.status_code == 503
    assert rejected.json()["detail"] == "本机当前已停止接受新任务"
    multipart_rejected = client.post(
        "/api/v1/videos",
        data={"prompt": "multipart 新任务"},
    )
    assert multipart_rejected.status_code == 503

    resumed = client.put(
        "/api/settings/task-acceptance",
        json={"accepting_tasks": True},
    )
    assert resumed.json() == {"accepting_tasks": True}
    accepted = client.post("/api/v1/videos/generations", json={
        "prompt": "恢复后接收的任务", "duration": 4, "auto_start": False,
    })
    assert accepted.status_code == 202


def test_auto_dispatch_can_prepare_manual_execution(bridge):
    client = bridge["client"]
    _set_mode(client, "manual")
    _set_auto_dispatch(client, True)

    created = client.post("/api/v1/videos/generations", json={
        "prompt": "自动调度但人工发送", "duration": 4,
    })
    assert created.status_code == 202, created.text
    state = _wait_status(client, created.json()["id"], set(web_api.ACTIVE))
    assert state["status"] in web_api.ACTIVE
    assert state["web"]["submit_mode"] == "manual"
    assert bridge["started"] == ["manual"]


def test_manual_dispatch_can_start_automatic_execution(bridge):
    client = bridge["client"]
    _set_mode(client, "auto")
    _set_auto_dispatch(client, False)

    created = client.post("/api/v1/videos/generations", json={
        "prompt": "手动调度但自动执行", "duration": 4,
    })
    assert created.status_code == 202, created.text
    job = created.json()
    assert job["status"] == "queued"
    assert job["web"]["submit_mode"] == "auto"
    time.sleep(0.1)
    assert bridge["started"] == []

    response = client.post(f"/api/v1/videos/generations/{job['id']}/dispatch")
    assert response.status_code == 200, response.text
    state = _wait_status(client, job["id"], set(web_api.ACTIVE))
    assert state["status"] in web_api.ACTIVE
    assert state["web"]["submit_mode"] == "auto"
    assert bridge["started"] == ["auto"]


def test_undispatched_auto_job_is_not_resumed_after_setting_saved(bridge):
    with bridge["session"]() as client:
        _set_mode(client, "auto")
        _set_auto_dispatch(client, False)
        job = client.post("/api/v1/videos/generations", json={
            "prompt": "关闭自动调度后等待人工放行", "duration": 4,
        }).json()
        assert job["status"] == "queued"
        assert not job["web"]["dispatched_at"]
        assert _resume_in_loop(client) == 0
        assert bridge["started"] == []

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
        # The interrupted blocker now needs review and still owns its account.
        # Recovery must leave this task unassigned until the operator ends it.
        deadline = time.time() + 5
        while time.time() < deadline:
            state = client.get(f"/api/v1/videos/generations/{probe['id']}").json()
            if state["web"]["next_retry_at"]:
                break
            time.sleep(0.01)
        assert state["web"]["next_retry_at"]
        assert state["status"] == "queued"
        assert state["account_id"] is None
        assert state["web"]["quota_state"] == ""
        assert client.post(
            f"/api/v1/videos/generations/{blocker['id']}/cancel?confirm_cancel=true"
        ).status_code == 200
        with application.db() as conn:
            conn.execute("UPDATE video_web_jobs SET next_retry_at='' WHERE job_id=?", (probe["id"],))

        async def redispatch():
            return client.app.state.video_redispatch()

        assert client.portal.call(redispatch) == 1
        state = _wait_status(client, probe["id"], set(web_api.ACTIVE))
        events = client.get(
            f"/api/v1/videos/generations/{probe['id']}/events"
        ).json()
        assert state["status"] in web_api.ACTIVE, (state, events)
        assert state["account_id"] == "account_1"


def test_restart_auto_waits_for_processing_slot_before_allocating_account(bridge, tmp_path):
    with application.db() as conn:
        conn.execute(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            ("account_2", "账号二", "", str(tmp_path / "p2"), "2026-01-02"),
        )
    with bridge["session"]() as client:
        _set_mode(client, "manual")
        _set_auto_dispatch(client, False)
        client.put("/api/settings/processing-limit", json={"processing_limit": 1})
        blocker = client.post("/api/v1/videos/generations", json={
            "account_id": "account_2", "prompt": "占住名额", "duration": 4,
            "auto_start": True,
        }).json()
        assert _wait_status(client, blocker["id"], set(web_api.ACTIVE))["status"] in web_api.ACTIVE
        job = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "换号后自动分配", "duration": 4,
        }).json()

        response = client.post(f"/api/v1/videos/generations/{job['id']}/restart-auto")
        assert response.status_code == 200, response.text
        state = client.get(f"/api/v1/videos/generations/{job['id']}").json()
        assert state["status"] == "queued"
        assert state["account_id"] is None
        assert state["web"]["submit_mode"] == "auto"
        assert state["web"]["dispatched_at"]
        with application.db() as conn:
            assert web_api.quota_summary(conn, "account_1")["reserved_seconds"] == 0

        assert client.post(
            f"/api/v1/videos/generations/{blocker['id']}/cancel?confirm_cancel=true"
        ).status_code == 200
        state = _wait_status(client, job["id"], set(web_api.ACTIVE))
        assert state["status"] in web_api.ACTIVE
        assert state["account_id"] == "account_1"
        assert bridge["started"] == ["manual", "auto"]


@pytest.mark.parametrize("resume", [False, True], ids=["redispatch", "resume"])
def test_restart_auto_keeps_automatic_allocation_after_deferral(bridge, tmp_path, resume):
    with application.db() as conn:
        conn.execute(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            ("account_2", "账号二", "", str(tmp_path / "p2"), "2026-01-02"),
        )
    with bridge["session"]() as client:
        _set_mode(client, "manual")
        _set_auto_dispatch(client, False)
        job = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "暂不可分配时自动重试", "duration": 4,
        }).json()
        with application.db() as conn:
            conn.execute("UPDATE accounts SET paused=1")
        response = client.post(f"/api/v1/videos/generations/{job['id']}/restart-auto")
        assert response.status_code == 200, response.text
        deadline = time.time() + 5
        while time.time() < deadline:
            state = client.get(f"/api/v1/videos/generations/{job['id']}").json()
            if state["web"]["next_retry_at"]:
                break
            time.sleep(0.01)
        assert state["web"]["next_retry_at"]
        assert state["status"] == "queued"
        assert state["account_id"] is None
        assert state["web"]["submit_mode"] == "auto"
        assert bridge["started"] == []

        with application.db() as conn:
            conn.execute("UPDATE accounts SET paused=0")
            conn.execute("UPDATE video_web_jobs SET next_retry_at='' WHERE job_id=?", (job["id"],))

        async def redispatch():
            return client.app.state.video_redispatch()

        assert (_resume_in_loop(client) if resume else client.portal.call(redispatch)) == 1
        state = _wait_status(client, job["id"], set(web_api.ACTIVE))
        assert state["status"] in web_api.ACTIVE
        assert state["account_id"] == "account_1"
        assert bridge["started"] == ["auto"]


@pytest.mark.parametrize("occupied_status", [
    "opening", "generating", "awaiting_manual_send", "needs_review",
])
def test_automatic_queue_never_binds_or_reserves_busy_accounts(bridge, occupied_status):
    with bridge["session"]() as client:
        _set_mode(client, "auto")
        _set_auto_dispatch(client, False)
        client.put("/api/settings/processing-limit", json={"processing_limit": 0})
        occupied = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "账号已有任务", "duration": 4,
        }).json()
        # Persisted ownership must count even without a live worker/account lock.
        with application.db() as conn:
            conn.execute("UPDATE jobs SET status=? WHERE id=?", (occupied_status, occupied["id"]))
        waiting = client.post("/api/v1/videos/generations", json={
            "prompt": "等空闲账号再分配", "duration": 4,
        }).json()
        assert waiting["account_id"] is None
        assert waiting["web"]["quota_state"] == ""
        assert not waiting["web"]["dispatched_at"]
        response = client.post(f"/api/v1/videos/generations/{waiting['id']}/dispatch")
        assert response.status_code == 200, response.text
        deadline = time.time() + 5
        while time.time() < deadline:
            state = client.get(f"/api/v1/videos/generations/{waiting['id']}").json()
            if state["web"]["next_retry_at"]:
                break
            time.sleep(0.01)
        assert state["web"]["next_retry_at"]
        assert state["status"] == "queued"
        assert state["account_id"] is None
        assert state["web"]["quota_state"] == ""
        assert bridge["started"] == []
        with application.db() as conn:
            assert web_api.quota_summary(conn, "account_1")["reserved_seconds"] == 4
        assert client.post(
            f"/api/v1/videos/generations/{occupied['id']}/cancel?confirm_cancel=true"
        ).status_code == 200
        with application.db() as conn:
            conn.execute("UPDATE video_web_jobs SET next_retry_at='' WHERE job_id=?", (waiting["id"],))

        async def redispatch():
            return client.app.state.video_redispatch()

        assert client.portal.call(redispatch) == 1
        state = _wait_status(client, waiting["id"], set(web_api.ACTIVE))
        assert state["status"] in web_api.ACTIVE
        assert state["account_id"] == "account_1"
        assert bridge["started"] == ["auto"]


@pytest.mark.parametrize("manual_selection", [False, True])
def test_resume_releases_only_old_automatic_queue_bindings(bridge, manual_selection):
    with bridge["session"]() as client:
        _set_mode(client, "auto")
        _set_auto_dispatch(client, False)
        client.put("/api/settings/processing-limit", json={"processing_limit": 1})
        blocker = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "占住处理名额", "duration": 4,
            "auto_start": True,
        }).json()
        assert _wait_status(client, blocker["id"], set(web_api.ACTIVE))["status"] in web_api.ACTIVE
        waiting = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "旧版提前分配", "duration": 4,
        }).json()
        with application.db() as conn:
            conn.execute(
                "UPDATE video_web_jobs SET dispatched_at=? WHERE job_id=?",
                (web_api.ts(), waiting["id"]),
            )
            conn.execute(
                "INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                (waiting["id"], "queued", "进入处理前分配账号 account_1，并预占 4 秒额度", web_api.ts()),
            )
        if manual_selection:
            assert client.post(
                f"/api/jobs/{waiting['id']}/account", json={"account_id": "account_1"}
            ).status_code == 200
        assert _resume_in_loop(client) == 1
        state = client.get(f"/api/v1/videos/generations/{waiting['id']}").json()
        assert state["status"] == "queued"
        assert state["account_id"] == ("account_1" if manual_selection else None)
        assert state["web"]["quota_state"] == ("reserved" if manual_selection else "")
        with application.db() as conn:
            assert web_api.quota_summary(conn, "account_1")["reserved_seconds"] == (
                8 if manual_selection else 4
            )


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
    assert "disabled:!takeover&&(!canPrepare||dispatched)" in body
    assert "已交给调度器，正在等处理名额或可用账号" in source


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("submit_mode", ["manual", "auto"])
def test_undispatch_stops_waiting_worker_and_allows_redispatch(bridge, legacy, submit_mode):
    with bridge["session"]() as client:
        _set_mode(client, submit_mode)
        if not legacy:
            _set_auto_dispatch(client, False)
        client.put("/api/settings/processing-limit", json={"processing_limit": 1})
        blocker = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "占用名额", "duration": 4,
            "auto_start": True, "submit_mode": "auto",
        }).json()
        assert _wait_status(client, blocker["id"], set(web_api.ACTIVE))["status"] in web_api.ACTIVE
        waiting = client.post("/api/v1/videos/generations", json={
            "account_id": "account_1", "prompt": "可撤回的排队任务", "duration": 4,
            "auto_start": False, "submit_mode": submit_mode,
        }).json()
        job_id = waiting["id"]
        path = f"/api/v1/videos/generations/{job_id}"
        assert client.post(f"{path}/dispatch").status_code == 200
        response = client.post(f"{path}/undispatch")
        assert response.status_code == 200, response.text
        item = response.json()
        assert item["status"] == "queued"
        assert item["account_id"] == "account_1"
        assert item["web"]["dispatched_at"] == ""
        assert item["web"]["submit_mode"] == submit_mode
        with application.db() as conn:
            assert web_api.quota_summary(conn, "account_1")["reserved_seconds"] == 4
        # Repeating withdrawal must not subtract the running task's reservation.
        assert client.post(f"{path}/undispatch").status_code == 200
        with application.db() as conn:
            assert web_api.quota_summary(conn, "account_1")["reserved_seconds"] == 4
        async def tick():
            return client.app.state.video_redispatch()
        assert client.portal.call(tick) == 0
        assert _resume_in_loop(client) == 0
        queue = client.get("/api/v1/videos/queue/status").json()
        assert queue["pending_dispatched"] == 0
        assert queue["pending_undispatched"] == 1
        # A free processing slot must not wake the withdrawn worker.
        assert client.post(
            f"/api/v1/videos/generations/{blocker['id']}/cancel?confirm_cancel=true"
        ).status_code == 200
        assert client.portal.call(tick) == 0
        assert client.get(path).json()["status"] == "queued"
        assert len(bridge["started"]) == 1
        assert client.post(f"{path}/dispatch").status_code == 200
        state = _wait_status(client, job_id, set(web_api.ACTIVE))
        assert state["status"] in web_api.ACTIVE
        assert state["web"]["dispatched_at"]
        assert state["web"]["submit_mode"] == submit_mode
        with application.db() as conn:
            assert web_api.quota_summary(conn, "account_1")["reserved_seconds"] == 4


def test_undispatch_persists_across_restart_and_clears_bypass(bridge):
    with bridge["session"]() as client:
        _set_mode(client, "auto")
        job = client.post("/api/v1/videos/generations", json={
            "prompt": "重启后等待再次放行", "duration": 4, "auto_start": False,
        }).json()
        path = f"/api/v1/videos/generations/{job['id']}"
        with application.db() as conn:
            conn.execute(
                "UPDATE video_web_jobs SET dispatched_at=?,bypass_processing_limit=1,"
                "next_retry_at=? WHERE job_id=?", (web_api.ts(), web_api.ts(), job["id"]),
            )
        response = client.post(f"{path}/undispatch")
        assert response.status_code == 200, response.text
        assert not response.json()["web"]["bypass_processing_limit"]
        assert response.json()["web"]["next_retry_at"] == ""
    with bridge["session"]() as client:
        assert _resume_in_loop(client) == 0
        assert client.get(path).json()["web"]["dispatched_at"] == ""
        assert bridge["started"] == []


def test_undispatch_rejects_running_task(bridge):
    with bridge["session"]() as client:
        job = client.post("/api/v1/videos/generations", json={
            "prompt": "已经开始处理", "duration": 4, "auto_start": True,
            "submit_mode": "auto",
        }).json()
        path = f"/api/v1/videos/generations/{job['id']}"
        before = _wait_status(client, job["id"], set(web_api.ACTIVE))
        assert before["status"] in web_api.ACTIVE
        response = client.post(f"{path}/undispatch")
        assert response.status_code == 409, response.text
        assert client.get(path).json()["web"]["dispatched_at"] == before["web"]["dispatched_at"]


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

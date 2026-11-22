import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application


def _day():
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def test_pending_job_gets_account_only_when_it_starts(tmp_path, monkeypatch):
    """待处理任务不分配账号；进入处理前才挑选账号并预占额度。"""
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "pending-account.sqlite3")

    with TestClient(application.app) as client:
        exact = client.post("/api/accounts", json={"name": "刚好够用", "priority": 100}).json()
        larger = client.post("/api/accounts", json={"name": "额度较多", "priority": 0}).json()

        with application.db() as conn:
            conn.execute(
                "INSERT INTO account_quota"
                "(account_id, day, reserved_seconds, consumed_seconds) "
                "VALUES(?, ?, 0, 26)",
                (exact["id"], _day()),
            )

        created = client.post("/api/v1/videos/generations", json={
            "prompt": "建单时不分配账号",
            "duration": 4,
            "auto_start": False,
        })
        assert created.status_code == 202, created.text
        job = created.json()
        assert job["status"] == "queued"
        # 关键断言：待处理任务没有账号，也没有预占额度。
        assert job["account_id"] is None
        assert job["web"]["quota_state"] == ""

        with application.db() as conn:
            reserved = conn.execute(
                "SELECT SUM(reserved_seconds) FROM account_quota WHERE day=?", (_day(),)
            ).fetchone()[0]
        assert (reserved or 0) == 0

        # 启动只是进入处理队列；真正拿到处理名额、进入处理前才分配账号。
        started = client.post(f"/api/v1/videos/generations/{job['id']}/start")
        assert started.status_code == 200, started.text

        def wait_assigned(job_id):
            deadline = time.time() + 10
            state = {}
            while time.time() < deadline:
                state = client.get(f"/api/v1/videos/generations/{job_id}").json()
                if state["account_id"]:
                    break
                time.sleep(0.01)
            return state

        state = wait_assigned(job["id"])
        # 只按余额选择，不采用刚好够用或优先级规则。
        assert state["account_id"] == larger["id"], state
        assert state["web"]["quota_state"] == "reserved", state

        # 删除首个任务释放账号锁，再验证第二个任务仍按最大余额选择。
        with application.db() as conn:
            conn.execute(
                "UPDATE account_quota SET consumed_seconds=30, reserved_seconds=0 WHERE account_id=?",
                (exact["id"],),
            )
            conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (job["id"],))
        task = application.app.state.video_pending.pop(job["id"], None)
        if task is not None:
            task.cancel()
        deleted = client.post("/api/jobs/bulk-delete", json={"job_ids": [job["id"]]})
        assert deleted.status_code == 200, deleted.text
        with application.db() as conn:
            conn.execute(
                "UPDATE account_quota SET consumed_seconds=30, reserved_seconds=0 WHERE account_id=?",
                (exact["id"],),
            )

        fallback = client.post("/api/v1/videos/generations", json={
            "prompt": "刚好够用的账号额度耗尽后使用其他账号",
            "duration": 4,
            "auto_start": False,
        }).json()
        assert fallback["account_id"] is None
        started_fallback = client.post(f"/api/v1/videos/generations/{fallback['id']}/start")
        assert started_fallback.status_code == 200, started_fallback.text
        state = wait_assigned(fallback["id"])
        assert state["account_id"] == larger["id"], state

        client.post(f"/api/jobs/bulk-delete", json={"job_ids": [job["id"], fallback["id"]]})


def test_pending_job_cannot_be_forced_onto_an_account_at_creation(tmp_path, monkeypatch):
    """显式给账号仍在建单时被接受，但账号由调用方给出，不由后端自动挑选。"""
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "explicit-account.sqlite3")

    with TestClient(application.app) as client:
        account = client.post("/api/accounts", json={"name": "指定账号", "priority": 0}).json()
        created = client.post("/api/v1/videos/generations", json={
            "prompt": "显式指定账号",
            "duration": 4,
            "account_id": account["id"],
            "auto_start": False,
        })
        assert created.status_code == 202, created.text
        job = created.json()
        assert job["account_id"] == account["id"]
        assert job["web"]["quota_state"] == "reserved"


def test_legacy_quota_override_is_persisted_without_blocking_overage(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "quota-override.sqlite3")

    with TestClient(application.app) as client:
        account = client.post(
            "/api/accounts", json={"name": "额度已用完", "priority": 0}
        ).json()
        with application.db() as conn:
            conn.execute(
                "INSERT INTO account_quota"
                "(account_id, day, reserved_seconds, consumed_seconds) "
                "VALUES(?, ?, 0, 30)",
                (account["id"], _day()),
            )

        payload = {
            "account_id": account["id"],
            "prompt": "明确解除额度判断",
            "duration": 4,
            "auto_start": False,
        }
        accepted = client.post("/api/v1/videos/generations", json=payload)
        assert accepted.status_code == 202, accepted.text

        updated = client.put(
            "/api/settings/ignore-quota-check",
            json={"ignore_quota_check": True},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json() == {"ignore_quota_check": True}
        assert client.get("/api/settings/ignore-quota-check").json() == {
            "ignore_quota_check": True
        }

        forced = client.post("/api/v1/videos/generations", json=payload)
        assert forced.status_code == 202, forced.text
        job = forced.json()
        assert job["account_id"] == account["id"]
        assert job["web"]["quota_state"] == "reserved"
        assert "ignore_quota" not in job["web"]

        with application.db() as conn:
            quota = conn.execute(
                "SELECT reserved_seconds, consumed_seconds FROM account_quota "
                "WHERE account_id=? AND day=?",
                (account["id"], _day()),
            ).fetchone()
        assert quota["reserved_seconds"] == 8
        assert quota["consumed_seconds"] == 30


@pytest.mark.parametrize("legacy_override", [False, True])
def test_auto_dispatch_uses_largest_remainder_even_when_insufficient(
    tmp_path, monkeypatch, legacy_override
):
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "auto-quota-override.sqlite3")

    with TestClient(application.app) as client:
        low = client.post(
            "/api/accounts", json={"name": "零额度", "priority": 100}
        ).json()
        high = client.post(
            "/api/accounts", json={"name": "较高额度", "priority": 0}
        ).json()
        with application.db() as conn:
            conn.executemany(
                "INSERT INTO account_quota"
                "(account_id, day, reserved_seconds, consumed_seconds) "
                "VALUES(?, ?, 0, ?)",
                (
                    (low["id"], _day(), 30),
                    (high["id"], _day(), 27),
                ),
            )

        updated = client.put(
            "/api/settings/ignore-quota-check",
            json={"ignore_quota_check": legacy_override},
        )
        assert updated.status_code == 200, updated.text

        created = client.post(
            "/api/v1/videos/generations",
            json={
                "prompt": "额度不足时自动挑选剩余最多的账号",
                "duration": 4,
                "auto_start": False,
            },
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        started = client.post(f"/api/v1/videos/generations/{job_id}/start")
        assert started.status_code == 200, started.text

        deadline = time.time() + 10
        state = {}
        while time.time() < deadline:
            state = client.get(f"/api/v1/videos/generations/{job_id}").json()
            if state["account_id"]:
                break
            time.sleep(0.01)

        assert state["account_id"] == high["id"], state
        assert state["web"]["quota_state"] == "reserved", state

        task = application.app.state.video_pending.pop(job_id, None)
        if task is not None:
            task.cancel()

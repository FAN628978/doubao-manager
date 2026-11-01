import sys
import time
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application


def _day():
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def test_pending_job_gets_account_only_when_it_starts(tmp_path, monkeypatch):
    """待处理任务不分配账号；进入处理前才挑选账号并预占额度。"""
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "pending-account.sqlite3")

    with TestClient(application.app) as client:
        exact = client.post("/api/accounts", json={"name": "刚好够用", "priority": 0}).json()
        larger = client.post("/api/accounts", json={"name": "额度较多", "priority": 100}).json()

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
        # 优先填满“刚好够用”的账号。
        assert state["account_id"] == exact["id"], state
        assert state["web"]["quota_state"] == "reserved", state

        # 第一个任务在测试里拿不到真实浏览器，会一直卡在“正在处理”占着 exact 的账号锁；
        # 直接删掉它释放账号锁，再用“额度已用尽”验证第二个任务会切到其他账号。
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

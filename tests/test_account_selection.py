import sys
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application


def test_auto_account_selection_prefers_best_quota_fit(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "account-fit.sqlite3")

    with TestClient(application.app) as client:
        exact = client.post("/api/accounts", json={
            "name": "刚好够用",
            "priority": 0,
        }).json()
        larger = client.post("/api/accounts", json={
            "name": "额度较多",
            "priority": 100,
        }).json()

        day = datetime.now().astimezone().strftime("%Y-%m-%d")
        with application.db() as conn:
            conn.execute(
                "INSERT INTO account_quota"
                "(account_id, day, reserved_seconds, consumed_seconds) "
                "VALUES(?, ?, 0, 26)",
                (exact["id"], day),
            )

        created = client.post("/api/v1/videos/generations", json={
            "prompt": "优先填满刚好够用的账号",
            "duration": 4,
            "auto_start": False,
        })
        assert created.status_code == 202, created.text
        assert created.json()["account_id"] == exact["id"]

        fallback = client.post("/api/v1/videos/generations", json={
            "prompt": "刚好够用的账号额度耗尽后使用其他账号",
            "duration": 4,
            "auto_start": False,
        })
        assert fallback.status_code == 202, fallback.text
        assert fallback.json()["account_id"] == larger["id"]

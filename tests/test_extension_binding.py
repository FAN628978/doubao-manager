import asyncio
import sqlite3
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app as application
import web_api
from doubao_manager.web.schema import ensure_schema


class BoundPage:
    def __init__(self):
        self.init_scripts = []
        self.evaluated = []
        self.closed = False

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    async def evaluate(self, script):
        self.evaluated.append(script)

    def is_closed(self):
        return self.closed


def build_bridge(tmp_path, monkeypatch):
    monkeypatch.delenv("DOUBAO_LOCAL_API_KEY", raising=False)
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "extension.sqlite3")
    application.init_db()
    ensure_schema(application.db)
    with application.db() as connection:
        connection.execute(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            ("account_1", "扩展账号", "", "data/profiles/account_1", "2026-01-01"),
        )
        for index in range(2):
            job_id = f"job_{index}"
            connection.execute(
                "INSERT INTO jobs(id,project_id,episode_id,shot_id,prompt,account_id,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, "project", "ep01", f"shot{index}", "prompt", "account_1",
                 "awaiting_manual_send", "2026-01-01", "2026-01-02"),
            )
            connection.execute(
                "INSERT INTO video_web_jobs(job_id,task_name,submit_mode,phase) VALUES(?,?,?,?)",
                (job_id, f"分镜 {index}", "manual", "awaiting_manual_send"),
            )
            connection.execute(
                "INSERT INTO video_web_events(job_id,status,message,timestamp) VALUES(?,?,?,?)",
                (job_id, "awaiting_manual_send", f"任务 {index} 已准备", "2026-01-02T03:04:05+00:00"),
            )

    async def browser(_account_id):
        raise AssertionError("绑定解析不应启动浏览器")

    bridge = FastAPI()
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
    return bridge


def test_explicit_tab_tokens_resolve_distinct_tasks(tmp_path, monkeypatch):
    bridge = build_bridge(tmp_path, monkeypatch)
    first, second = BoundPage(), BoundPage()
    first_token = asyncio.run(bridge.state.video_bind_task_page("job_0", first))
    second_token = asyncio.run(bridge.state.video_bind_task_page("job_1", second))

    assert first_token != second_token
    assert len(first_token) >= 32
    assert first_token in first.init_scripts[0]
    assert "doubao-manager-task-binding" in first.init_scripts[0]
    assert "X-API-Key" not in first.init_scripts[0]
    assert bridge.state.video_prepared_pages["job_0"] is first
    assert bridge.state.video_prepared_pages["job_1"] is second

    with TestClient(bridge) as client:
        resolved_first = client.get(
            "/api/v1/extension/tasks/resolve", params={"binding_token": first_token}
        )
        resolved_second = client.get(
            "/api/v1/extension/tasks/resolve", params={"binding_token": second_token}
        )
        assert resolved_first.status_code == 200, resolved_first.text
        assert resolved_second.status_code == 200, resolved_second.text
        assert resolved_first.json()["job_id"] == "job_0"
        assert resolved_second.json()["job_id"] == "job_1"
        assert resolved_first.json()["account_name"] == "扩展账号"
        assert resolved_first.json()["last_action"] == "任务 0 已准备"
        actions = resolved_first.json()["actions"]
        assert [action["label"] for action in actions] == [
            "提交素材", "确认发送", "发送确认语", "下载视频", "推送服务器"
        ]
        assert [action["enabled"] for action in actions] == [True, True, True, True, False]
        generation = client.get("/api/v1/videos/generations/job_0").json()
        assert "extension_binding_token" not in generation["web"]


def test_unknown_or_short_binding_is_never_guessed(tmp_path, monkeypatch):
    bridge = build_bridge(tmp_path, monkeypatch)
    with TestClient(bridge) as client:
        assert client.get(
            "/api/v1/extension/tasks/resolve", params={"binding_token": "short"}
        ).status_code == 404
        assert client.get(
            "/api/v1/extension/tasks/resolve", params={"binding_token": "x" * 43}
        ).status_code == 404


def test_content_script_uses_only_explicit_binding_marker():
    content = (ROOT / "browser/extension/content.js").read_text(encoding="utf-8")
    assert 'meta[name="doubao-manager-task-binding"]' in content
    assert "document.title" not in content
    assert "location.href" not in content


def test_extension_actions_call_existing_task_apis():
    background = (ROOT / "browser/extension/background.js").read_text(encoding="utf-8")
    expected = [
        "/dispatch", "/prepare-manual", "/send?confirm_send=true",
        "/send-confirmation?confirm_send=true", "/recall?force_redownload=true",
        "/push-result",
    ]
    for endpoint in expected:
        assert endpoint in background
    assert "taskActions[message.action]" in background

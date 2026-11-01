"""待处理不分配账号 / 进入处理前分配 / 处理完不自动归档 / 手动归档进“已归档”。

只验证队列与归档语义，不启动浏览器：自动任务用一个假的 perform 直接产出视频。
"""
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
import web_api


def _day():
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _seed_accounts(tmp_path):
    with application.db() as conn:
        conn.executemany(
            "INSERT INTO accounts(id,name,note,profile_dir,created_at) VALUES(?,?,?,?,?)",
            [
                ("account_1", "账号一", "", str(tmp_path / "p1"), "2026-01-01T00:00:00+00:00"),
                ("account_2", "账号二", "", str(tmp_path / "p2"), "2026-01-02T00:00:00+00:00"),
            ],
        )


def _reserved_seconds():
    with application.db() as conn:
        row = conn.execute(
            "SELECT SUM(reserved_seconds) AS reserved FROM account_quota"
        ).fetchone()
    return int(row["reserved"] or 0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setenv("DOUBAO_BROWSER_IDLE_CLOSE_SECONDS", "0")
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "queue.sqlite3")
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    return tmp_path


def test_pending_jobs_hold_no_account_until_they_start(env):
    """待处理任务不占账号、不占额度；额度只在真正开始执行前才预占。"""
    _seed_accounts(env)
    bridge = FastAPI()
    web_api.mount_video_api(
        bridge, db=application.db, get_account=application.get_account,
        get_job=application.get_job, browser=lambda _a: None,
        base_dir=env, downloads_dir=env / "videos", serialize=application.job_row,
    )
    with TestClient(bridge) as client:
        first = client.post("/api/v1/videos/generations", json={
            "prompt": "任务一", "duration": 4, "submit_mode": "manual",
        }).json()
        second = client.post("/api/v1/videos/generations", json={
            "prompt": "任务二", "duration": 4, "submit_mode": "manual",
        }).json()

        assert first["status"] == "queued"
        assert first["account_id"] is None
        assert second["account_id"] is None
        # 多个待处理任务不预占任何账号额度。
        assert _reserved_seconds() == 0

        status = client.get("/api/v1/videos/queue/status").json()
        assert status["pending"] == 2
        assert status["pending_unassigned"] == 2
        assert status["processing"] == 0

        # 操作者可以在进入处理前手动指定账号；指定本身也不预占额度。
        assigned = client.post(f"/api/jobs/{first['id']}/account",
                               json={"account_id": "account_1"})
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["account_id"] == "account_1"
        assert _reserved_seconds() == 0

        cleared = client.post(f"/api/jobs/{first['id']}/account", json={"account_id": None})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["account_id"] is None


def test_account_is_allocated_only_when_job_enters_processing(tmp_path, monkeypatch):
    """自动任务：建单后仍待处理、无账号；被调度进入处理时才分配账号并预占额度。"""
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "allocate.sqlite3")
    application.init_db()
    web_api.recover_interrupted_jobs(application.db)
    _seed_accounts(tmp_path)

    started_accounts = []
    release = asyncio.Event()

    async def fake_perform(ctx, req, assets, target, status, page_url, binding,
                           on_manual_ready=None, confirmation_text="",
                           close_on_success=False, on_review_page=None):
        started_accounts.append(req.account_id)
        # 停在“正在处理”，直到测试放行，才能稳定观察排队任务没被分配账号。
        await release.wait()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video")
        return {"output": str(target),
                "video_url": "https://example.com/v.mp4",
                "web_page_url": "https://www.doubao.com/chat/x"}

    class FakeContext:
        """worker 打开浏览器后只会把它交给 perform（此处被替换），给个最小占位即可。"""

        pages: list = []

        async def new_page(self):
            return None

    async def fake_browser(account_id):
        return FakeContext()

    # facade 会在 mount 时把当前 web_api.<name> 同步进实现模块，所以先替换 perform 再挂载。
    monkeypatch.setattr(web_api, "perform", fake_perform)
    bridge = FastAPI()
    web_api.mount_video_api(
        bridge, db=application.db, get_account=application.get_account,
        get_job=application.get_job, browser=fake_browser,
        base_dir=tmp_path, downloads_dir=tmp_path / "videos",
        serialize=application.job_row,
    )
    monkeypatch.setattr(web_api, "automatic_step_delay",
                        lambda mode: asyncio.sleep(0))

    # 处理上限设为 1：第一个任务进入处理后，第二个只能停在待处理，方便验证它没有账号。
    with application.db() as conn:
        conn.execute(
            "INSERT INTO local_settings(key,value) VALUES('processing_limit','1') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )

    with TestClient(bridge) as client:
        first = client.post("/api/v1/videos/generations", json={
            "prompt": "自动任务一", "duration": 4, "submit_mode": "auto",
        }).json()
        # 自动任务的分配发生在“开始执行”阶段，不发生在建单阶段：账号此刻要么还没分配，
        # 要么已经分配给正在处理的它（绝不可能是别的账号）。
        assert first["account_id"] in (None, "account_1", "account_2")

        # 等到第一个任务真正进入处理：它拿到账号。
        deadline = time.time() + 10
        first_state = {}
        while time.time() < deadline:
            first_state = client.get(f"/api/v1/videos/generations/{first['id']}").json()
            if first_state["status"] in web_api.ACTIVE:
                break
            time.sleep(0.01)
        assert first_state["status"] in web_api.ACTIVE, first_state["status"]
        # 处理中的任务拿到的账号，就是 perform 实际使用的账号。
        assert first_state["account_id"] == started_accounts[0], first_state

        # 第二个任务仍在待处理：它还没有账号，也没有预占额度。
        second = client.post("/api/v1/videos/generations", json={
            "prompt": "自动任务二", "duration": 4, "submit_mode": "auto",
        }).json()
        second_state = client.get(f"/api/v1/videos/generations/{second['id']}").json()
        assert second_state["status"] == "queued", second_state
        assert second_state["account_id"] is None, second_state
        assert second_state["web"]["quota_state"] == "", second_state

        release.set()
        for job_id in (first["id"], second["id"]):
            for _ in range(400):
                state = client.get(f"/api/v1/videos/generations/{job_id}").json()
                if state["status"] in ("downloaded", "needs_review", "failed"):
                    break
                time.sleep(0.01)
            assert state["status"] == "downloaded", state
            # 下载完成并不等于归档：archived_at 为空，任务留在“处理完”。
            assert state["web"]["archived_at"] == ""
            assert state["archived"] is False


def test_downloaded_job_stays_finished_until_user_archives(env):
    """处理完 → 已归档只能由用户操作；归档必须存在本地文件，且可以取消归档。"""
    _seed_accounts(env)
    bridge = FastAPI()
    web_api.mount_video_api(
        bridge, db=application.db, get_account=application.get_account,
        get_job=application.get_job, browser=lambda _a: None,
        base_dir=env, downloads_dir=env / "videos", serialize=application.job_row,
    )
    # /api/stats 与 /api/jobs 挂在主应用上，队列与归档接口挂在 bridge 上。
    with TestClient(application.app) as main, TestClient(bridge) as client:
        job = client.post("/api/v1/videos/generations", json={
            "prompt": "完成后等待归档", "duration": 4, "account_id": "account_1",
            "submit_mode": "manual",
        }).json()
        job_id = job["id"]
        video = env / "videos" / "library" / "archived.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"video-bytes")
        with application.db() as conn:
            conn.execute("UPDATE jobs SET status='downloaded',video_path=? WHERE id=?",
                         (str(video.relative_to(env)), job_id))

        stats = main.get("/api/stats").json()
        assert stats["finished"] == 1
        assert stats["archived"] == 0

        listed = {item["id"]: item for item in main.get("/api/jobs").json()}
        assert listed[job_id]["archived"] is False

        # 未点击归档前，任务不在“已归档”。
        archived = client.post(f"/api/jobs/{job_id}/archive")
        assert archived.status_code == 200, archived.text
        assert archived.json()["archived"] is True
        assert archived.json()["web"]["archived_at"]

        listed = {item["id"]: item for item in main.get("/api/jobs").json()}
        assert listed[job_id]["archived"] is True
        stats = main.get("/api/stats").json()
        assert stats["finished"] == 0
        assert stats["archived"] == 1

        # 取消归档：回到“处理完”，视频文件不动。
        reverted = client.delete(f"/api/jobs/{job_id}/archive")
        assert reverted.status_code == 200, reverted.text
        assert reverted.json()["archived"] is False
        assert video.is_file()


def test_job_without_video_cannot_be_archived(env):
    _seed_accounts(env)
    bridge = FastAPI()
    web_api.mount_video_api(
        bridge, db=application.db, get_account=application.get_account,
        get_job=application.get_job, browser=lambda _a: None,
        base_dir=env, downloads_dir=env / "videos", serialize=application.job_row,
    )
    with TestClient(bridge) as client:
        job = client.post("/api/v1/videos/generations", json={
            "prompt": "还在排队", "duration": 4, "submit_mode": "manual",
        }).json()
        blocked = client.post(f"/api/jobs/{job['id']}/archive")
        assert blocked.status_code == 409
        assert "处理完" in blocked.json()["detail"]


def test_batch_archive_each_selected_finished_job(env):
    """批量“归档所选”：逐个调用归档接口，全部应成功并进入已归档。"""
    _seed_accounts(env)
    bridge = FastAPI()
    web_api.mount_video_api(
        bridge, db=application.db, get_account=application.get_account,
        get_job=application.get_job, browser=lambda _a: None,
        base_dir=env, downloads_dir=env / "videos", serialize=application.job_row,
    )
    with TestClient(bridge) as client:
        job_ids = []
        for index in range(3):
            job = client.post("/api/v1/videos/generations", json={
                "prompt": f"批量归档 {index}", "duration": 4,
                "account_id": "account_1", "submit_mode": "manual",
            }).json()
            job_id = job["id"]
            job_ids.append(job_id)
            video = env / "videos" / "library" / f"{job_id}.mp4"
            video.parent.mkdir(parents=True, exist_ok=True)
            video.write_bytes(b"video-bytes")
            with application.db() as conn:
                conn.execute(
                    "UPDATE jobs SET status='downloaded',video_path=? WHERE id=?",
                    (str(video.relative_to(env)), job_id),
                )

        # 模拟前端的批量归档：按勾选顺序逐个调用。
        for job_id in job_ids:
            response = client.post(f"/api/jobs/{job_id}/archive")
            assert response.status_code == 200, response.text
            assert response.json()["archived"] is True

        for job_id in job_ids:
            state = client.get(f"/api/v1/videos/generations/{job_id}").json()
            assert state["archived"] is True
            assert state["web"]["archived_at"]


def test_archive_accepts_video_path_relative_to_downloads_dir(env):
    """老数据里的 video_path 可能相对下载目录，归档不应误判为文件不存在。"""
    _seed_accounts(env)
    bridge = FastAPI()
    web_api.mount_video_api(
        bridge, db=application.db, get_account=application.get_account,
        get_job=application.get_job, browser=lambda _a: None,
        base_dir=env, downloads_dir=env / "videos", serialize=application.job_row,
    )
    with TestClient(bridge) as client:
        job = client.post("/api/v1/videos/generations", json={
            "prompt": "相对下载目录的路径", "duration": 4,
            "account_id": "account_1", "submit_mode": "manual",
        }).json()
        job_id = job["id"]
        video = env / "videos" / "library" / "legacy.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"legacy-video")
        with application.db() as conn:
            conn.execute(
                "UPDATE jobs SET status='downloaded',video_path=? WHERE id=?",
                (str(video.relative_to(env / "videos")), job_id),
            )

        response = client.post(f"/api/jobs/{job_id}/archive")
        assert response.status_code == 200, response.text
        assert response.json()["archived"] is True

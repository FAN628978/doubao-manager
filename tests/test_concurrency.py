import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import web_api


@pytest.fixture(autouse=True)
def _disable_claude_recovery(monkeypatch):
    monkeypatch.setenv("DOUBAO_AGENT_ENABLED", "0")


def test_second_account_starts_in_parallel_without_global_browser_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setenv("DOUBAO_MAX_CONCURRENT_ACCOUNTS", "0")
    dbfile = tmp_path / "concurrency.sqlite"

    def db():
        conn = sqlite3.connect(dbfile)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    with db() as conn:
        conn.executescript("""
            CREATE TABLE accounts(
                id TEXT PRIMARY KEY,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE jobs(
                id TEXT PRIMARY KEY,
                project_id TEXT,
                episode_id TEXT,
                shot_id TEXT,
                prompt TEXT,
                account_id TEXT,
                status TEXT,
                created_at TEXT,
                updated_at TEXT,
                error_message TEXT DEFAULT '',
                video_path TEXT
            );
        """)
        conn.executemany(
            "INSERT INTO accounts(id, archived, created_at) VALUES(?, 0, ?)",
            [(f"account_{index}", f"2026-01-{index:02d}") for index in range(1, 6)],
        )

    def get_account(account_id):
        with db() as conn:
            return conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def get_job(job_id):
        with db() as conn:
            return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    async def run():
        app = FastAPI()
        active = 0
        first_started = asyncio.Event()
        first_waiting = asyncio.Event()
        second_started = asyncio.Event()
        allow_first_wait = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def fake_browser(account_id):
            return account_id

        async def fake_perform(context, req, assets, target, on_status, on_page_url, on_binding,
                               on_manual_ready=None, confirmation_text="", close_on_success=False,
                               on_review_page=None):
            nonlocal active
            active += 1
            calls.append(req.prompt)
            if req.prompt == "first":
                first_started.set()
                await allow_first_wait.wait()
                await on_status("generating", "等待视频生成")
                first_waiting.set()
            else:
                second_started.set()
            await release.wait()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"test-video")
            active -= 1
            return {
                "output": str(target),
                "video_url": "https://example.com/video.mp4",
                "web_page_url": "https://www.doubao.com/chat/test",
            }

        monkeypatch.setattr(web_api, "perform", fake_perform)
        web_api.mount_video_api(
            app,
            db=db,
            get_account=get_account,
            get_job=get_job,
            browser=fake_browser,
            base_dir=tmp_path,
            downloads_dir=tmp_path / "data" / "videos",
            serialize=dict,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            first = await client.post("/api/v1/videos/generations", json={
                "account_id": "account_1",
                "prompt": "first",
                "duration": 4,
                "submit_mode": "auto",
                "auto_start": True,
            })
            assert first.status_code == 202
            await asyncio.wait_for(first_started.wait(), timeout=1)

            second = await client.post("/api/v1/videos/generations", json={
                "account_id": "account_2",
                "prompt": "second",
                "duration": 4,
                "submit_mode": "auto",
                "auto_start": True,
            })
            assert second.status_code == 202
            # 不再有全局浏览器串行锁：不同账号可以同时准备和提交，无需等
            # 第一个任务进入“等待视频生成”。
            await asyncio.wait_for(second_started.wait(), timeout=1)
            assert active == 2
            assert calls == ["first", "second"]

            allow_first_wait.set()
            await asyncio.wait_for(first_waiting.wait(), timeout=1)
            release.set()

            job_ids = [first.json()["id"], second.json()["id"]]
            for _ in range(50):
                states = [
                    (await client.get(f"/api/v1/videos/generations/{job_id}")).json()["status"]
                    for job_id in job_ids
                ]
                if states == ["downloaded", "downloaded"]:
                    break
                await asyncio.sleep(0.01)
            assert states == ["downloaded", "downloaded"]

    asyncio.run(run())


def test_same_account_jobs_wait_until_previous_task_releases(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setenv("DOUBAO_MAX_CONCURRENT_ACCOUNTS", "0")
    monkeypatch.setenv("DOUBAO_BROWSER_IDLE_CLOSE_SECONDS", "0")
    dbfile = tmp_path / "same-account.sqlite"

    def db():
        conn = sqlite3.connect(dbfile)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    with db() as conn:
        conn.executescript("""
            CREATE TABLE accounts(id TEXT PRIMARY KEY, archived INTEGER NOT NULL DEFAULT 0,
                                  created_at TEXT NOT NULL);
            CREATE TABLE jobs(id TEXT PRIMARY KEY, project_id TEXT, episode_id TEXT,
                              shot_id TEXT, prompt TEXT, account_id TEXT, status TEXT,
                              created_at TEXT, updated_at TEXT, error_message TEXT DEFAULT '',
                              video_path TEXT);
        """)
        conn.execute("INSERT INTO accounts(id,archived,created_at) VALUES('account_1',0,'2026-01-01')")

    def get_account(account_id):
        with db() as conn:
            return conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def get_job(job_id):
        with db() as conn:
            return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    async def run():
        app = FastAPI()
        active = 0
        maximum_active = 0
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release_first = asyncio.Event()
        release_second = asyncio.Event()
        calls = []
        browser_calls = []
        close_calls = []
        bound_pages = {}

        class FakePage:
            def __init__(self):
                self.closed = False

            def is_closed(self):
                return self.closed

            async def close(self):
                self.closed = True

        async def fake_browser(account_id):
            browser_calls.append(account_id)
            return account_id

        async def fake_close_browser(account_id):
            close_calls.append(account_id)

        async def fake_perform(context, req, assets, target, on_status, on_page_url, on_binding,
                               on_manual_ready=None, confirmation_text="", close_on_success=False,
                               on_review_page=None):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            calls.append(req.prompt)
            page = FakePage()
            bound_pages[req.prompt] = page
            await on_review_page(page)
            if req.prompt.endswith("0"):
                first_started.set()
                await on_status("generating", "等待视频生成")
                await release_first.wait()
            else:
                second_started.set()
                await release_second.wait()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"serial-video")
            active -= 1
            return {"output": str(target), "video_url": "https://example.com/video.mp4",
                    "web_page_url": "https://www.doubao.com/chat/test"}

        monkeypatch.setattr(web_api, "perform", fake_perform)
        web_api.mount_video_api(
            app, db=db, get_account=get_account, get_job=get_job, browser=fake_browser,
            base_dir=tmp_path, downloads_dir=tmp_path / "data" / "videos", serialize=dict,
            close_browser=fake_close_browser,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            created = [await client.post("/api/v1/videos/generations", json={
                "account_id": "account_1", "prompt": f"并行任务{index}",
                "duration": 4, "submit_mode": "auto", "auto_start": True,
            }) for index in range(2)]
            assert all(response.status_code == 202 for response in created)
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await asyncio.sleep(0.05)
            assert not second_started.is_set()
            assert maximum_active == 1
            assert calls == ["并行任务0"]
            job_ids = [response.json()["id"] for response in created]
            assert app.state.video_prepared_pages[job_ids[0]] is bound_pages["并行任务0"]
            release_first.set()

            for _ in range(100):
                states = [(await client.get(f"/api/v1/videos/generations/{job_id}")).json()["status"]
                          for job_id in job_ids]
                if states[0] == "downloaded":
                    break
                await asyncio.sleep(0.01)
            assert states[0] == "downloaded"
            await asyncio.wait_for(second_started.wait(), timeout=1)
            assert calls == ["并行任务0", "并行任务1"]
            assert app.state.video_prepared_pages[job_ids[1]] is bound_pages["并行任务1"]
            assert bound_pages["并行任务0"] is not bound_pages["并行任务1"]
            for _ in range(50):
                if bound_pages["并行任务0"].closed:
                    break
                await asyncio.sleep(0.01)
            assert bound_pages["并行任务0"].closed is True
            assert bound_pages["并行任务1"].closed is False
            assert close_calls == []

            release_second.set()
            for _ in range(100):
                states = [(await client.get(f"/api/v1/videos/generations/{job_id}")).json()["status"]
                          for job_id in job_ids]
                if states == ["downloaded", "downloaded"]:
                    break
                await asyncio.sleep(0.01)
            assert states == ["downloaded", "downloaded"]
            assert maximum_active == 1
            assert browser_calls == ["account_1", "account_1"]
            for _ in range(50):
                if close_calls:
                    break
                await asyncio.sleep(0.01)
            assert close_calls == ["account_1"]

    asyncio.run(run())


def test_processing_limit_updates_immediately_and_waiting_jobs_stay_queued(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.delenv("DOUBAO_MAX_CONCURRENT_ACCOUNTS", raising=False)
    dbfile = tmp_path / "processing-limit.sqlite"

    def db():
        conn = sqlite3.connect(dbfile)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    with db() as conn:
        conn.executescript("""
            CREATE TABLE accounts(
                id TEXT PRIMARY KEY,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE jobs(
                id TEXT PRIMARY KEY,
                project_id TEXT,
                episode_id TEXT,
                shot_id TEXT,
                prompt TEXT,
                account_id TEXT,
                status TEXT,
                created_at TEXT,
                updated_at TEXT,
                error_message TEXT DEFAULT '',
                video_path TEXT
            );
        """)
        conn.executemany(
            "INSERT INTO accounts(id, archived, created_at) VALUES(?, 0, ?)",
            [(f"account_{index}", f"2026-01-{index:02d}") for index in range(1, 6)],
        )

    def get_account(account_id):
        with db() as conn:
            return conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def get_job(job_id):
        with db() as conn:
            return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    async def run():
        app = FastAPI()
        active = 0
        maximum_active = 0
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        allow_first_wait = asyncio.Event()
        release = asyncio.Event()

        async def fake_browser(account_id):
            return account_id

        async def fake_perform(context, req, assets, target, on_status, on_page_url, on_binding,
                               on_manual_ready=None, confirmation_text="", close_on_success=False,
                               on_review_page=None):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            if req.prompt == "limit task 1":
                first_started.set()
                await allow_first_wait.wait()
                await on_status("generating", "等待视频生成")
            else:
                second_started.set()
            await release.wait()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"limited-video")
            active -= 1
            return {
                "output": str(target),
                "video_url": "https://example.com/video.mp4",
                "web_page_url": "https://www.doubao.com/chat/test",
            }

        monkeypatch.setattr(web_api, "perform", fake_perform)
        web_api.mount_video_api(
            app,
            db=db,
            get_account=get_account,
            get_job=get_job,
            browser=fake_browser,
            base_dir=tmp_path,
            downloads_dir=tmp_path / "data" / "videos",
            serialize=dict,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            assert (await client.get("/api/settings/processing-limit")).json() == {
                "processing_limit": 3,
                "active_processing": 0,
            }
            limited = await client.put(
                "/api/settings/processing-limit", json={"processing_limit": 1}
            )
            assert limited.status_code == 200

            created = [await client.post("/api/v1/videos/generations", json={
                "account_id": f"account_{index}",
                "prompt": f"limit task {index}",
                "duration": 4,
                "submit_mode": "auto",
                "auto_start": True,
            }) for index in range(1, 6)]
            assert all(response.status_code == 202 for response in created)
            job_ids = [response.json()["id"] for response in created]

            await asyncio.wait_for(first_started.wait(), timeout=1)
            await asyncio.sleep(0.05)
            states = [
                (await client.get(f"/api/v1/videos/generations/{job_id}")).json()["status"]
                for job_id in job_ids
            ]
            assert states.count("opening") == 1
            assert states.count("queued") == 4
            assert maximum_active == 1

            expanded = await client.put(
                "/api/settings/processing-limit", json={"processing_limit": 2}
            )
            assert expanded.status_code == 200
            assert expanded.json()["processing_limit"] == 2
            for _ in range(50):
                limit_state = (
                    await client.get("/api/settings/processing-limit")
                ).json()
                if limit_state["active_processing"] == 2:
                    break
                await asyncio.sleep(0.01)
            assert limit_state["active_processing"] == 2

            # 扩容后第二个任务立刻开始准备；不同账号的浏览器工作并行。
            await asyncio.wait_for(second_started.wait(), timeout=1)
            states = [
                (await client.get(f"/api/v1/videos/generations/{job_id}")).json()["status"]
                for job_id in job_ids
            ]
            assert states.count("opening") == 2
            assert states.count("queued") == 3
            assert maximum_active == 2

            allow_first_wait.set()
            await asyncio.sleep(0.05)
            states = [
                (await client.get(f"/api/v1/videos/generations/{job_id}")).json()["status"]
                for job_id in job_ids
            ]
            assert states.count("generating") == 1
            assert states.count("opening") == 1
            assert states.count("queued") == 3

            release.set()
            for _ in range(100):
                states = [
                    (await client.get(f"/api/v1/videos/generations/{job_id}")).json()["status"]
                    for job_id in job_ids
                ]
                if states == ["downloaded"] * 5:
                    break
                await asyncio.sleep(0.01)
            assert states == ["downloaded"] * 5
            assert maximum_active == 2

    asyncio.run(run())


def test_needs_review_releases_processing_slot_and_next_job_starts(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    dbfile = tmp_path / "processing-lifetime.sqlite"

    def db():
        conn = sqlite3.connect(dbfile)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    with db() as conn:
        conn.executescript("""
            CREATE TABLE accounts(
                id TEXT PRIMARY KEY,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE jobs(
                id TEXT PRIMARY KEY,
                project_id TEXT,
                episode_id TEXT,
                shot_id TEXT,
                prompt TEXT,
                account_id TEXT,
                status TEXT,
                created_at TEXT,
                updated_at TEXT,
                error_message TEXT DEFAULT '',
                video_path TEXT
            );
        """)
        conn.executemany(
            "INSERT INTO accounts(id, archived, created_at) VALUES(?, 0, ?)",
            [(f"account_{index}", f"2026-02-{index:02d}") for index in range(1, 3)],
        )

    def get_account(account_id):
        with db() as conn:
            return conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def get_job(job_id):
        with db() as conn:
            return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    async def run():
        app = FastAPI()
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        calls = []

        async def fake_browser(account_id):
            return account_id

        async def fake_perform(context, req, assets, target, on_status, on_page_url, on_binding,
                               on_manual_ready=None, confirmation_text="", close_on_success=False,
                               on_review_page=None):
            calls.append(req.prompt)
            if req.prompt == "first":
                first_started.set()
                raise web_api.ReviewNeeded("LOGIN_REQUIRED", "需要人工登录")
            second_started.set()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"second-video")
            return {
                "output": str(target),
                "video_url": "https://example.com/second.mp4",
                "web_page_url": "https://www.doubao.com/chat/second",
            }

        monkeypatch.setattr(web_api, "perform", fake_perform)
        web_api.mount_video_api(
            app,
            db=db,
            get_account=get_account,
            get_job=get_job,
            browser=fake_browser,
            base_dir=tmp_path,
            downloads_dir=tmp_path / "data" / "videos",
            serialize=dict,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            await client.put(
                "/api/settings/processing-limit", json={"processing_limit": 1}
            )
            first = await client.post("/api/v1/videos/generations", json={
                "account_id": "account_1",
                "prompt": "first",
                "duration": 4,
                "submit_mode": "manual",
                "auto_start": True,
            })
            assert first.status_code == 202
            first_id = first.json()["id"]

            # Manual tasks intentionally ignore auto_start; an operator must launch
            # the first task explicitly before it can occupy a processing slot.
            started = await client.post(
                f"/api/v1/videos/generations/{first_id}/start"
            )
            assert started.status_code == 200

            await asyncio.wait_for(first_started.wait(), timeout=1)
            for _ in range(100):
                if (await client.get(
                    f"/api/v1/videos/generations/{first_id}"
                )).json()["status"] == "needs_review":
                    break
                await asyncio.sleep(0.01)
            assert (await client.get(
                f"/api/v1/videos/generations/{first_id}"
            )).json()["status"] == "needs_review"

            second = await client.post("/api/v1/videos/generations", json={
                "account_id": "account_2",
                "prompt": "second",
                "duration": 4,
                "submit_mode": "auto",
                "auto_start": True,
            })
            assert second.status_code == 202
            second_id = second.json()["id"]

            # A human-review hand-off must not consume the machine slot.
            await asyncio.wait_for(second_started.wait(), timeout=1)
            for _ in range(100):
                if (await client.get(
                    f"/api/v1/videos/generations/{second_id}"
                )).json()["status"] == "downloaded":
                    break
                await asyncio.sleep(0.01)
            assert (await client.get(
                f"/api/v1/videos/generations/{second_id}"
            )).json()["status"] == "downloaded"
            assert calls == ["first", "second"]

            # After both hand-offs finish, the slot counter must be back to zero.
            for _ in range(100):
                limit_state = (
                    await client.get("/api/settings/processing-limit")
                ).json()
                if limit_state["active_processing"] == 0:
                    break
                await asyncio.sleep(0.01)
            assert limit_state["active_processing"] == 0

            # The first task still awaits a human; it just no longer blocks the queue.
            assert (await client.get(
                f"/api/v1/videos/generations/{first_id}"
            )).json()["status"] == "needs_review"
            marked = await client.post(
                f"/api/v1/videos/generations/{first_id}/mark-failed?confirm_failed=true"
            )
            assert marked.status_code == 200

    asyncio.run(run())

# ---------------------------------------------------------------------------
# 调度锁与账号选择的回归用例
# ---------------------------------------------------------------------------

def _make_scheduling_db(tmp_path, names):
    dbfile = tmp_path / "scheduling.sqlite"

    def db():
        conn = sqlite3.connect(dbfile)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    with db() as conn:
        conn.executescript("""
            CREATE TABLE accounts(id TEXT PRIMARY KEY, archived INTEGER NOT NULL DEFAULT 0,
                                  created_at TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
                                  profile_dir TEXT NOT NULL DEFAULT '');
            CREATE TABLE jobs(id TEXT PRIMARY KEY, project_id TEXT, episode_id TEXT,
                              shot_id TEXT, prompt TEXT, account_id TEXT, status TEXT,
                              created_at TEXT, updated_at TEXT, error_message TEXT DEFAULT '',
                              video_path TEXT);
            CREATE TABLE account_quota(account_id TEXT NOT NULL, day TEXT NOT NULL,
                                       reserved_seconds INTEGER NOT NULL DEFAULT 0,
                                       consumed_seconds INTEGER NOT NULL DEFAULT 0,
                                       manual_exhausted INTEGER NOT NULL DEFAULT 0,
                                       PRIMARY KEY(account_id, day));
        """)
        conn.executemany(
            "INSERT INTO accounts(id, archived, created_at, name, profile_dir) "
            "VALUES(?,0,?,?,?)",
            [(name, f"2026-01-{index + 1:02d}", name, "data/profiles/" + name)
             for index, name in enumerate(names)],
        )

    def get_account(account_id):
        with db() as conn:
            return conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def get_job(job_id):
        with db() as conn:
            return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    return db, get_account, get_job


def test_same_account_does_not_deadlock_execution_and_account_lanes(tmp_path, monkeypatch):
    """任务 A 等账号、任务 B 等执行名额时，不得互相等待。

    旧实现先拿全局执行名额再等账号锁：A 持有执行名额等 B 的账号，B 生成结束
    需要执行名额去下载，两边永久互等，界面看起来就是“卡着不动”。
    """
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setenv("DOUBAO_MAX_CONCURRENT_ACCOUNTS", "0")
    db, get_account, get_job = _make_scheduling_db(tmp_path, ["account_1"])

    async def run():
        app = FastAPI()
        first_generating = asyncio.Event()
        allow_first_download = asyncio.Event()
        calls = []

        async def fake_browser(account_id):
            return account_id

        async def fake_perform(context, req, assets, target, on_status, on_page_url, on_binding,
                               on_manual_ready=None, confirmation_text="", close_on_success=False,
                               on_review_page=None):
            calls.append(req.prompt)
            if req.prompt == "first":
                await on_status("generating", "等待视频生成")
                first_generating.set()
                await allow_first_download.wait()
                # 从“等待视频”回到浏览器操作，需要重新拿执行名额。
                await on_status("downloading", "开始下载")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"video")
            return {
                "output": str(target),
                "video_url": "https://example.com/video.mp4",
                "web_page_url": "https://www.doubao.com/chat/test",
            }

        monkeypatch.setattr(web_api, "perform", fake_perform)
        web_api.mount_video_api(
            app,
            db=db,
            get_account=get_account,
            get_job=get_job,
            browser=fake_browser,
            base_dir=tmp_path,
            downloads_dir=tmp_path / "data" / "videos",
            serialize=dict,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            first = (await client.post("/api/v1/videos/generations", json={
                "account_id": "account_1", "prompt": "first", "duration": 4,
                "submit_mode": "auto", "auto_start": True,
            })).json()
            await asyncio.wait_for(first_generating.wait(), timeout=2)

            second = (await client.post("/api/v1/videos/generations", json={
                "account_id": "account_1", "prompt": "second", "duration": 4,
                "submit_mode": "auto", "auto_start": True,
            })).json()
            # 让第二个 worker 走到“等账号”这一步，再放第一个去下载。
            await asyncio.sleep(0.1)
            allow_first_download.set()

            states = []
            for _ in range(300):
                states = [
                    (await client.get(
                        f"/api/v1/videos/generations/{job_id}")).json()["status"]
                    for job_id in (first["id"], second["id"])
                ]
                if states == ["downloaded", "downloaded"]:
                    break
                await asyncio.sleep(0.01)
            assert states == ["downloaded", "downloaded"]
            assert calls == ["first", "second"]

    asyncio.run(asyncio.wait_for(run(), timeout=5))


def test_auto_select_prefers_idle_account_over_best_fit_busy_account(tmp_path, monkeypatch):
    """自动选号先看是否空闲，再比剩余额度，不能把任务塞给正在生成的任务。"""
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setenv("DOUBAO_MAX_CONCURRENT_ACCOUNTS", "0")
    db, get_account, get_job = _make_scheduling_db(tmp_path, ["account_1", "account_2"])
    with db() as conn:
        # account_1 更适合 best-fit（剩 10 秒），但正在跑任务；
        # account_2 剩 20 秒，空闲。
        conn.execute(
            "INSERT INTO account_quota VALUES('account_1',?,0,20,0)",
            (web_api.local_day(),),
        )
        conn.execute(
            "INSERT INTO account_quota VALUES('account_2',?,0,10,0)",
            (web_api.local_day(),),
        )

    async def run():
        app = FastAPI()
        first_generating = asyncio.Event()
        release = asyncio.Event()

        async def fake_browser(account_id):
            return account_id

        async def fake_perform(context, req, assets, target, on_status, on_page_url, on_binding,
                               on_manual_ready=None, confirmation_text="", close_on_success=False,
                               on_review_page=None):
            if req.prompt == "first":
                await on_status("generating", "等待视频生成")
                first_generating.set()
                await release.wait()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"video")
            return {
                "output": str(target),
                "video_url": "https://example.com/video.mp4",
                "web_page_url": "https://www.doubao.com/chat/test",
            }

        monkeypatch.setattr(web_api, "perform", fake_perform)
        web_api.mount_video_api(
            app,
            db=db,
            get_account=get_account,
            get_job=get_job,
            browser=fake_browser,
            base_dir=tmp_path,
            downloads_dir=tmp_path / "data" / "videos",
            serialize=dict,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            first = (await client.post("/api/v1/videos/generations", json={
                "account_id": "account_1", "prompt": "first", "duration": 4,
                "submit_mode": "auto", "auto_start": True,
            })).json()
            await asyncio.wait_for(first_generating.wait(), timeout=2)

            second = (await client.post("/api/v1/videos/generations", json={
                "prompt": "second", "duration": 4,
                "submit_mode": "auto", "auto_start": True,
            })).json()
            state = {}
            for _ in range(200):
                state = (await client.get(
                    f"/api/v1/videos/generations/{second['id']}")).json()
                if state["account_id"]:
                    break
                await asyncio.sleep(0.01)
            assert state["account_id"] == "account_2", state
            release.set()
            for _ in range(200):
                if (await client.get(
                    f"/api/v1/videos/generations/{second['id']}")).json()["status"] == "downloaded":
                    break
                await asyncio.sleep(0.01)

    asyncio.run(asyncio.wait_for(run(), timeout=5))

def test_needs_review_keeps_account_until_operator_resolves_it(tmp_path, monkeypatch):
    """异常待人工仍占用账号；明确结束后同账号的下一任务才能运行。"""
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setenv("DOUBAO_MAX_CONCURRENT_ACCOUNTS", "0")
    monkeypatch.setenv("DOUBAO_BROWSER_IDLE_CLOSE_SECONDS", "3600")
    db, get_account, get_job = _make_scheduling_db(tmp_path, ["account_1"])

    async def run():
        app = FastAPI()
        second_started = asyncio.Event()
        calls = []

        async def fake_browser(account_id):
            return account_id

        async def fake_perform(context, req, assets, target, on_status, on_page_url, on_binding,
                               on_manual_ready=None, confirmation_text="", close_on_success=False,
                               on_review_page=None):
            calls.append(req.prompt)
            if req.prompt == "first":
                raise web_api.ReviewNeeded("LOGIN_REQUIRED", "需要人工登录")
            second_started.set()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"video")
            return {
                "output": str(target),
                "video_url": "https://example.com/video.mp4",
                "web_page_url": "https://www.doubao.com/chat/test",
            }

        monkeypatch.setattr(web_api, "perform", fake_perform)
        web_api.mount_video_api(
            app,
            db=db,
            get_account=get_account,
            get_job=get_job,
            browser=fake_browser,
            base_dir=tmp_path,
            downloads_dir=tmp_path / "data" / "videos",
            serialize=dict,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            first = (await client.post("/api/v1/videos/generations", json={
                "account_id": "account_1", "prompt": "first", "duration": 4,
                "submit_mode": "auto", "auto_start": True,
            })).json()
            for _ in range(200):
                state = (await client.get(
                    f"/api/v1/videos/generations/{first['id']}")).json()
                if state["status"] == "needs_review":
                    break
                await asyncio.sleep(0.01)
            assert state["status"] == "needs_review"
            assert calls == ["first"]

            second = (await client.post("/api/v1/videos/generations", json={
                "account_id": "account_1", "prompt": "second", "duration": 4,
                "submit_mode": "auto", "auto_start": True,
            })).json()
            await asyncio.sleep(0.1)
            second_state = (await client.get(
                f"/api/v1/videos/generations/{second['id']}"
            )).json()
            assert second_state["status"] == "queued"
            assert not second_started.is_set()

            # 操作者重置异常任务后才释放旧占用，等待任务随后自动继续。
            reset = await client.post(
                f"/api/v1/videos/generations/{first['id']}/reset?confirm_reset=true"
            )
            assert reset.status_code == 200, reset.text
            await asyncio.wait_for(second_started.wait(), timeout=2)
            for _ in range(200):
                state = (await client.get(
                    f"/api/v1/videos/generations/{second['id']}")).json()
                if state["status"] == "downloaded":
                    break
                await asyncio.sleep(0.01)
            assert state["status"] == "downloaded"

    asyncio.run(asyncio.wait_for(run(), timeout=6))


def test_redispatch_tick_revives_queued_auto_jobs(tmp_path, monkeypatch):
    """dispatched_at 已置位但没有 worker 的任务，必须被后台 tick 自动重新派发。"""
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setenv("DOUBAO_MAX_CONCURRENT_ACCOUNTS", "0")
    monkeypatch.setenv("DOUBAO_BROWSER_IDLE_CLOSE_SECONDS", "3600")
    db, get_account, get_job = _make_scheduling_db(tmp_path, ["account_1"])

    async def run():
        app = FastAPI()
        started = asyncio.Event()

        async def fake_browser(account_id):
            return account_id

        async def fake_perform(context, req, assets, target, on_status, on_page_url, on_binding,
                               on_manual_ready=None, confirmation_text="", close_on_success=False,
                               on_review_page=None):
            started.set()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"video")
            return {
                "output": str(target),
                "video_url": "https://example.com/video.mp4",
                "web_page_url": "https://www.doubao.com/chat/test",
            }

        monkeypatch.setattr(web_api, "perform", fake_perform)
        web_api.mount_video_api(
            app,
            db=db,
            get_account=get_account,
            get_job=get_job,
            browser=fake_browser,
            base_dir=tmp_path,
            downloads_dir=tmp_path / "data" / "videos",
            serialize=dict,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            created = (await client.post("/api/v1/videos/generations", json={
                "account_id": "account_1", "prompt": "orphan", "duration": 4,
                "submit_mode": "auto", "auto_start": False,
            })).json()
            job_id = created["id"]
            with db() as conn:
                conn.execute(
                    "UPDATE video_web_jobs SET dispatched_at=? WHERE job_id=?",
                    (web_api.ts(), job_id),
                )
            assert app.state.video_redispatch() == 1
            await asyncio.wait_for(started.wait(), timeout=2)

            # next_retry_at 未到期的任务不会被重复派发。
            with db() as conn:
                conn.execute(
                    "UPDATE video_web_jobs SET next_retry_at=? WHERE job_id=?",
                    ((datetime.now(timezone.utc) + timedelta(minutes=5))
                     .isoformat(timespec="seconds"), job_id),
                )
            assert app.state.video_redispatch() == 0

    asyncio.run(asyncio.wait_for(run(), timeout=6))


def test_stall_watchdog_interrupts_a_frozen_worker_and_releases_resources(tmp_path, monkeypatch):
    """阶段长时间无进展的 worker 必须被切断，不能永远占着账号和处理名额。"""
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.setenv("DOUBAO_MAX_CONCURRENT_ACCOUNTS", "0")
    monkeypatch.setenv("DOUBAO_BROWSER_IDLE_CLOSE_SECONDS", "3600")
    monkeypatch.setenv("DOUBAO_STALL_PREPARE_SECONDS", "1")
    db, get_account, get_job = _make_scheduling_db(tmp_path, ["account_1"])

    async def run():
        app = FastAPI()
        frozen_started = asyncio.Event()
        frozen = asyncio.Event()
        next_started = asyncio.Event()

        async def fake_browser(account_id):
            return account_id

        async def fake_perform(context, req, assets, target, on_status, on_page_url, on_binding,
                               on_manual_ready=None, confirmation_text="", close_on_success=False,
                               on_review_page=None):
            if req.prompt == "frozen":
                await on_status("opening", "卡住不动")
                frozen_started.set()
                await frozen.wait()  # 模拟永远不会返回的浏览器操作
            next_started.set()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"video")
            return {
                "output": str(target),
                "video_url": "https://example.com/video.mp4",
                "web_page_url": "https://www.doubao.com/chat/test",
            }

        monkeypatch.setattr(web_api, "perform", fake_perform)
        web_api.mount_video_api(
            app,
            db=db,
            get_account=get_account,
            get_job=get_job,
            browser=fake_browser,
            base_dir=tmp_path,
            downloads_dir=tmp_path / "data" / "videos",
            serialize=dict,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            created = (await client.post("/api/v1/videos/generations", json={
                "account_id": "account_1", "prompt": "frozen", "duration": 4,
                "submit_mode": "auto", "auto_start": True,
            })).json()
            await asyncio.wait_for(frozen_started.wait(), timeout=2)
            with db() as conn:
                conn.execute(
                    "UPDATE video_web_jobs SET phase_started_at=? WHERE job_id=?",
                    ((datetime.now(timezone.utc) - timedelta(minutes=10))
                     .isoformat(timespec="seconds"), created["id"]),
                )
            assert await app.state.video_stall_watchdog_once() == 1
            await asyncio.sleep(0.1)
            state = (await client.get(
                f"/api/v1/videos/generations/{created['id']}")).json()
            assert state["status"] == "needs_review"

            # 异常待人工仍占用账号；下一个任务必须留在待处理。
            next_job = (await client.post("/api/v1/videos/generations", json={
                "account_id": "account_1", "prompt": "next", "duration": 4,
                "submit_mode": "auto", "auto_start": True,
            })).json()
            await asyncio.sleep(0.1)
            waiting = (await client.get(
                f"/api/v1/videos/generations/{next_job['id']}"
            )).json()
            assert waiting["status"] == "queued"
            assert not next_started.is_set()

            reset = await client.post(
                f"/api/v1/videos/generations/{created['id']}/reset?confirm_reset=true"
            )
            assert reset.status_code == 200, reset.text
            await asyncio.wait_for(next_started.wait(), timeout=2)

    asyncio.run(asyncio.wait_for(run(), timeout=8))

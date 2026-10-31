import asyncio
import sqlite3
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import web_api


def test_different_accounts_run_concurrently(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
    monkeypatch.delenv("DOUBAO_MAX_CONCURRENT_ACCOUNTS", raising=False)
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
        active_accounts = set()
        maximum_active = 0
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def fake_browser(account_id):
            return account_id

        async def fake_perform(context, req, assets, target, on_status, on_page_url, on_binding,
                               on_manual_ready=None, confirmation_text="", close_on_success=False,
                               on_review_page=None):
            nonlocal maximum_active
            active_accounts.add(context)
            maximum_active = max(maximum_active, len(active_accounts))
            if len(active_accounts) == 5:
                both_started.set()
            await release.wait()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"test-video")
            active_accounts.remove(context)
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
            created = [await client.post("/api/v1/videos/generations", json={
                "prompt": f"任务{index}", "duration": 4, "auto_start": True,
            }) for index in range(1, 6)]
            assert all(response.status_code == 202 for response in created)
            assert {response.json()["account_id"] for response in created} == {
                f"account_{index}" for index in range(1, 6)
            }

            await asyncio.wait_for(both_started.wait(), timeout=1)
            assert maximum_active == 5
            release.set()

            job_ids = [response.json()["id"] for response in created]
            for _ in range(50):
                states = [
                    (await client.get(f"/api/v1/videos/generations/{job_id}")).json()["status"]
                    for job_id in job_ids
                ]
                if states == ["downloaded", "downloaded"]:
                    break
                await asyncio.sleep(0.01)
            assert states == ["downloaded"] * 5

    asyncio.run(run())


def test_same_account_runs_strictly_serial(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_TEST_MODE", "1")
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
        release = asyncio.Event()
        browser_calls = []
        close_calls = []

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
            first_started.set()
            await release.wait()
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
                "account_id": "account_1", "prompt": f"串行任务{index}",
                "duration": 4, "auto_start": True,
            }) for index in range(2)]
            assert all(response.status_code == 202 for response in created)
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await asyncio.sleep(0.05)
            assert maximum_active == 1
            release.set()

            job_ids = [response.json()["id"] for response in created]
            for _ in range(100):
                states = [(await client.get(f"/api/v1/videos/generations/{job_id}")).json()["status"]
                          for job_id in job_ids]
                if states == ["downloaded", "downloaded"]:
                    break
                await asyncio.sleep(0.01)
            assert states == ["downloaded", "downloaded"]
            assert maximum_active == 1
            assert browser_calls == ["account_1", "account_1"]
            assert close_calls == ["account_1", "account_1"]

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
        release = asyncio.Event()

        async def fake_browser(account_id):
            return account_id

        async def fake_perform(context, req, assets, target, on_status, on_page_url, on_binding,
                               on_manual_ready=None, confirmation_text="", close_on_success=False,
                               on_review_page=None):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            if active >= 1:
                first_started.set()
            if active >= 2:
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
                "processing_limit": 0,
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
            await asyncio.wait_for(second_started.wait(), timeout=1)
            await asyncio.sleep(0.05)
            states = [
                (await client.get(f"/api/v1/videos/generations/{job_id}")).json()["status"]
                for job_id in job_ids
            ]
            assert states.count("opening") == 2
            assert states.count("queued") == 3
            assert maximum_active == 2

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

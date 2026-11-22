import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import async_playwright

import app as application
from doubao_manager.account_names import read_doubao_account_name
from test_v3 import launch_chromium


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "BASE_DIR", tmp_path)
    monkeypatch.setattr(application, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "accounts.sqlite3")
    monkeypatch.setattr(application, "open_contexts", {})
    application.init_db()
    return TestClient(application.app)


def test_account_name_defaults_and_manual_override(client, monkeypatch):
    created = client.post("/api/accounts", json={}).json()
    account_id = created["id"]
    assert created["follow_doubao_name"] is True
    assert created["name"] == "doubao01"
    page = SimpleNamespace(url="https://www.doubao.com/chat/123")

    async def evaluate(_script):
        return "豆包用户名"

    page.evaluate = evaluate
    context = SimpleNamespace(pages=[page])
    application.open_contexts[account_id] = context
    asyncio.run(application.sync_account_name(account_id, context))
    assert client.get("/api/accounts").json()[0]["name"] == "豆包用户名"
    # 旧客户端只更新会员/优先级时，不能重置跟随设置。
    assert client.patch(f"/api/accounts/{account_id}", json={
        "is_member": True, "priority": 10,
    }).json()["follow_doubao_name"] is True
    manual = client.patch(f"/api/accounts/{account_id}", json={
        "is_member": True, "priority": 10,
        "follow_doubao_name": False, "name": "  人工备注名  ",
    }).json()
    assert manual["name"] == "人工备注名"
    assert manual["follow_doubao_name"] is False
    asyncio.run(application.sync_account_name(account_id, context))
    assert application.get_account(account_id)["name"] == "人工备注名"
    resumed = client.patch(f"/api/accounts/{account_id}", json={
        "is_member": True, "priority": 10, "follow_doubao_name": True,
    }).json()
    assert resumed["name"] == "豆包用户名"
    custom = client.post("/api/accounts", json={
        "name": "自定义账号", "follow_doubao_name": False,
    }).json()
    assert custom["name"] == "自定义账号"
    assert custom["follow_doubao_name"] is False


@pytest.mark.parametrize("change", ["empty", "navigate", "close", "disable", "error"])
def test_unavailable_or_stale_name_preserves_account(client, change):
    created = client.post("/api/accounts", json={"name": "原名"}).json()
    account_id = created["id"]
    page = SimpleNamespace(url="https://www.doubao.com/chat/123")

    async def evaluate(_script):
        if change == "navigate":
            page.url = "https://www.doubao.com/chat/456"
        elif change == "close":
            application.open_contexts.pop(account_id)
        elif change == "disable":
            with application.db() as conn:
                conn.execute("UPDATE accounts SET follow_doubao_name=0 WHERE id=?", (account_id,))
        elif change == "error":
            raise RuntimeError("page closed")
        return "" if change == "empty" else "过期名称"

    page.evaluate = evaluate
    context = SimpleNamespace(pages=[page])
    application.open_contexts[account_id] = context
    asyncio.run(application.sync_account_name(account_id, context))
    assert application.get_account(account_id)["name"] == "原名"


def test_existing_accounts_enable_name_following_on_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "legacy.sqlite3")
    with sqlite3.connect(application.DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE accounts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, note TEXT DEFAULT '',
                profile_dir TEXT NOT NULL, archived INTEGER DEFAULT 0,
                created_at TEXT NOT NULL, last_opened_at TEXT
            );
            INSERT INTO accounts(id,name,profile_dir,created_at)
            VALUES('legacy','原账号','data/profiles/legacy','2026-01-01');
        """)
    application.init_db()
    application.init_db()
    row = application.get_account("legacy")
    assert row["name"] == "原账号"
    assert row["follow_doubao_name"] == 1


def test_background_sync_follows_open_account_and_stops_on_shutdown(client):
    account_id = client.post("/api/accounts", json={}).json()["id"]
    display = {"name": "首次登录名"}

    async def evaluate(_script):
        return display["name"]

    async def close():
        pass

    context = SimpleNamespace(
        pages=[SimpleNamespace(url="https://www.doubao.com/chat/123", evaluate=evaluate)],
        close=close,
    )
    application.open_contexts[account_id] = context

    def wait_for_name(running_client, expected):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            accounts = running_client.get("/api/accounts").json()
            if accounts[0]["name"] == expected:
                return
            time.sleep(0.05)
        assert accounts[0]["name"] == expected

    with TestClient(application.app) as running_client:
        wait_for_name(running_client, "首次登录名")
        display["name"] = "修改后的豆包名"
        wait_for_name(running_client, "修改后的豆包名")
    assert not application.open_contexts


def test_read_name_from_doubao_header_in_browser():
    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await browser.new_page()
                await page.route("**/*", lambda route: route.fulfill(
                    content_type="text/html", body="<html><body></body></html>",
                ))
                await page.goto("https://www.doubao.com/chat/test")
                await page.set_content('''<div>
                    <img data-testid="chat_header_avatar_button">
                    <div class="flex-col"><div><span>  Haitao\n豆包  </span></div></div>
                </div>''')
                assert await read_doubao_account_name(page) == "Haitao 豆包"
                await page.evaluate("""() => {
                    window._ROUTER_DATA = {loaderData: {chat_layout: {chat_layout: {
                        accountInfo: {data: {is_visitor_account: true}}
                    }}}};
                }""")
                assert await read_doubao_account_name(page) == ""
                await page.evaluate("() => { delete window._ROUTER_DATA; }")
                await page.locator('div.flex-col span').evaluate("el => el.textContent = '名'.repeat(100)")
                assert await read_doubao_account_name(page) == "名" * 80
                await page.locator('img').evaluate("el => el.remove()")
                assert await read_doubao_account_name(page) == ""
                await page.goto("https://www.doubao.com.example.test/")
                await page.set_content('''<div><img data-testid="chat_header_avatar_button">
                    <div class="flex-col"><div><span>其他网站</span></div></div></div>''')
                assert await read_doubao_account_name(page) == ""
            finally:
                await browser.close()
    asyncio.run(run())


def test_name_following_controls_and_manual_name_in_browser():
    from test_refresh_ui import Server, _open_page

    async def run():
        server = Server()
        updates = []
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)

                async def save_settings(route):
                    payload = route.request.post_data_json
                    updates.append(payload)
                    server.accounts[0].update(payload)
                    await route.fulfill(content_type="application/json", body=json.dumps(server.accounts[0]))

                await page.route("**/api/accounts/account_1", save_settings)
                await page.evaluate("() => openModal('accountModal')")
                assert await page.is_checked("#accountFollowDoubaoName")
                assert await page.locator("#accountName").evaluate("el => el.readOnly")
                await page.uncheck("#accountFollowDoubaoName")
                assert not await page.locator("#accountName").evaluate("el => el.readOnly")
                await page.evaluate("() => closeModal('accountModal')")
                await page.evaluate("() => openAccountSettings('account_1')")
                assert await page.is_checked("#accountSettingsFollowDoubaoName")
                assert await page.locator("#accountSettingsName").evaluate("el => el.readOnly")
                await page.uncheck("#accountSettingsFollowDoubaoName")
                await page.fill("#accountSettingsName", "自定义备注")
                await page.locator("#accountSettingsForm button[type=submit]").click()
                await page.wait_for_function("() => !document.getElementById('accountSettingsModal').classList.contains('show')")
                assert updates == [{
                    "name": "自定义备注", "follow_doubao_name": False,
                    "is_member": False, "priority": 0,
                }]
                await page.wait_for_function(
                    "() => state.accounts[0].follow_doubao_name === false"
                )
                await page.evaluate("() => openAccountSettings('account_1')")
                assert not await page.is_checked("#accountSettingsFollowDoubaoName")
                assert await page.input_value("#accountSettingsName") == "自定义备注"
            finally:
                await browser.close()
    asyncio.run(run())

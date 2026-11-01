"""分镜任务前端的刷新机制回归。

覆盖曾出现的问题：
1. 行签名漏字段，数据变了界面不更新；
2. 原地更新对行序敏感，任务增删后整表重绘；
3. 列表变空时残留旧行；
4. 搜索框聚焦后自动刷新被永久冻结；
5. 静默刷新失败时不提示。

真实 Chromium + 真实 app.js / index.html，后端接口用路由拦截提供数据。
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STATIC = Path(__file__).resolve().parents[1] / "src" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLES = (STATIC / "styles.css").read_text(encoding="utf-8")


async def launch_chromium(pw):
    exe = os.getenv("DOUBAO_TEST_CHROMIUM_EXE")
    if not exe and Path("/usr/bin/chromium").is_file():
        exe = "/usr/bin/chromium"
    options = dict(headless=True, args=["--no-sandbox"])
    if exe:
        options["executable_path"] = exe
    else:
        options["channel"] = "chrome"
    return await pw.chromium.launch(**options)


def _job(job_id, status="downloaded", **overrides):
    job = {
        "id": job_id,
        "project_id": "项目",
        "episode_id": "EP01",
        "shot_id": job_id,
        "prompt": "提示词",
        "account_id": "account_1",
        "status": status,
        "video_path": None,
        "error_message": "",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "task_name": "",
        "is_web_generation": True,
        "submit_mode": "manual",
        "web_page_url": "",
        "has_task_page": False,
        "duration": 4,
        "mode": "text2video",
        "ratio": "16:9",
        "model": "",
        "error_code": "",
        "recall_state": "",
        "can_push_result": False,
        "result_pushed_at": "",
        "result_push_error": "",
        "archived_at": "",
        "archived": False,
        "manual_confirm_sent": False,
    }
    job.update(overrides)
    return job


class Server:
    """极简路由拦截：用可变数据回答 /api/*。"""

    def __init__(self):
        self.jobs = []
        self.fail = False

    async def handle(self, route):
        url = route.request.url
        if self.fail:
            await route.fulfill(status=500, body='{"detail":"本地服务不可用"}',
                                content_type="application/json")
            return
        if url.endswith("/api/jobs"):
            await route.fulfill(status=200, content_type="application/json",
                                body=_json(self.jobs))
        elif url.endswith("/api/accounts"):
            await route.fulfill(status=200, content_type="application/json",
                                body=_json([{"id": "account_1", "name": "账号一",
                                             "note": "", "is_member": False,
                                             "priority": 0, "paused": False,
                                             "archived": False, "sequence": 1,
                                             "browser_open": False,
                                             "quota": {"remaining_seconds": 30,
                                                       "consumed_seconds": 0,
                                                       "reserved_seconds": 0,
                                                       "daily_limit_seconds": 30}}]))
        elif url.endswith("/api/stats"):
            await route.fulfill(status=200, content_type="application/json",
                                body=_json({"accounts_total": 1, "pending": 0,
                                            "processing": 0, "attention": 0,
                                            "finished": sum(1 for j in self.jobs
                                                            if j["status"] == "downloaded" and not j["archived"]),
                                            "archived": sum(1 for j in self.jobs if j["archived"]),
                                            "failed": 0, "queued": 0}))
        elif url.endswith("/api/settings/video-submit-mode"):
            await route.fulfill(status=200, content_type="application/json",
                                body=_json({"submit_mode": "auto"}))
        elif url.endswith("/api/settings/processing-limit"):
            await route.fulfill(status=200, content_type="application/json",
                                body=_json({"processing_limit": 3}))
        else:
            await route.fulfill(status=200, content_type="application/json", body="{}")


def _json(value):
    import json
    return json.dumps(value, ensure_ascii=False)


async def _open_page(browser, server):
    page = await browser.new_page()
    await page.route("**/api/**", server.handle)

    async def serve_index(route, request):
        await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                            body=INDEX_HTML)

    async def serve_app(route, request):
        await route.fulfill(status=200, content_type="application/javascript; charset=utf-8",
                            body=APP_JS)

    async def serve_css(route, request):
        await route.fulfill(status=200, content_type="text/css; charset=utf-8", body=STYLES)

    await page.route("http://local.test/", serve_index)
    await page.route("http://local.test/index.html", serve_index)
    await page.route("http://local.test/static/app.js", serve_app)
    await page.route("http://local.test/static/styles.css", serve_css)
    await page.goto("http://local.test/", wait_until="domcontentloaded")
    await page.wait_for_function("() => window.switchView && document.getElementById('jobsTable')")
    return page


async def _show_finished(page):
    await page.evaluate("() => setJobSection('finished')")
    await page.wait_for_timeout(120)


def test_silent_refresh_updates_changed_row_content():
    """数据变化后静默刷新必须更新到界面（行签名不能漏字段）。"""

    async def run():
        server = Server()
        server.jobs = [_job("job_a", status="downloaded")]
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.evaluate("() => switchView('jobs')")
                await _show_finished(page)
                before = await page.inner_text("#jobsTable")
                assert "✔ 归档" in before, before

                # 后端把该任务标记为已归档：行的归档按钮应消失。
                server.jobs = [_job("job_a", status="downloaded",
                                    archived=True,
                                    archived_at="2026-02-02T00:00:00+00:00")]
                await page.evaluate("() => refreshAll({silent:true})")
                await page.wait_for_timeout(250)
                after = await page.inner_text("#jobsTable")
                # 归档后该任务离开“处理完”，当前分区应变成空状态。
                assert "✔ 归档" not in after, after
                assert await page.locator("#jobsTable tr[data-job-id]").count() == 0

                # 切到“已归档”能看到它。
                await page.evaluate("() => setJobSection('archived')")
                await page.wait_for_timeout(200)
                archived_text = await page.inner_text("#jobsTable")
                assert "job_a" in archived_text, archived_text
            finally:
                await browser.close()

    asyncio.run(run())


def test_incremental_refresh_handles_added_and_removed_rows():
    """任务增减时增量刷新即可，不依赖整表重绘。"""

    async def run():
        server = Server()
        server.jobs = [_job("job_a"), _job("job_b")]
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.evaluate("() => switchView('jobs')")
                await _show_finished(page)
                assert await page.locator("#jobsTable tr[data-job-id]").count() == 2

                # 新增一个任务并静默刷新。
                server.jobs = [_job("job_c"), _job("job_a"), _job("job_b")]
                await page.evaluate("() => refreshAll({silent:true})")
                await page.wait_for_timeout(200)
                ids = await page.eval_on_selector_all(
                    "#jobsTable tr[data-job-id]",
                    "rows => rows.map(r => r.dataset.jobId)")
                assert ids == ["job_c", "job_a", "job_b"], ids

                # 删除一个任务并静默刷新。
                server.jobs = [_job("job_a"), _job("job_b")]
                await page.evaluate("() => refreshAll({silent:true})")
                await page.wait_for_timeout(200)
                ids = await page.eval_on_selector_all(
                    "#jobsTable tr[data-job-id]",
                    "rows => rows.map(r => r.dataset.jobId)")
                assert ids == ["job_a", "job_b"], ids
            finally:
                await browser.close()

    asyncio.run(run())


def test_empty_list_does_not_leave_stale_rows():
    async def run():
        server = Server()
        server.jobs = [_job("job_a")]
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.evaluate("() => switchView('jobs')")
                await _show_finished(page)
                assert await page.locator("#jobsTable tr[data-job-id]").count() == 1

                server.jobs = []
                await page.evaluate("() => refreshAll({silent:true})")
                await page.wait_for_timeout(200)
                assert await page.locator("#jobsTable tr[data-job-id]").count() == 0
                text = await page.inner_text("#jobsTable")
                assert "还没有分镜任务" in text or "暂无任务" in text
            finally:
                await browser.close()

    asyncio.run(run())


def test_search_focus_does_not_freeze_auto_refresh():
    """聚焦搜索框不应让自动刷新永久暂停。"""

    async def run():
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, Server())
                await page.evaluate("() => switchView('jobs')")
                await page.focus("#jobSearch")
                paused = await page.evaluate("() => autoRefreshPaused()")
                assert paused is False
            finally:
                await browser.close()

    asyncio.run(run())


def test_silent_refresh_failure_is_visible_and_recovers():
    """静默刷新失败要露出提示，恢复后自动消失。"""

    async def run():
        server = Server()
        server.jobs = [_job("job_a")]
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.evaluate("() => switchView('jobs')")
                await _show_finished(page)

                server.fail = True
                await page.evaluate("() => refreshAll({silent:true})")
                await page.wait_for_timeout(200)
                status = page.locator("#refreshStatus")
                assert await status.evaluate("el => el.classList.contains('show')") is True
                assert "刷新失败" in await status.inner_text()

                server.fail = False
                await page.evaluate("() => refreshAll({silent:true})")
                await page.wait_for_timeout(200)
                assert await status.evaluate("el => el.classList.contains('show')") is False
            finally:
                await browser.close()

    asyncio.run(run())


def test_shift_select_anchor_survives_silent_refresh():
    """静默刷新不应清空 Shift 连选锚点。"""

    async def run():
        server = Server()
        server.jobs = [_job("job_a"), _job("job_b"), _job("job_c")]
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.evaluate("() => switchView('jobs')")
                await _show_finished(page)

                first = page.locator("#jobsTable tr[data-job-id='job_a'] .job-select-input")
                await first.click()
                assert await page.evaluate("() => state.lastJobCheckIndex") == 0

                await page.evaluate("() => refreshAll({silent:true})")
                await page.wait_for_timeout(200)
                assert await page.evaluate("() => state.lastJobCheckIndex") == 0

                third = page.locator("#jobsTable tr[data-job-id='job_c'] .job-select-input")
                await third.click(modifiers=["Shift"])
                selected = await page.evaluate("() => [...state.selectedJobIds]")
                assert sorted(selected) == ["job_a", "job_b", "job_c"], selected
            finally:
                await browser.close()

    asyncio.run(run())

def test_row_signature_covers_every_rendered_field():
    """行签名必须覆盖所有会显示在行里的字段，否则静默刷新会漏更新。"""
    import re

    path = Path(__file__).resolve().parents[1] / "src" / "static" / "app.js"
    source = path.read_text(encoding="utf-8")
    signature = re.search(r"function jobRowSignature\(j\)\{[^\n]*\}", source).group(0)
    fields = set(re.findall(r"j\.(\w+)", signature))
    # 这些字段会直接改变行的可见内容或按钮可用状态。
    must_have = {
        "id", "status", "archived", "archived_at", "account_id", "video_path",
        "submit_mode", "task_name", "has_task_page", "web_page_url",
        "session_url", "session_id", "result_card_index",
        "recall_state", "can_push_result", "result_pushed_at",
        "result_push_error", "error_code", "updated_at",
    }
    missing = must_have - fields
    assert not missing, f"行签名缺少字段：{sorted(missing)}"


def test_incremental_update_is_not_order_sensitive():
    """原地更新必须按 id 对齐，不能因为行序不同就放弃增量更新。"""
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "src" / "static" / "app.js"
    source = path.read_text(encoding="utf-8")
    body = source[source.index("function updateJobsInPlace"):]
    body = body[:body.index("\n  function openJobActions")]
    # 不应再出现“行序不一致就 return false”的整表回退写法。
    assert "rows.some((row,i)=>row.dataset.jobId!==list[i].id)" not in body
    # 应按 id 建索引。
    assert "new Map(rows.map" in body
    # 空列表交给整表渲染，避免残留旧行。
    assert "if(!list.length)return false" in body


def test_search_focus_does_not_freeze_auto_refresh_source():
    """搜索框聚焦不应成为自动刷新暂停条件。"""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src" / "static" / "app.js").read_text(encoding="utf-8")
    start = source.index("function autoRefreshPaused")
    body = source[start:source.index("\n  }", start)]
    assert "freezeRefresh" in body
    assert "['INPUT','TEXTAREA','SELECT'].includes(active.tagName))return true" not in body


def test_silent_refresh_has_visible_failure_state():
    """静默刷新失败要有可见提示，而不是完全静默。"""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src" / "static" / "app.js").read_text(encoding="utf-8")
    assert "setRefreshStatus(true" in source
    assert "refreshStatus" in source
    html = (Path(__file__).resolve().parents[1] / "src" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="refreshStatus"' in html

def test_processing_section_contains_normal_and_abnormal_subgroups():
    """“待人工”并入“正在处理”：顶部 4 个标签，处理内部再分正常 / 异常待人工。"""
    source = (Path(__file__).resolve().parents[1] / "src" / "static" / "app.js").read_text(encoding="utf-8")
    # 顶部不再有独立的 attention 队列。
    assert "{key:'attention'" not in source
    assert "'needs_review'" in source
    # 正在处理包含等待人工状态。
    processing = source[source.index("{key:'processing'"):source.index("{key:'finished'")]
    assert "'awaiting_manual_send'" in processing and "'needs_review'" in processing
    # 子分组：正常 / 异常待人工。
    assert "key:'normal'" in processing and "key:'abnormal'" in processing
    assert "异常待人工" in processing

    html = (Path(__file__).resolve().parents[1] / "src" / "static" / "index.html").read_text(encoding="utf-8")
    assert html.count("data-job-section=") == 4
    assert 'data-job-section="attention"' not in html


def test_subgroup_classification_logic():
    """needs_review / awaiting_manual_send 归入异常待人工，执行阶段归入正常。"""
    import re

    source = (Path(__file__).resolve().parents[1] / "src" / "static" / "app.js").read_text(encoding="utf-8")
    defs = source[source.index("const jobGroupDefs="):source.index("function jobSectionKey")]
    normal = re.search(r"key:'normal',.*?statuses:new Set\(\[(.*?)\]\)", defs, re.S).group(1)
    abnormal = re.search(r"key:'abnormal',.*?statuses:new Set\(\[(.*?)\]\)", defs, re.S).group(1)
    for stage in ("opening", "configuring", "generating", "downloading"):
        assert f"'{stage}'" in normal
    for wait in ("awaiting_manual_send", "needs_review"):
        assert f"'{wait}'" in abnormal
        assert f"'{wait}'" not in normal


def test_processing_stat_includes_attention(tmp_path, monkeypatch):
    """概览“正在处理”卡片包含待人工，与标签计数一致。"""
    import app as application
    from fastapi.testclient import TestClient

    monkeypatch.setattr(application, "DB_PATH", tmp_path / "stats.sqlite3")
    with TestClient(application.app) as client:
        account = client.post("/api/accounts", json={"name": "a"}).json()
        job = client.post("/api/v1/videos/generations", json={
            "prompt": "统计口径", "duration": 4, "account_id": account["id"],
            "submit_mode": "manual",
        }).json()
        assert "id" in job, job
        with application.db() as conn:
            conn.execute("UPDATE jobs SET status='needs_review' WHERE id=?", (job["id"],))
        stats = client.get("/api/stats").json()
        assert stats["attention"] == 1
        assert stats["processing"] == 1
        assert stats["processing_running"] == 0

def test_subgroup_bar_renders_and_filters_in_browser():
    """真实页面：正在处理下出现 全部 / 正常 / 异常待人工，点击可筛选。"""
    async def run():
        server = Server()
        server.jobs = [
            _job("job_run", status="generating"),
            _job("job_wait", status="needs_review"),
            _job("job_manual", status="awaiting_manual_send"),
        ]
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.evaluate("() => switchView('jobs')")
                await page.evaluate("() => setJobSection('processing')")
                await page.wait_for_timeout(150)

                tabs = await page.eval_on_selector_all(
                    "#jobSubgroupBar .job-subgroup-tab",
                    "els => els.map(e => e.textContent.trim())")
                assert len(tabs) == 3, tabs
                assert any("正常" in t for t in tabs)
                assert any("异常待人工" in t for t in tabs)

                # 默认全部：三个任务都在处理分区下。
                ids = await page.eval_on_selector_all(
                    "#jobsTable tr[data-job-id]", "rows => rows.map(r => r.dataset.jobId)")
                assert sorted(ids) == ["job_manual", "job_run", "job_wait"], ids

                # 只看异常待人工。
                await page.evaluate("() => setJobSubgroup('abnormal')")
                await page.wait_for_timeout(150)
                ids = await page.eval_on_selector_all(
                    "#jobsTable tr[data-job-id]", "rows => rows.map(r => r.dataset.jobId)")
                assert sorted(ids) == ["job_manual", "job_wait"], ids

                # 只看正常。
                await page.evaluate("() => setJobSubgroup('normal')")
                await page.wait_for_timeout(150)
                ids = await page.eval_on_selector_all(
                    "#jobsTable tr[data-job-id]", "rows => rows.map(r => r.dataset.jobId)")
                assert ids == ["job_run"], ids

                # 切到别的队列时子分组栏隐藏。
                await page.evaluate("() => setJobSection('queue')")
                await page.wait_for_timeout(150)
                hidden = await page.eval_on_selector("#jobSubgroupBar",
                                                    "el => el.classList.contains('hidden')")
                assert hidden is True
            finally:
                await browser.close()

    asyncio.run(run())

def test_frontend_assets_are_not_cached(tmp_path, monkeypatch):
    """页面与静态资源必须带 no-cache，否则升级后浏览器仍用旧 app.js。"""
    import app as application
    from fastapi.testclient import TestClient

    monkeypatch.setattr(application, "DB_PATH", tmp_path / "cache.sqlite3")
    with TestClient(application.app) as client:
        for path in ("/", "/static/app.js", "/static/styles.css"):
            response = client.get(path)
            assert response.status_code == 200, path
            header = response.headers.get("cache-control", "")
            assert "no-cache" in header, f"{path} 缺少 no-cache：{header!r}"
        # API 响应不受影响。
        api = client.get("/api/health")
        assert api.headers.get("cache-control") is None

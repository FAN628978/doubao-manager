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
import json
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
        "operator_takeover": False,
        "manual_confirm_sent": False,
        "recovery": {},
    }
    job.update(overrides)
    return job


class Server:
    """极简路由拦截：用可变数据回答 /api/*。"""

    def __init__(self):
        self.jobs = []
        self.fail = False
        self.feedback_delay = 0
        self.feedback_error = False
        self.feedback_requests = 0
        self.auto_dispatch_enabled = True
        self.auto_dispatch_updates = []
        self.submit_mode = "auto"
        self.submit_mode_updates = []
        self.member_video_model = "Seedance 2.0"
        self.non_member_video_model = "Seedance 2.0 Mini"
        self.video_model_updates = []
        self.ignore_quota_check = False
        self.ignore_quota_updates = []
        self.claude_recovery_enabled = False
        self.claude_recovery_updates = []
        self.job_account_updates = []
        self.accounts = [{
            "id": "account_1", "name": "账号一", "note": "",
            "is_member": False, "priority": 0, "paused": False,
            "archived": False, "sequence": 1, "browser_open": False,
            "quota": {
                "remaining_seconds": 30, "consumed_seconds": 0,
                "reserved_seconds": 0, "daily_limit_seconds": 30,
            },
        }]

    async def handle(self, route):
        url = route.request.url
        if self.fail:
            await route.fulfill(status=500, body='{"detail":"本地服务不可用"}',
                                content_type="application/json")
            return
        if url.endswith("/api/test/feedback"):
            self.feedback_requests += 1
            if self.feedback_delay:
                await asyncio.sleep(self.feedback_delay)
            if self.feedback_error:
                await route.fulfill(
                    status=500,
                    content_type="application/json",
                    body='{"detail":"反馈测试失败"}',
                )
            else:
                await route.fulfill(status=200, content_type="application/json",
                                    body='{"ok":true}')
            return
        if url.endswith("/api/test/feedback-chain"):
            if self.feedback_delay:
                await asyncio.sleep(self.feedback_delay)
            await route.fulfill(status=200, content_type="application/json",
                                body='{"ok":true}')
            return
        if url.endswith("/api/jobs"):
            await route.fulfill(status=200, content_type="application/json",
                                body=_json(self.jobs))
        elif url.endswith("/api/accounts"):
            await route.fulfill(status=200, content_type="application/json",
                                body=_json(self.accounts))
        elif url.endswith("/api/stats"):
            await route.fulfill(status=200, content_type="application/json",
                                body=_json({"accounts_total": 1, "pending": 0,
                                            "processing": 0, "attention": 0,
                                            "finished": sum(1 for j in self.jobs
                                                            if j["status"] == "downloaded" and not j["archived"]),
                                            "archived": sum(1 for j in self.jobs if j["archived"]),
                                            "failed": 0, "queued": 0}))
        elif url.endswith("/api/settings/video-submit-mode"):
            if route.request.method == "PUT":
                body = json.loads(route.request.post_data or "{}")
                self.submit_mode = body.get("submit_mode") or "manual"
                self.submit_mode_updates.append(self.submit_mode)
            await route.fulfill(status=200, content_type="application/json",
                                body=_json({"submit_mode": self.submit_mode}))
        elif url.endswith("/api/settings/auto-dispatch"):
            if route.request.method == "PUT":
                body = json.loads(route.request.post_data or "{}")
                self.auto_dispatch_enabled = bool(
                    body.get("auto_dispatch_enabled")
                )
                self.auto_dispatch_updates.append(
                    self.auto_dispatch_enabled
                )
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=_json({
                    "auto_dispatch_enabled": self.auto_dispatch_enabled,
                }),
            )
        elif url.endswith("/api/settings/video-models"):
            if route.request.method == "PUT":
                body = json.loads(route.request.post_data or "{}")
                self.member_video_model = body.get("member_model")
                self.non_member_video_model = body.get("non_member_model")
                self.video_model_updates.append((
                    self.member_video_model,
                    self.non_member_video_model,
                ))
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=_json({
                    "member_model": self.member_video_model,
                    "non_member_model": self.non_member_video_model,
                    "available_models": [
                        "Seedance 2.5", "Seedance 2.0",
                        "Seedance 2.0 Fast", "Seedance 2.0 Mini",
                    ],
                }),
            )
        elif url.endswith("/api/settings/processing-limit"):
            await route.fulfill(status=200, content_type="application/json",
                                body=_json({"processing_limit": 3}))
        elif url.endswith("/api/settings/ignore-quota-check"):
            if route.request.method == "PUT":
                body = json.loads(route.request.post_data or "{}")
                self.ignore_quota_check = bool(body.get("ignore_quota_check"))
                self.ignore_quota_updates.append(self.ignore_quota_check)
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=_json({"ignore_quota_check": self.ignore_quota_check}),
            )
        elif url.endswith("/api/settings/claude-recovery"):
            if route.request.method == "PUT":
                body = json.loads(route.request.post_data or "{}")
                self.claude_recovery_enabled = bool(
                    body.get("claude_recovery_enabled")
                )
                self.claude_recovery_updates.append(
                    self.claude_recovery_enabled
                )
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=_json({
                    "claude_recovery_enabled": self.claude_recovery_enabled,
                    "environment_enabled": True,
                    "active_recoveries": 0,
                }),
            )
        elif "/api/jobs/" in url and url.endswith("/account"):
            job_id = url.split("/api/jobs/", 1)[1].rsplit("/account", 1)[0]
            body = json.loads(route.request.post_data or "{}")
            account_id = body.get("account_id") or None
            self.job_account_updates.append((job_id, account_id))
            job = next(item for item in self.jobs if item["id"] == job_id)
            job["account_id"] = account_id
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=_json(job),
            )
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


def test_takeover_task_enables_all_five_process_buttons():
    async def run():
        server = Server()
        server.jobs = [_job(
            "job_takeover",
            status="needs_review",
            operator_takeover=True,
        )]
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.evaluate(
                    "() => {switchView('jobs');setJobSection('processing')}"
                )
                buttons = page.locator(
                    "#jobsTable tr[data-job-id='job_takeover'] .next-row > button"
                )
                assert await buttons.count() >= 5
                for index in range(5):
                    assert not await buttons.nth(index).is_disabled()
            finally:
                await browser.close()

    asyncio.run(run())


def test_resumed_takeover_displays_generating_not_downloading():
    async def run():
        server = Server()
        server.jobs = [_job(
            "job_resumed",
            status="generating",
            recall_state="recalling",
        )]
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.evaluate(
                    "() => {switchView('jobs');setJobSection('processing')}"
                )
                row = page.locator("#jobsTable tr[data-job-id='job_resumed']")
                text = await row.inner_text()
                assert "生成中" in text
                assert "等待原结果" in text
                assert "重新下载中" not in text
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
        "result_push_error", "operator_takeover", "error_code", "updated_at", "recovery",
    }
    missing = must_have - fields
    assert not missing, f"行签名缺少字段：{sorted(missing)}"


def test_bulk_push_only_applies_to_unpushed_jobs():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    applies = source[source.index("function bulkApplies"):]
    applies = applies[:applies.index("\n  function bulkActionJobs")]
    assert "if(action==='push')" in applies
    assert "Boolean(job.can_push_result)&&!job.result_pushed_at" in applies


def test_task_open_close_actions_target_bound_page_not_account_window():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    actions = source[source.index("function fixedJobActions"):]
    actions = actions[:actions.index("\n  function filteredJobs")]
    assert "label:'打开网页'" in actions
    assert "label:'关闭网页'" in actions
    assert "openJobPage" in actions
    assert "closeJobPage" in actions
    assert "/open-page`" in source
    assert "/close-page`" in source
    assert "function openJobWindow" not in source
    assert "function closeJobWindow" not in source


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


def test_new_task_creation_keeps_current_management_view():
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    create_job = source[source.index("async function createJob"):]
    create_job = create_job[:create_job.index("function syncWebMode")]
    assert "switchView('jobs')" not in create_job

    web_submit = source[source.index("$('webForm').addEventListener"):]
    web_submit = web_submit[:web_submit.index("$('accountForm').addEventListener")]
    assert "switchView('jobs')" not in web_submit


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


def test_jobs_api_exposes_live_claude_status(tmp_path, monkeypatch):
    import app as application
    from fastapi.testclient import TestClient

    monkeypatch.setattr(application, "DB_PATH", tmp_path / "recovery.sqlite3")
    with TestClient(application.app) as client:
        created = client.post("/api/jobs", json={
            "prompt": "Claude status",
            "project_id": "project",
            "episode_id": "episode",
            "shot_id": "shot",
        })
        assert created.status_code == 201, created.text
        job_id = created.json()["id"]
        service = application.app.state.video_recovery_service
        monkeypatch.setattr(
            service,
            "status_for_job",
            lambda requested: {
                "active": requested == job_id,
                "backend": "claude-cli",
                "state": "acting",
                "action": "inspect_dom",
                "detail": "Claude is inspecting the page",
            },
        )
        jobs = client.get("/api/jobs").json()

    target = next(job for job in jobs if job["id"] == job_id)
    assert target["recovery"]["active"] is True
    assert target["recovery"]["backend"] == "claude-cli"
    assert target["recovery"]["action"] == "inspect_dom"


def test_claude_recovery_status_is_visible_and_updates_in_browser():
    async def run():
        server = Server()
        server.jobs = [_job(
            "job_agent",
            status="needs_review",
            recovery={
                "active": True,
                "backend": "claude-cli",
                "state": "acting",
                "phase": "tool",
                "action": "inspect_dom",
                "detail": "Claude 正在检查页面",
            },
        )]
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.evaluate("() => switchView('jobs')")
                await page.evaluate("() => setJobSection('processing')")
                await page.wait_for_timeout(150)
                active_text = await page.inner_text("#jobsTable")
                assert "Claude 处理中" in active_text
                assert "Claude 正在检查页面" in active_text
                assert "检查页面 DOM" in active_text

                server.jobs = [_job(
                    "job_agent",
                    status="needs_review",
                    recovery={
                        "active": False,
                        "backend": "claude-cli",
                        "state": "needs_human",
                        "manual_reason": "需要人工登录豆包",
                        "detail": "需要人工登录豆包",
                    },
                )]
                await page.evaluate("() => refreshAll({silent:true})")
                await page.wait_for_timeout(250)
                finished_text = await page.inner_text("#jobsTable")
                assert "Claude 处理中" not in finished_text
                assert "Claude 已转人工" in finished_text
                assert "需要人工登录豆包" in finished_text
            finally:
                await browser.close()

    asyncio.run(run())

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


def _add_feedback_button(page, label, endpoint, refresh_after=False):
    return page.evaluate(
        """({label, endpoint, refreshAfter}) => {
            const button = document.createElement('button');
            button.id = 'feedbackTest';
            button.className = 'btn primary';
            button.textContent = label;
            button.dataset.clicks = '0';
            button.addEventListener('click', async () => {
                button.dataset.clicks = String(Number(button.dataset.clicks) + 1);
                try {
                    await api(endpoint);
                    if (refreshAfter) await refreshAll();
                    toast('测试操作成功');
                } catch (error) {
                    toast(error.message, true);
                }
            });
            document.body.appendChild(button);
        }""",
        {"label": label, "endpoint": endpoint, "refreshAfter": refresh_after},
    )


def test_button_feedback_loading_success_and_duplicate_click_guard():
    async def run():
        server = Server()
        server.feedback_delay = 2.0
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await _add_feedback_button(page, "测试操作", "/api/test/feedback")
                button = page.locator("#feedbackTest")
                await button.click()
                await page.wait_for_selector("#feedbackTest.is-loading")
                loading = await page.evaluate(
                    """() => {
                        const button = document.querySelector('#feedbackTest');
                        return {
                            text: button.textContent,
                            disabled: button.disabled,
                            ariaBusy: button.getAttribute('aria-busy'),
                        };
                    }"""
                )
                assert "处理中" in loading["text"], loading
                assert loading["disabled"] is True, loading
                assert loading["ariaBusy"] == "true", loading

                await page.evaluate("() => document.querySelector('#feedbackTest').click()")
                await page.evaluate("() => document.querySelector('#feedbackTest').click()")
                assert await button.get_attribute("data-clicks") == "1"
                assert server.feedback_requests == 1

                await page.wait_for_selector("#feedbackTest.is-success", timeout=6000)
                assert "已完成" in await button.inner_text()
                assert await button.is_disabled()
                await page.wait_for_function(
                    """() => {
                        const button = document.querySelector('#feedbackTest');
                        return !button.classList.contains('is-loading')
                            && !button.classList.contains('is-success')
                            && !button.disabled
                            && button.textContent === '测试操作';
                    }""",
                    timeout=3000,
                )
                assert await button.get_attribute("aria-busy") is None
            finally:
                await browser.close()

    asyncio.run(run())


def test_button_feedback_error_is_visible_and_restores_button():
    async def run():
        server = Server()
        server.feedback_delay = 0.05
        server.feedback_error = True
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await _add_feedback_button(page, "失败操作", "/api/test/feedback")
                button = page.locator("#feedbackTest")
                await button.click()
                await page.wait_for_function(
                    "() => document.querySelector('#toast').classList.contains('error')"
                )
                assert "反馈测试失败" in await page.locator("#toast").inner_text()
                await page.wait_for_function(
                    """() => {
                        const button = document.querySelector('#feedbackTest');
                        return !button.classList.contains('is-loading')
                            && !button.disabled
                            && button.textContent === '失败操作';
                    }""",
                    timeout=3000,
                )
            finally:
                await browser.close()

    asyncio.run(run())


def test_consecutive_api_calls_keep_original_button_content():
    async def run():
        server = Server()
        server.feedback_delay = 0.05
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await _add_feedback_button(
                    page, "连续操作", "/api/test/feedback-chain", refresh_after=True
                )
                await page.locator("#feedbackTest").click()
                await page.wait_for_function(
                    """() => {
                        const button = document.querySelector('#feedbackTest');
                        return !button.classList.contains('is-loading')
                            && !button.classList.contains('is-success')
                            && !button.disabled
                            && button.textContent === '连续操作';
                    }""",
                    timeout=4000,
                )
                assert "处理中" not in await page.locator("#feedbackTest").inner_text()
            finally:
                await browser.close()

    asyncio.run(run())


def test_button_styles_cover_states_focus_and_reduced_motion():
    for selector in (
        ".btn:hover:not(:disabled)",
        ".btn:active:not(:disabled)",
        ".btn.is-loading",
        ".btn.is-success",
        "button:focus-visible",
        "@media(prefers-reduced-motion:reduce)",
    ):
        assert selector in STYLES, selector


def test_mobile_buttons_do_not_clip_labels():
    async def run():
        server = Server()
        server.jobs = [_job("job_a", status="downloaded", video_path="job_a.mp4")]
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.evaluate("() => switchView('accounts')")
                assert await page.evaluate(
                    """() => [...document.querySelectorAll('button')]
                        .filter(button => button.offsetParent !== null)
                        .every(button => button.scrollWidth <= button.clientWidth + 1
                            && button.scrollHeight <= button.clientHeight + 1)"""
                )
                await page.evaluate(
                    "() => {switchView('jobs');setJobSection('finished')}"
                )
                await page.wait_for_timeout(150)
                clipped = await page.evaluate(
                    """() => [...document.querySelectorAll('button')]
                        .filter(button => button.offsetParent !== null)
                        .filter(button => button.scrollWidth > button.clientWidth + 1
                            || button.scrollHeight > button.clientHeight + 1)
                        .map(button => button.textContent.trim())"""
                )
                assert clipped == [], clipped
            finally:
                await browser.close()

    asyncio.run(run())


def test_automation_switches_are_saved_independently():
    async def run():
        server = Server()
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.evaluate("() => openSubmitModeSettings()")
                assert await page.is_checked("#autoDispatchSettingToggle")
                assert await page.is_checked("#autoExecutionSettingToggle")
                assert await page.inner_text("#dispatchFlowState") == "自动进入调度"
                assert await page.inner_text("#executionFlowState") == "自动执行完整流程"
                assert await page.input_value("#memberModelSetting") == "Seedance 2.0"
                assert await page.input_value("#nonMemberModelSetting") == "Seedance 2.0 Mini"

                await page.click("#autoDispatchSettingToggle")
                await page.select_option("#memberModelSetting", "Seedance 2.0 Fast")
                await page.select_option("#nonMemberModelSetting", "Seedance 2.5")
                assert not await page.is_checked("#autoDispatchSettingToggle")
                assert await page.is_checked("#autoExecutionSettingToggle")
                assert await page.inner_text("#dispatchFlowState") == "等待手动放行"
                await page.evaluate("() => $('submitModeForm').requestSubmit()")
                await page.wait_for_function(
                    "() => !document.getElementById('submitModeModal').classList.contains('show')"
                )

                assert server.auto_dispatch_updates == [False]
                assert server.submit_mode_updates == ["auto"]
                assert server.video_model_updates == [
                    ("Seedance 2.0 Fast", "Seedance 2.5")
                ]
                assert await page.evaluate(
                    "() => state.autoDispatchEnabled"
                ) is False
                assert await page.evaluate("() => state.submitMode") == "auto"
            finally:
                await browser.close()

    asyncio.run(run())


def test_task_settings_modal_body_scrolls_on_short_viewport():
    async def run():
        server = Server()
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.set_viewport_size({"width": 1000, "height": 560})
                await page.evaluate("() => openSubmitModeSettings()")
                dimensions = await page.locator(
                    "#submitModeModal .task-settings-body"
                ).evaluate(
                    "el => ({clientHeight: el.clientHeight, scrollHeight: el.scrollHeight})"
                )
                assert dimensions["scrollHeight"] > dimensions["clientHeight"]
                await page.locator(
                    "#submitModeModal .task-settings-body"
                ).evaluate("el => { el.scrollTop = el.scrollHeight; }")
                assert await page.locator(
                    "#submitModeModal .task-settings-body"
                ).evaluate("el => el.scrollTop") > 0
                assert await page.locator(
                    "#submitModeModal .modal-foot"
                ).is_visible()
            finally:
                await browser.close()

    asyncio.run(run())


def test_global_quota_override_is_saved_from_task_settings():
    async def run():
        server = Server()
        server.accounts = [
            {
                "id": "account_low", "name": "low", "note": "",
                "is_member": False, "priority": 100, "paused": False,
                "archived": False, "sequence": 1, "browser_open": False,
                "quota": {"remaining_seconds": 0},
            },
            {
                "id": "account_max", "name": "max", "note": "",
                "is_member": False, "priority": 0, "paused": False,
                "archived": False, "sequence": 2, "browser_open": False,
                "quota": {"remaining_seconds": 12},
            },
            {
                "id": "account_paused", "name": "paused", "note": "",
                "is_member": False, "priority": 0, "paused": True,
                "archived": False, "sequence": 3, "browser_open": False,
                "quota": {"remaining_seconds": 30},
            },
            {
                "id": "account_busy", "name": "busy", "note": "",
                "is_member": False, "priority": 0, "paused": False,
                "archived": False, "sequence": 4, "browser_open": True,
                "quota": {"remaining_seconds": 30},
            },
        ]
        server.jobs = [
            _job("busy_job", status="generating", account_id="account_busy")
        ]
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.evaluate("() => openSubmitModeSettings()")
                assert not await page.is_checked("#ignoreQuotaSettingToggle")
                await page.click("#ignoreQuotaSettingToggle")
                assert await page.evaluate("() => state.settingIgnoreQuota") is True
                assert await page.is_checked("#ignoreQuotaSettingToggle")
                assert "已开启" in await page.inner_text("#ignoreQuotaSettingState")
                await page.evaluate("() => $('submitModeForm').requestSubmit()")
                await page.wait_for_function(
                    "() => !document.getElementById('submitModeModal').classList.contains('show')"
                )
                assert server.ignore_quota_updates == [True]
                assert await page.evaluate("() => state.ignoreQuotaCheck") is True
                await page.evaluate("() => openModal('webModal')")
                # 新任务默认保持“进入处理前自动分配”；最大剩余额度选择由
                # 后端在真正执行时完成，操作者也可以手动选定账号。
                assert await page.input_value("#webAccount") == ""
                assert "自动分配" in await page.locator(
                    "#webAccount option"
                ).first.inner_text()
                await page.evaluate("() => closeModal('webModal')")

                await page.evaluate("() => openSubmitModeSettings()")
                assert await page.is_checked("#ignoreQuotaSettingToggle")
            finally:
                await browser.close()

    asyncio.run(run())


def test_claude_recovery_switch_is_saved_from_task_settings():
    async def run():
        server = Server()
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.evaluate("() => openSubmitModeSettings()")
                assert not await page.is_checked("#claudeRecoverySettingToggle")
                await page.click("#claudeRecoverySettingToggle")
                assert await page.is_checked("#claudeRecoverySettingToggle")
                assert await page.evaluate(
                    "() => state.settingClaudeRecovery"
                ) is True
                assert "已开启" in await page.inner_text(
                    "#claudeRecoverySettingState"
                )

                await page.evaluate("() => $('submitModeForm').requestSubmit()")
                await page.wait_for_function(
                    "() => !document.getElementById('submitModeModal').classList.contains('show')"
                )
                assert server.claude_recovery_updates == [True]
                assert await page.evaluate(
                    "() => state.claudeRecoveryEnabled"
                ) is True

                await page.evaluate("() => openSubmitModeSettings()")
                assert await page.is_checked("#claudeRecoverySettingToggle")
            finally:
                await browser.close()

    asyncio.run(run())


def test_pending_web_job_can_select_or_restore_automatic_account():
    async def run():
        server = Server()
        server.jobs = [_job(
            "job_pending_account",
            status="queued",
            account_id=None,
            dispatched_at="",
        )]
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.evaluate("() => switchView('jobs')")
                await page.evaluate("() => setJobSection('queue')")
                select = page.locator(
                    "tr[data-job-id='job_pending_account'] .web-account-select"
                )
                assert await select.input_value() == ""
                assert "自动分配" in await select.locator("option").first.inner_text()

                await select.select_option("account_1")
                await page.wait_for_function(
                    "() => state.jobs.find(j => j.id === 'job_pending_account').account_id === 'account_1'"
                )
                assert server.job_account_updates == [
                    ("job_pending_account", "account_1")
                ]

                await select.select_option("")
                await page.wait_for_function(
                    "() => state.jobs.find(j => j.id === 'job_pending_account').account_id === null"
                )
                assert server.job_account_updates[-1] == (
                    "job_pending_account", None
                )
            finally:
                await browser.close()

    asyncio.run(run())


def test_account_list_paginates_and_search_resets_to_first_page():
    async def run():
        server = Server()
        template = server.accounts[0]
        server.accounts = [{
            **template,
            "id": f"account_{index:02d}",
            "name": f"账号{index:02d}",
            "sequence": index,
            "quota": dict(template["quota"]),
        } for index in range(1, 46)]
        async with async_playwright() as pw:
            browser = await launch_chromium(pw)
            try:
                page = await _open_page(browser, server)
                await page.evaluate("() => switchView('accounts')")

                assert await page.locator("#accountsTable tr").count() == 20
                assert "第 1 / 3 页" in await page.inner_text("#accountPagination")
                first_page = await page.inner_text("#accountsTable")
                assert "account_01" in first_page
                assert "account_21" not in first_page

                await page.get_by_role("button", name="下一页").click()
                second_page = await page.inner_text("#accountsTable")
                assert "account_21" in second_page
                assert "account_01" not in second_page
                assert "第 2 / 3 页" in await page.inner_text("#accountPagination")

                await page.fill("#accountSearch", "账号45")
                assert await page.locator("#accountsTable tr").count() == 1
                assert "account_45" in await page.inner_text("#accountsTable")
                assert await page.evaluate("() => state.accountPage") == 1
                assert await page.locator("#accountPagination").is_hidden()
            finally:
                await browser.close()

    asyncio.run(run())

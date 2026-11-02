from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from doubao_manager.chrome_runtime import extension_launch_args, resolve_project_chrome  # noqa: E402


TOKENS = {"token-a-" + "a" * 36: ("job-a", "账号 A"), "token-b-" + "b" * 36: ("job-b", "账号 B")}
POSTS: list[str] = []


def payload(token: str) -> dict:
    job_id, account = TOKENS[token]
    labels = ["提交素材", "确认发送", "发送确认语", "下载视频", "推送服务器"]
    keys = ["prepare", "send", "confirm", "download", "push"]
    return {
        "job_id": job_id, "account_id": account, "account_name": account,
        "project_id": f"project-{job_id[-1]}", "episode_id": "ep01", "shot_id": "shot01",
        "task_name": f"分镜 {job_id}", "status": "awaiting_manual_send", "submit_mode": "manual",
        "error_code": "", "error_message": "", "updated_at": "2026-11-02T10:00:00+08:00",
        "last_action_at": "2026-11-02T10:00:00+08:00", "last_action": "Mock 任务已同步",
        "recovery": {}, "events": [],
        "actions": [
            {
                "key": key, "label": label, "enabled": True, "reason": "",
                "confirm": "" if key in ("download", "push") else "确认测试？",
            }
            for key, label in zip(keys, labels)
        ],
        "confirmation_presets": [{"id": "preset_default", "name": "默认确认语", "text": "确认"}],
        "takeover": {"active": False, "can_takeover": True, "can_resume": False},
        "manual_controls": [
            {
                "key": "restart_auto", "label": "重新自动执行", "enabled": True,
                "reason": "", "confirm": "确认重新自动执行？",
            },
            {
                "key": "next_task", "label": "下一个任务", "enabled": True,
                "reason": "", "confirm": "确认取出下一个任务？",
            },
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        token = parse_qs(parsed.query).get("binding_token", [""])[0]
        if parsed.path == "/api/v1/extension/tasks/resolve" and token in TOKENS:
            body = json.dumps(payload(token), ensure_ascii=False).encode()
            self.send_response(200)
        else:
            body = json.dumps({"detail": "not found"}).encode()
            self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        POSTS.append(self.path)
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


async def verify_context(playwright, executable: Path, profile: str, api_base: str, restarted=False):
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=profile,
        executable_path=str(executable),
        headless=True,
        args=[*extension_launch_args(PROJECT_ROOT), "--no-first-run"],
    )

    async def mock_doubao(route):
        suffix = "a" if route.request.url.endswith("/a") else "b"
        token = next(value for value in TOKENS if value.startswith(f"token-{suffix}-"))
        await route.fulfill(
            content_type="text/html",
            body=f'<html><head><meta name="doubao-manager-task-binding" content="{token}"></head><body>Mock</body></html>',
        )

    await context.route("https://www.doubao.com/chat/mock/**", mock_doubao)
    first = context.pages[0] if context.pages else await context.new_page()
    await first.goto("https://www.doubao.com/chat/mock/a")
    await first.locator("#doubao-manager-extension-root").wait_for(state="attached")
    for _ in range(50):
        if context.service_workers:
            break
        await first.wait_for_timeout(100)
    if not context.service_workers:
        raise RuntimeError("扩展 Service Worker 未启动")
    await context.service_workers[0].evaluate(
        "base => chrome.storage.local.set({apiBase: base})", api_base
    )
    await first.reload()
    second = await context.new_page()
    await second.goto("https://www.doubao.com/chat/mock/b")

    async def panel_text(page):
        await page.wait_for_function(
            "document.querySelector('#doubao-manager-extension-root')?.shadowRoot?.textContent.includes('推送服务器')"
        )
        return await page.locator("#doubao-manager-extension-root").evaluate(
            "host => host.shadowRoot.textContent"
        )

    first_text, second_text = await panel_text(first), await panel_text(second)
    assert "账号 A" in first_text and "job-a" in first_text and "账号 B" not in first_text
    assert "账号 B" in second_text and "job-b" in second_text and "账号 A" not in second_text
    for label in [
        "提交素材", "确认发送", "发送确认语", "下载视频", "推送服务器",
        "重新自动执行", "下一个任务",
    ]:
        assert label in first_text
    if not restarted:
        expected_posts = [
            "/api/v1/videos/generations/job-a/prepare-manual",
            "/api/v1/videos/generations/job-a/send?confirm_send=true",
            "/api/v1/videos/generations/job-a/send-confirmation?confirm_send=true",
            "/api/v1/videos/generations/job-a/recall?force_redownload=true",
            "/api/v1/videos/generations/job-a/push-result",
            "/api/v1/videos/generations/job-a/restart-auto",
            "/api/v1/videos/generations/job-a/dispatch-next",
        ]
        for index, action in enumerate(
            ["prepare", "send", "confirm", "download", "push", "restart_auto", "next_task"],
            start=1,
        ):
            await first.locator(
                "#doubao-manager-extension-root"
            ).locator(f'[data-task-action="{action}"]').click()
            for _ in range(50):
                if len(POSTS) >= index:
                    break
                await first.wait_for_timeout(100)
        assert POSTS == expected_posts, POSTS
        assert await first.locator(
            "#doubao-manager-extension-root"
        ).locator('[data-role="confirm"]').count() == 0
    if restarted:
        assert "job-a" in first_text
    await first.locator("#doubao-manager-extension-root").evaluate(
        "host => host.shadowRoot.querySelector('[data-action=close]').click()"
    )
    assert not first.is_closed() and not second.is_closed()
    await context.close()


async def check() -> None:
    executable = resolve_project_chrome(PROJECT_ROOT)
    if executable is None:
        raise RuntimeError("尚未安装项目专用 Chrome")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        api_base = f"http://127.0.0.1:{server.server_port}"
        with tempfile.TemporaryDirectory(prefix="doubao-extension-panel-") as profile:
            async with async_playwright() as playwright:
                await verify_context(playwright, executable, profile, api_base)
                await verify_context(playwright, executable, profile, api_base, restarted=True)
        assert POSTS == [
            "/api/v1/videos/generations/job-a/prepare-manual",
            "/api/v1/videos/generations/job-a/send?confirm_send=true",
            "/api/v1/videos/generations/job-a/send-confirmation?confirm_send=true",
            "/api/v1/videos/generations/job-a/recall?force_redownload=true",
            "/api/v1/videos/generations/job-a/push-result",
            "/api/v1/videos/generations/job-a/restart-auto",
            "/api/v1/videos/generations/job-a/dispatch-next",
        ], "关闭扩展面板不应额外调用任务或浏览器关闭接口"
    finally:
        server.shutdown()
        server.server_close()
    print("[OK] 多账号、多标签页、页面刷新、浏览器重启和面板关闭隔离验收通过")


if __name__ == "__main__":
    asyncio.run(check())

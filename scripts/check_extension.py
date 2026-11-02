from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from doubao_manager.chrome_runtime import extension_launch_args, resolve_project_chrome  # noqa: E402


async def check() -> None:
    executable = resolve_project_chrome(PROJECT_ROOT)
    if executable is None:
        raise RuntimeError("尚未安装项目专用 Chrome")
    with tempfile.TemporaryDirectory(prefix="doubao-extension-check-") as profile:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=profile,
                executable_path=str(executable),
                headless=True,
                args=[*extension_launch_args(PROJECT_ROOT), "--no-first-run"],
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    await page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                await page.locator("#doubao-manager-extension-root").wait_for(
                    state="attached", timeout=15000
                )
                for _ in range(50):
                    if context.service_workers:
                        break
                    await page.wait_for_timeout(100)
                if not context.service_workers:
                    raise RuntimeError("扩展 Service Worker 未启动")
            finally:
                await context.close()
    print("[OK] 项目 Chrome 已自动加载 MV3 扩展，豆包页面面板容器和 Service Worker 正常")


if __name__ == "__main__":
    asyncio.run(check())

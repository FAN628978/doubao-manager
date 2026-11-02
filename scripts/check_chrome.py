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

from doubao_manager.chrome_runtime import browser_version, resolve_project_chrome  # noqa: E402


async def check() -> None:
    executable = resolve_project_chrome(PROJECT_ROOT)
    if executable is None:
        raise RuntimeError("尚未安装项目专用 Chrome")
    with tempfile.TemporaryDirectory(prefix="doubao-chrome-check-") as profile:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=profile,
                executable_path=str(executable),
                headless=True,
                args=["--no-first-run", "--no-default-browser-check"],
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto("data:text/html,<title>doubao-project-chrome-ok</title>")
                if await page.title() != "doubao-project-chrome-ok":
                    raise RuntimeError("项目 Chrome 页面自检失败")
            finally:
                await context.close()
    print(f"[OK] 项目专用 Chrome 启动自检通过：{browser_version(executable)}")
    print(f"     {executable}")


if __name__ == "__main__":
    asyncio.run(check())

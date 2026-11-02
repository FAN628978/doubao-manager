from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from doubao_manager.chrome_runtime import (  # noqa: E402
    ChromeInstallError,
    browser_version,
    install_project_chrome,
    resolve_project_chrome,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="安装或检查项目专用 Chrome for Testing")
    parser.add_argument("--check", action="store_true", help="只检查现有项目 Chrome")
    parser.add_argument("--if-missing", action="store_true", help="已安装时不检查远程升级")
    parser.add_argument("--force", action="store_true", help="强制重新检查并安装稳定版")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    try:
        current = resolve_project_chrome(PROJECT_ROOT)
        if args.check or (args.if_missing and current is not None):
            if current is None:
                raise ChromeInstallError("尚未安装项目专用 Chrome，请运行 scripts/install_chrome.py")
            result = {
                "ok": True,
                "installed": False,
                "executable": str(current),
                "version": browser_version(current),
            }
        else:
            installed = install_project_chrome(PROJECT_ROOT, force=args.force)
            result = {
                "ok": True,
                "installed": installed.installed,
                "executable": str(installed.executable),
                "version": installed.version,
                "platform": installed.platform,
                "sha256": installed.sha256,
            }
    except ChromeInstallError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        action = "已安装" if result.get("installed") else "可用"
        print(f"[OK] 项目专用 Chrome {action}：{result['version']}")
        print(f"     {result['executable']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

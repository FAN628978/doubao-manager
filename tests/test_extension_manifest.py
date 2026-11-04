import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from doubao_manager.chrome_runtime import extension_launch_args


def test_manifest_v3_targets_only_doubao_pages():
    manifest = json.loads((ROOT / "browser/extension/manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert tuple(map(int, manifest["version"].split("."))) >= (1, 0, 2)
    assert manifest["background"] == {"service_worker": "background.js", "type": "module"}
    assert manifest["options_ui"] == {"page": "options.html", "open_in_tab": True}
    matches = manifest["content_scripts"][0]["matches"]
    assert matches == ["https://doubao.com/*", "https://www.doubao.com/*"]
    assert set(manifest["host_permissions"]) == {"http://127.0.0.1/*", "http://localhost/*"}
    assert "<all_urls>" not in json.dumps(manifest)


def test_project_chrome_loads_only_project_extension():
    args = extension_launch_args(ROOT)
    extension = str((ROOT / "browser/extension").resolve())
    assert args == [
        f"--disable-extensions-except={extension}",
        f"--load-extension={extension}",
    ]


def test_extension_api_key_stays_in_service_worker_storage():
    api = (ROOT / "browser/extension/api.js").read_text(encoding="utf-8")
    content = (ROOT / "browser/extension/content.js").read_text(encoding="utf-8")
    assert 'chrome.storage.local.get(["apiBase", "apiKey"])' in api
    assert "X-API-Key" in api
    assert "apiKey" not in content
    options = (ROOT / "browser/extension/options.js").read_text(encoding="utf-8")
    assert "chrome.storage.local.set" in options
    assert "127\\.0\\.0\\.1" in options and "localhost" in options

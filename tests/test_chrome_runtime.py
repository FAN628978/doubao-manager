import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from doubao_manager.chrome_runtime import (
    ChromeInstallError,
    chrome_platform,
    install_project_chrome,
    resolve_project_chrome,
)


class Response:
    def __init__(self, body: bytes):
        self.body = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.body.read(size)


def chrome_archive() -> bytes:
    output = io.BytesIO()
    executable = "chrome.exe" if sys.platform == "win32" else "chrome"
    with zipfile.ZipFile(output, "w") as package:
        package.writestr(f"chrome-test/{executable}", b"mock chrome")
    return output.getvalue()


def test_platform_detection():
    assert chrome_platform("Windows", "AMD64") == "win64"
    assert chrome_platform("Windows", "x86") == "win32"
    assert chrome_platform("Darwin", "arm64") == "mac-arm64"
    assert chrome_platform("Linux", "x86_64") == "linux64"
    with pytest.raises(ChromeInstallError):
        chrome_platform("Linux", "arm64")


def test_install_and_reuse_project_chrome(tmp_path):
    target = chrome_platform()
    metadata_url = "https://metadata.test/stable.json"
    download_url = "https://downloads.test/chrome.zip"
    metadata = json.dumps({
        "channels": {"Stable": {"version": "123.0.1.2", "downloads": {
            "chrome": [{"platform": target, "url": download_url}]
        }}}
    }).encode()
    archive = chrome_archive()
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, timeout))
        return Response(metadata if request.full_url == metadata_url else archive)

    installed = install_project_chrome(
        tmp_path,
        metadata_url=metadata_url,
        opener=opener,
        version_probe=lambda _path: "123.0.1.2",
    )
    assert installed.installed is True
    assert installed.executable.is_file()
    assert resolve_project_chrome(tmp_path) == installed.executable
    manifest = json.loads((tmp_path / "browser/chrome/install.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "123.0.1.2"
    assert len(manifest["sha256"]) == 64

    reused = install_project_chrome(
        tmp_path,
        metadata_url=metadata_url,
        opener=opener,
        version_probe=lambda _path: "123.0.1.2",
    )
    assert reused.installed is False
    assert [url for url, _timeout in calls].count(download_url) == 1


def test_legacy_browser_remains_first_choice(tmp_path):
    legacy = tmp_path / "browser/chrome.exe"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    managed = tmp_path / "browser/chrome/123/chrome-win64/chrome.exe"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"managed")
    assert resolve_project_chrome(tmp_path) == legacy


def test_rejects_corrupt_archive(tmp_path):
    target = chrome_platform()
    metadata_url = "https://metadata.test/stable.json"
    download_url = "https://downloads.test/chrome.zip"
    metadata = json.dumps({
        "channels": {"Stable": {"version": "123", "downloads": {
            "chrome": [{"platform": target, "url": download_url}]
        }}}
    }).encode()

    def opener(request, timeout):
        return Response(metadata if request.full_url == metadata_url else b"not a zip")

    with pytest.raises(ChromeInstallError, match="无法解压"):
        install_project_chrome(tmp_path, metadata_url=metadata_url, opener=opener)

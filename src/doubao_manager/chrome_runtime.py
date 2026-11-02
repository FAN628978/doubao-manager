from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
import ctypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


CHROME_METADATA_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)


class ChromeInstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChromeInstallation:
    executable: Path
    version: str
    platform: str
    source_url: str
    sha256: str
    installed: bool


def chrome_platform(system: str | None = None, machine: str | None = None) -> str:
    system_name = (system or platform.system()).lower()
    architecture = (machine or platform.machine()).lower()
    if system_name == "windows":
        return "win64" if architecture in {"amd64", "x86_64", "arm64", "aarch64"} else "win32"
    if system_name == "darwin":
        return "mac-arm64" if architecture in {"arm64", "aarch64"} else "mac-x64"
    if system_name == "linux" and architecture in {"amd64", "x86_64"}:
        return "linux64"
    raise ChromeInstallError(f"当前操作系统或架构不受 Chrome for Testing 支持：{system_name}/{architecture}")


def _executable_name() -> str:
    return "chrome.exe" if platform.system().lower() == "windows" else "chrome"


def _read_manifest(project_root: Path) -> dict:
    manifest = project_root / "browser" / "chrome" / "install.json"
    if not manifest.is_file():
        return {}
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def resolve_project_chrome(project_root: Path) -> Path | None:
    """Resolve the legacy portable Chrome first, then the managed CfT install."""
    root = Path(project_root).resolve()
    legacy = root / "browser" / "chrome.exe"
    if legacy.is_file():
        return legacy
    manifest = _read_manifest(root)
    relative = str(manifest.get("executable") or "").strip()
    if relative:
        candidate = (root / relative).resolve()
        managed_root = (root / "browser" / "chrome").resolve()
        if candidate.is_file() and (candidate == managed_root or managed_root in candidate.parents):
            return candidate
    managed_root = root / "browser" / "chrome"
    if managed_root.is_dir():
        candidates = sorted(managed_root.glob(f"*/**/{_executable_name()}"), reverse=True)
        if candidates:
            return candidates[0]
    return None


def browser_version(executable: Path) -> str:
    if platform.system().lower() == "windows":
        try:
            size = ctypes.windll.version.GetFileVersionInfoSizeW(str(executable), None)
            if not size:
                raise OSError("文件没有版本资源")
            buffer = ctypes.create_string_buffer(size)
            if not ctypes.windll.version.GetFileVersionInfoW(
                str(executable), 0, size, buffer
            ):
                raise OSError("无法读取版本资源")
            value = ctypes.c_void_p()
            length = ctypes.c_uint()
            if not ctypes.windll.version.VerQueryValueW(
                buffer, "\\", ctypes.byref(value), ctypes.byref(length)
            ):
                raise OSError("版本资源缺少固定信息")

            class FixedFileInfo(ctypes.Structure):
                _fields_ = [
                    ("signature", ctypes.c_uint32),
                    ("struct_version", ctypes.c_uint32),
                    ("file_version_ms", ctypes.c_uint32),
                    ("file_version_ls", ctypes.c_uint32),
                    ("product_version_ms", ctypes.c_uint32),
                    ("product_version_ls", ctypes.c_uint32),
                ]

            info = ctypes.cast(value, ctypes.POINTER(FixedFileInfo)).contents
            parts = (
                info.product_version_ms >> 16,
                info.product_version_ms & 0xFFFF,
                info.product_version_ls >> 16,
                info.product_version_ls & 0xFFFF,
            )
            if any(parts):
                return ".".join(str(part) for part in parts)
        except (AttributeError, OSError, ValueError):
            pass
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ChromeInstallError(f"无法检测项目 Chrome 版本：{exc}") from exc
    output = (completed.stdout or completed.stderr or "").strip()
    version = next((part for part in output.split() if part[:1].isdigit()), "")
    if not version:
        raise ChromeInstallError(f"项目 Chrome 返回了无法识别的版本信息：{output[:200]}")
    return version


def _read_json(url: str, opener: Callable = urllib.request.urlopen) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "doubao-manager-chrome-installer/1"})
    try:
        with opener(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise ChromeInstallError(f"无法获取 Chrome for Testing 版本信息：{exc}") from exc


def latest_stable_download(metadata: dict, target_platform: str) -> tuple[str, str]:
    stable = (metadata.get("channels") or {}).get("Stable") or {}
    version = str(stable.get("version") or "").strip()
    downloads = (stable.get("downloads") or {}).get("chrome") or []
    url = next(
        (str(item.get("url") or "") for item in downloads if item.get("platform") == target_platform),
        "",
    )
    if not version or not url:
        raise ChromeInstallError(f"稳定版元数据中没有 {target_platform} 的 Chrome 下载地址")
    return version, url


def _download(url: str, destination: Path, opener: Callable) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "doubao-manager-chrome-installer/1"})
    digest = hashlib.sha256()
    downloaded = 0
    try:
        with opener(request, timeout=120) as response, destination.open("wb") as output:
            expected = int(response.headers.get("Content-Length") or 0)
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
    except Exception as exc:
        raise ChromeInstallError(f"下载项目 Chrome 失败：{exc}") from exc
    if downloaded == 0:
        raise ChromeInstallError("下载项目 Chrome 失败：服务器返回了空文件")
    if expected and downloaded != expected:
        raise ChromeInstallError(f"项目 Chrome 下载不完整：应为 {expected} 字节，实际 {downloaded} 字节")
    return digest.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as package:
            corrupt = package.testzip()
            if corrupt:
                raise ChromeInstallError(f"项目 Chrome 压缩包校验失败：{corrupt}")
            destination_root = destination.resolve()
            for member in package.infolist():
                target = (destination / member.filename).resolve()
                if target != destination_root and destination_root not in target.parents:
                    raise ChromeInstallError("项目 Chrome 压缩包包含不安全路径，已停止安装")
            package.extractall(destination)
    except ChromeInstallError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise ChromeInstallError(f"无法解压项目 Chrome：{exc}") from exc


def install_project_chrome(
    project_root: Path,
    *,
    force: bool = False,
    metadata_url: str = CHROME_METADATA_URL,
    opener: Callable = urllib.request.urlopen,
    version_probe: Callable[[Path], str] = browser_version,
) -> ChromeInstallation:
    root = Path(project_root).resolve()
    target_platform = chrome_platform()
    metadata = _read_json(metadata_url, opener)
    version, download_url = latest_stable_download(metadata, target_platform)
    current = resolve_project_chrome(root)
    if current is not None and not force:
        try:
            current_version = version_probe(current)
        except ChromeInstallError:
            current_version = ""
        if current_version == version or current == root / "browser" / "chrome.exe":
            manifest = _read_manifest(root)
            return ChromeInstallation(
                current,
                current_version or str(manifest.get("version") or "legacy"),
                str(manifest.get("platform") or target_platform),
                str(manifest.get("source_url") or "legacy"),
                str(manifest.get("sha256") or ""),
                False,
            )

    chrome_root = root / "browser" / "chrome"
    chrome_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".install-", dir=chrome_root))
    archive = work / "chrome.zip"
    extracted = work / "unpacked"
    try:
        sha256 = _download(download_url, archive, opener)
        extracted.mkdir()
        _safe_extract(archive, extracted)
        executable = next(extracted.glob(f"**/{_executable_name()}"), None)
        if executable is None or not executable.is_file():
            raise ChromeInstallError("项目 Chrome 压缩包中缺少浏览器程序")
        detected_version = version_probe(executable)
        if detected_version != version:
            raise ChromeInstallError(
                f"项目 Chrome 版本校验失败：期望 {version}，实际 {detected_version}"
            )
        version_dir = chrome_root / version
        if not version_dir.exists():
            os.replace(extracted, version_dir)
        installed_executable = next(version_dir.glob(f"**/{_executable_name()}"), None)
        if installed_executable is None:
            raise ChromeInstallError("项目 Chrome 安装完成后无法定位浏览器程序")
        relative = installed_executable.relative_to(root)
        manifest = {
            "version": version,
            "platform": target_platform,
            "source_url": download_url,
            "sha256": sha256,
            "executable": relative.as_posix(),
        }
        (chrome_root / "install.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return ChromeInstallation(
            installed_executable, version, target_platform, download_url, sha256, True
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

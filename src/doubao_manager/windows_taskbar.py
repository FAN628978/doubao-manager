from __future__ import annotations

import ctypes
import os
import time
from ctypes import POINTER, byref, c_byte, c_long, c_ulong, c_ushort, c_void_p, c_wchar_p
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    from ctypes import WINFUNCTYPE
except ImportError:  # pragma: no cover - Windows-only API
    WINFUNCTYPE = None


_IS_WINDOWS = os.name == "nt"
_ICON_SIZES = ((16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256))
_BADGE_COLORS = (
    "#b8f36b",
    "#7fc8ff",
    "#ffd166",
    "#ff9f80",
    "#d8a7ff",
    "#7ee4cf",
    "#f3a6c8",
    "#c4d37d",
)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", c_ulong),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", c_byte * 8),
    ]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", c_ulong)]


class _PROPVALUE(ctypes.Union):
    _fields_ = [
        ("pwszVal", c_wchar_p),
        ("pointer", c_void_p),
        ("_padding", c_byte * 16),
    ]


class _PROPVARIANT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("vt", c_ushort),
        ("reserved1", c_ushort),
        ("reserved2", c_ushort),
        ("reserved3", c_ushort),
        ("value", _PROPVALUE),
    ]


def _guid(value: str) -> _GUID:
    parts = value.strip("{}").split("-")
    tail = bytes.fromhex("".join(parts[3:]))
    result = _GUID()
    result.Data1 = int(parts[0], 16)
    result.Data2 = int(parts[1], 16)
    result.Data3 = int(parts[2], 16)
    for index, byte in enumerate(tail):
        result.Data4[index] = byte
    return result


_APP_USER_MODEL_FMTID = _guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3")
_PKEY_APP_USER_MODEL_ICON = _PROPERTYKEY(_APP_USER_MODEL_FMTID, 3)
_PKEY_APP_USER_MODEL_ID = _PROPERTYKEY(_APP_USER_MODEL_FMTID, 5)
_IID_PROPERTY_STORE = _guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
_VT_LPWSTR = 31
_HRESULT = c_long

_user32 = None
_shell32 = None
_ole32 = None


def account_title_prefix(sequence: int) -> str:
    return f"[账号 {int(sequence):02d}] "


def account_taskbar_app_id(sequence: int) -> str:
    return f"DoubaoAccountDesk.Account.{int(sequence):02d}"


def ensure_account_icon(sequence: int, icon_dir: str | Path) -> Path:
    """Create a multi-size account-number icon for the Windows taskbar."""
    sequence = int(sequence)
    if sequence < 1:
        raise ValueError("account sequence must be positive")

    icon_dir = Path(icon_dir)
    icon_dir.mkdir(parents=True, exist_ok=True)
    icon_path = icon_dir / f"account-{sequence:02d}.ico"
    if icon_path.is_file() and icon_path.stat().st_size:
        return icon_path

    label = f"{sequence:02d}"
    canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    fill = _BADGE_COLORS[(sequence - 1) % len(_BADGE_COLORS)]
    draw.rounded_rectangle(
        (8, 8, 248, 248),
        radius=52,
        fill=fill,
        outline="#18200f",
        width=8,
    )

    font_size = 142 if len(label) <= 2 else 96 if len(label) == 3 else 72
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\segoeuib.ttf", font_size)
    except OSError:
        try:
            font = ImageFont.load_default(size=font_size)
        except TypeError:
            font = ImageFont.load_default()
    bounds = draw.textbbox((0, 0), label, font=font)
    position = (
        (256 - (bounds[2] - bounds[0])) / 2 - bounds[0],
        (256 - (bounds[3] - bounds[1])) / 2 - bounds[1],
    )
    draw.text(position, label, font=font, fill="#10150c")
    canvas.save(icon_path, format="ICO", sizes=_ICON_SIZES)
    return icon_path


def _windows_api():
    global _user32, _shell32, _ole32
    if not _IS_WINDOWS:
        raise OSError("Windows taskbar integration is only available on Windows")
    if _user32 is not None:
        return _user32, _shell32, _ole32

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _ole32 = ctypes.WinDLL("ole32", use_last_error=True)

    _user32.EnumWindows.argtypes = [WINFUNCTYPE(ctypes.c_bool, c_void_p, c_void_p), c_void_p]
    _user32.EnumWindows.restype = ctypes.c_bool
    _user32.IsWindowVisible.argtypes = [c_void_p]
    _user32.IsWindowVisible.restype = ctypes.c_bool
    _user32.GetWindowTextLengthW.argtypes = [c_void_p]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [c_void_p, c_wchar_p, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _shell32.SHGetPropertyStoreForWindow.argtypes = [c_void_p, POINTER(_GUID), POINTER(c_void_p)]
    _shell32.SHGetPropertyStoreForWindow.restype = _HRESULT
    _ole32.CoInitializeEx.argtypes = [c_void_p, c_ulong]
    _ole32.CoInitializeEx.restype = _HRESULT
    return _user32, _shell32, _ole32


def _com_call(obj: c_void_p, index: int, argument_types: list, *arguments, restype=_HRESULT):
    vtable = ctypes.cast(obj, POINTER(c_void_p))[0]
    address = ctypes.cast(vtable, POINTER(c_void_p))[index]
    method = WINFUNCTYPE(restype, c_void_p, *argument_types)(address)
    return method(obj, *arguments)


def _set_string_property(store: c_void_p, key: _PROPERTYKEY, value: str) -> bool:
    prop = _PROPVARIANT()
    prop.vt = _VT_LPWSTR
    prop.pwszVal = value
    result = _com_call(
        store,
        6,
        [POINTER(_PROPERTYKEY), POINTER(_PROPVARIANT)],
        byref(key),
        byref(prop),
    )
    return result >= 0


def _apply_window_identity(hwnd: c_void_p, sequence: int, icon_path: Path) -> bool:
    _, shell32, ole32 = _windows_api()
    ole32.CoInitializeEx(None, 0x2)
    store = c_void_p()
    try:
        result = shell32.SHGetPropertyStoreForWindow(
            hwnd,
            byref(_IID_PROPERTY_STORE),
            byref(store),
        )
        if result < 0 or not store.value:
            return False
        if not _set_string_property(
            store,
            _PKEY_APP_USER_MODEL_ICON,
            f"{icon_path.resolve()},0",
        ):
            return False
        if not _set_string_property(
            store,
            _PKEY_APP_USER_MODEL_ID,
            account_taskbar_app_id(sequence),
        ):
            return False
        return _com_call(store, 7, []) >= 0
    finally:
        if store.value:
            _com_call(store, 2, [])


def _find_windows(title_prefix: str) -> list[c_void_p]:
    user32, _, _ = _windows_api()
    matches: list[c_void_p] = []

    @WINFUNCTYPE(ctypes.c_bool, c_void_p, c_void_p)
    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, len(title))
        if title.value.startswith(title_prefix):
            matches.append(hwnd)
        return True

    user32.EnumWindows(visit, None)
    return matches


def apply_account_taskbar_identity(
    sequence: int,
    title_prefix: str,
    icon_dir: str | Path,
    *,
    timeout: float = 4.0,
) -> bool:
    """Badge Chrome windows with the account number and keep each account separate."""
    if not _IS_WINDOWS or int(sequence) < 1:
        return False
    try:
        icon_path = ensure_account_icon(sequence, icon_dir)
    except Exception:
        return False

    applied: set[int] = set()
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        windows = _find_windows(title_prefix)
        for hwnd in windows:
            handle = int(hwnd or 0)
            if handle and handle not in applied and _apply_window_identity(hwnd, sequence, icon_path):
                applied.add(handle)
        if applied and all(int(hwnd or 0) in applied for hwnd in windows):
            return True
        if time.monotonic() >= deadline:
            return bool(applied)
        time.sleep(0.1)

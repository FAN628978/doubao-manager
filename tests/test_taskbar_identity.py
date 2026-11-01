import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doubao_manager.windows_taskbar import (  # noqa: E402
    account_taskbar_app_id,
    account_title_prefix,
    ensure_account_icon,
)


def test_account_taskbar_identity_uses_stable_sequence():
    assert account_title_prefix(1) == "[账号 01] "
    assert account_title_prefix(128) == "[账号 128] "
    assert account_taskbar_app_id(1) == "DoubaoAccountDesk.Account.01"
    assert account_taskbar_app_id(2) != account_taskbar_app_id(1)


def test_account_icon_contains_taskbar_sizes(tmp_path):
    icon_path = ensure_account_icon(12, tmp_path)

    assert icon_path.name == "account-12.ico"
    with Image.open(icon_path) as icon:
        assert {(16, 16), (24, 24), (32, 32), (48, 48), (256, 256)} <= icon.ico.sizes()
    assert ensure_account_icon(12, tmp_path) == icon_path

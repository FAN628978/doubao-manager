"""Compatibility facade for the split web-video package.

The implementation lives in :mod:`doubao_manager.web`. Existing imports from
``web_api`` continue to work, including monkeypatched browser helpers in tests.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager

import httpx

from doubao_manager.web import api as _api_impl
from doubao_manager.web import automation as _automation_impl
from doubao_manager.web.automation import *
from doubao_manager.web.config import *
from doubao_manager.web.models import *
from doubao_manager.web.quota import *
from doubao_manager.web.utils import *


_SYNCED_AUTOMATION_NAMES = (
    "perform",
    "persisted_url",
    "read_state",
    "read_composer_state",
    "click_result_card",
    "download_result_video",
    "download_media",
    "media_duration_seconds",
    "verify_video_file",
    "send_composer_text",
    "wait_confirm_send",
    "dismiss_blocking_dialogs",
    "ensure_chat_mode",
    "work_mode_active",
    "set_ui_option",
    "select_model",
    "upload_slot",
)
_ORIGINAL_AUTOMATION = {
    name: getattr(_automation_impl, name) for name in _SYNCED_AUTOMATION_NAMES
}


async def perform(*args, **kwargs):
    with _automation_overrides(skip={"perform"}):
        return await _ORIGINAL_AUTOMATION["perform"](*args, **kwargs)


async def persisted_url(*args, **kwargs):
    with _automation_overrides(skip={"persisted_url"}):
        return await _ORIGINAL_AUTOMATION["persisted_url"](*args, **kwargs)


_FACADE_WRAPPERS = {"perform": perform, "persisted_url": persisted_url}


@contextmanager
def _automation_overrides(skip: set[str] | None = None):
    """Expose facade monkeypatches to the implementation only for this call."""
    facade = sys.modules[__name__]
    saved = {}
    try:
        for name in _SYNCED_AUTOMATION_NAMES:
            if skip and name in skip:
                continue
            value = getattr(facade, name)
            if _FACADE_WRAPPERS.get(name) is value:
                continue
            saved[name] = getattr(_automation_impl, name)
            setattr(_automation_impl, name, value)
        yield
    finally:
        for name, value in saved.items():
            setattr(_automation_impl, name, value)


def mount_video_api(*args, **kwargs):
    # Keep the facade monkeypatch-friendly without mutating implementation globals
    # permanently. Function wrappers apply overrides only for their own call.
    for name in _SYNCED_AUTOMATION_NAMES:
        value = getattr(sys.modules[__name__], name)
        if _FACADE_WRAPPERS.get(name) is value:
            value = _ORIGINAL_AUTOMATION[name]
        setattr(_api_impl, name, value)
    _api_impl.httpx = httpx
    return _api_impl.mount_video_api(*args, **kwargs)

from __future__ import annotations

import asyncio
import os
import random
import re
from datetime import datetime, timezone

def ts(): return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe(s): return re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", s).strip("._")[:64] or "none"


def _compact_title_part(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:max(1, limit - 3)].rstrip() + "..."


_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _cn_number_to_int(value: str) -> int | None:
    total = section = number = 0
    for char in value:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
            continue
        unit = _CN_UNITS.get(char)
        if unit is None:
            return None
        if unit == 10000:
            section = (section + number) * unit
            total += section
            section = number = 0
        else:
            section += (number or 1) * unit
            number = 0
    return total + section + number


def _title_number(value: str) -> int | None:
    text = str(value or "")
    match = re.search(r"\d+", text)
    if match:
        return int(match.group())
    match = re.search(r"[零〇一二两三四五六七八九十百千万]+", text)
    return _cn_number_to_int(match.group()) if match else None


def concise_job_title(
    project_id: str = "",
    episode_id: str = "",
    shot_id: str = "",
    *,
    fallback: str = "",
) -> str:
    """Build the compact task title shown in the managed browser tab."""
    project = re.sub(r"\s+", " ", str(project_id or "")).strip()[:4].rstrip("-—/ ")
    episode_number = _title_number(episode_id)
    shot_number = _title_number(shot_id)
    episode = f"{episode_number}集" if episode_number is not None else _compact_title_part(episode_id, 10)
    shot = f"#{shot_number}" if shot_number is not None else _compact_title_part(shot_id, 12)
    parts = [project, episode, shot]
    return "-".join(part for part in parts if part) or _compact_title_part(fallback, 36) or "分镜"


async def set_job_page_title(page, title: str) -> None:
    """Apply and persist a task title for the lifetime of this browser tab."""
    if page is None or not title:
        return
    try:
        await page.evaluate(
            """title => {
                window.__doubaoDeskTaskTitle = title;
                try { sessionStorage.setItem('__doubaoDeskTaskTitle', title); } catch (e) {}
                document.title = title;
            }""",
            title,
        )
    except Exception:
        pass


async def automatic_step_delay(submit_mode: str = "auto") -> int:
    """Optionally pace workflow stages; defaults to no delay."""
    if submit_mode != "auto" or os.getenv("DOUBAO_TEST_MODE") == "1":
        return 0
    try:
        fixed = max(0, int(os.getenv("DOUBAO_STEP_DELAY_SECONDS", "0")))
    except ValueError:
        fixed = 0
    try:
        minimum = max(0, int(os.getenv("DOUBAO_STEP_DELAY_MIN_SECONDS", str(fixed))))
        maximum = max(0, int(os.getenv("DOUBAO_STEP_DELAY_MAX_SECONDS", str(fixed))))
    except ValueError:
        minimum = maximum = fixed
    if maximum < minimum:
        minimum, maximum = maximum, minimum
    seconds = random.randint(minimum, maximum)
    await asyncio.sleep(seconds)
    return seconds


def poll_interval(started_at: float | None = None) -> float:
    """Return adaptive polling delay: 1s early, then 2/4/8s."""
    override = os.getenv("DOUBAO_POLL_SECONDS")
    if override is not None:
        try:
            return max(0.0, float(override))
        except ValueError:
            pass
    elapsed = max(0.0, asyncio.get_running_loop().time() - (started_at or 0.0))
    if elapsed < 3:
        return 1.0
    if elapsed < 10:
        return 2.0
    if elapsed < 30:
        return 4.0
    return 8.0


CONFIRMATION_RESULT_POLL_DELAYS = (120.0, 60.0, 30.0, 15.0, 10.0)


def confirmation_result_poll_interval(probe_index: int) -> float:
    """Delay before each result probe after the confirmation answer was sent.

    The first probe waits 120 seconds, followed by 60/30/15/10-second gaps and
    then a steady 10-second cadence. DOUBAO_POLL_SECONDS remains the test and
    operational override shared with the rest of the automation flow.
    """
    override = os.getenv("DOUBAO_POLL_SECONDS")
    if override is not None:
        try:
            return max(0.0, float(override))
        except ValueError:
            pass
    index = max(0, int(probe_index))
    return CONFIRMATION_RESULT_POLL_DELAYS[
        min(index, len(CONFIRMATION_RESULT_POLL_DELAYS) - 1)
    ]


async def adaptive_poll(page=None, started_at: float | None = None) -> float:
    seconds = poll_interval(started_at)
    if page is not None and hasattr(page, "wait_for_timeout"):
        await page.wait_for_timeout(seconds * 1000)
    else:
        await asyncio.sleep(seconds)
    return seconds

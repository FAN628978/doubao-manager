from __future__ import annotations

import asyncio
import os
import random
import re
from datetime import datetime, timezone

def ts(): return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe(s): return re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", s).strip("._")[:64] or "none"


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


async def adaptive_poll(page=None, started_at: float | None = None) -> float:
    seconds = poll_interval(started_at)
    if page is not None and hasattr(page, "wait_for_timeout"):
        await page.wait_for_timeout(seconds * 1000)
    else:
        await asyncio.sleep(seconds)
    return seconds

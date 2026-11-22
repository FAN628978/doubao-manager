"""Export task preferences without account data or task history."""
from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path


SETTING_KEYS = (
    "accepting_tasks",
    "auto_dispatch_enabled",
    "auto_dispatch_interval_seconds",
    "auto_switch_on_exception",
    "default_confirmation_preset_id",
    "ignore_quota_check",
    "processing_limit",
    "processing_limit_v2",
    "scheduled_dispatch_enabled",
    "scheduled_dispatch_time",
    "video_execution_service_enabled",
    "video_model",
    "video_submit_mode",
)


def export_settings(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(destination)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        src.execute("BEGIN")
        placeholders = ",".join("?" for _ in SETTING_KEYS)
        settings = src.execute(
            f"SELECT key,value FROM local_settings WHERE key IN ({placeholders})",
            SETTING_KEYS,
        ).fetchall()
        presets = src.execute("SELECT * FROM confirmation_presets").fetchall()
        with closing(sqlite3.connect(destination)) as dst, dst:
            for table in ("local_settings", "confirmation_presets"):
                ddl = src.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()[0]
                dst.execute(ddl)
            dst.executemany("INSERT INTO local_settings VALUES(?,?)", settings)
            if presets:
                placeholders = ",".join("?" for _ in presets[0])
                dst.executemany(
                    f"INSERT INTO confirmation_presets VALUES({placeholders})", presets
                )
    print(f"Exported {len(settings)} task settings and {len(presets)} confirmation presets.")


if __name__ == "__main__":
    export_settings(Path(sys.argv[1]), Path(sys.argv[2]))

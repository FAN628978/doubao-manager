from __future__ import annotations

import multiprocessing
import os
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

SOURCE_DIR = Path(__file__).resolve().parent / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from app import app


HOST = "127.0.0.1"
PORT = int(os.getenv("DOUBAO_PORT", "8765"))


def open_manager() -> None:
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    threading.Timer(1.5, open_manager).start()
    uvicorn.run(app, host=HOST, port=PORT)

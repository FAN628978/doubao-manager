from __future__ import annotations

import multiprocessing
import os
import threading
import webbrowser

import uvicorn

from app import app


HOST = "127.0.0.1"
PORT = int(os.getenv("DOUBAO_PORT", "8765"))


def open_manager() -> None:
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    threading.Timer(1.5, open_manager).start()
    uvicorn.run(app, host=HOST, port=PORT)

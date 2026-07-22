"""Local launcher: starts uvicorn and opens the app in a browser.

For hosting, run uvicorn directly (see Dockerfile) instead of this script.
"""
from __future__ import annotations

import threading
import webbrowser

import uvicorn

HOST = "127.0.0.1"
PORT = 8000


def _open_browser() -> None:
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    threading.Timer(1.2, _open_browser).start()
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)

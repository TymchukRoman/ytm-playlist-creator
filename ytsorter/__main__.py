"""Launcher: serve the app on 127.0.0.1 and open it in the default browser."""
import os
import socket
import threading
import time
import webbrowser

import uvicorn

from .app import build_default_app

HOST = "127.0.0.1"  # local only: never reachable from other machines
DEFAULT_PORT = 8765


def pick_port(preferred: int = DEFAULT_PORT) -> int:
    """`preferred` if it is free, otherwise a free port chosen by the OS."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise OSError("No free port available on 127.0.0.1")


def server_config(port: int, app=None) -> uvicorn.Config:
    return uvicorn.Config(app if app is not None else build_default_app(),
                          host=HOST, port=port, log_level="warning")


def _open_browser_when_ready(server: uvicorn.Server, url: str) -> None:
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if server.started:
        webbrowser.open(url)


def main() -> None:
    port = pick_port()
    url = f"http://{HOST}:{port}/"
    server = uvicorn.Server(server_config(port))
    print(f"YT Music Sorter is running at {url}")
    print("Close this window to stop the app.")
    if not os.environ.get("YTSORTER_NO_BROWSER"):
        threading.Thread(target=_open_browser_when_ready, args=(server, url), daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()

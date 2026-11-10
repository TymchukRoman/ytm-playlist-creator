"""Launcher: serve the app on 127.0.0.1 and open it in the default browser.

Only one copy runs per data folder: a second start opens the running one instead, because
two job runners on the same saved job could add songs twice. An OS file lock decides which
copy runs (the OS releases it when the process ends, even after a crash); instance.json only
tells a second start where the running copy is.
"""
import json
import os
import socket
import threading
import time
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path

import uvicorn

from .app import build_default_app
from .paths import data_dir
from .store import read_json, write_json_atomic

HOST = "127.0.0.1"  # local only: never reachable from other machines
DEFAULT_PORT = 8765
ANSWER_TIMEOUT = 30.0  # /api/status may wait for the Claude sign-in check (up to 20 s)
FIND_RUNNING_FOR = 30.0  # how long a second start waits for the running copy to answer


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


def _url(port: int) -> str:
    return f"http://{HOST}:{port}/"


# --- one running copy per data folder ----------------------------------------------------------

def instance_file(demo: bool) -> Path:
    """Where the running copy notes its pid and port (demo mode keeps its own)."""
    return (data_dir() / "demo" if demo else data_dir()) / "instance.json"


def pid_alive(pid: int) -> bool:
    """Whether a process with this id exists (never signals or stops it)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def lock_file(demo: bool) -> Path:
    return instance_file(demo).with_name("instance.lock")


def acquire_lock(path: Path):
    """Take the one-copy lock without waiting. Returns the open file holding it (keep it open
    while the app runs), or None when another copy holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")  # noqa: SIM115 — must stay open for the lock's lifetime
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 127.0.0.1 never via a proxy


def app_answers(port: int, timeout: float = ANSWER_TIMEOUT) -> bool:
    """Whether this app (not some other program) answers GET /api/status on `port`."""
    try:
        with _NO_PROXY.open(f"{_url(port)}api/status", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "claude" in data and "youtube" in data


def running_instance_url(path: Path, alive: Callable[[int], bool] | None = None,
                         answers: Callable[[int], bool] | None = None) -> str | None:
    """The URL of a copy that is really running (its process is alive AND its port answers).

    None means start normally: no file, an unreadable one, or a stale one (left by a closed
    window) is simply taken over.
    """
    alive = alive or pid_alive
    answers = answers or app_answers
    try:
        info = read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    pid, port = info.get("pid"), info.get("port")
    if not isinstance(pid, int) or not isinstance(port, int) or isinstance(port, bool):
        return None
    if pid == os.getpid() or not alive(pid) or not answers(port):
        return None
    return _url(port)


def write_instance(path: Path, port: int) -> None:
    write_json_atomic(path, {"pid": os.getpid(), "port": port})


def remove_instance(path: Path) -> None:
    """Remove the file on shutdown, unless another copy has taken it over since."""
    try:
        info = read_json(path)
    except (OSError, ValueError):
        return
    if isinstance(info, dict) and info.get("pid") == os.getpid():
        path.unlink(missing_ok=True)


# --- start ----------------------------------------------------------------------------------------

def _open_browser_when_ready(server: uvicorn.Server, url: str) -> None:
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if server.started:
        webbrowser.open(url)


def find_running(marker: Path, wait: float = FIND_RUNNING_FOR) -> str | None:
    """The running copy's URL, waiting a little in case it is still starting."""
    deadline = time.monotonic() + wait
    while True:
        url = running_instance_url(marker, pid_alive, app_answers)
        if url or time.monotonic() >= deadline:
            return url
        time.sleep(0.5)


def main() -> None:
    open_browser = not os.environ.get("YTSORTER_NO_BROWSER")
    demo = os.environ.get("YTSORTER_DEMO") == "1"
    marker = instance_file(demo)
    lock = acquire_lock(lock_file(demo))
    if lock is None:  # another copy runs (or is starting) with this data folder
        running = find_running(marker)
        if running:
            print(f"YT Music Sorter is already running at {running}"
                  + (" — opening it in your browser." if open_browser else ". Use that one."), flush=True)
            if open_browser:
                webbrowser.open(running)
        else:
            print("YT Music Sorter is already running in another window, but it isn't answering. "
                  "Close the other window, then start the app again.", flush=True)
        return

    port = pick_port()
    url = _url(port)
    server = uvicorn.Server(server_config(port))  # builds the app (demo mode resets demo/ first)
    write_instance(marker, port)
    print(f"YT Music Sorter is running at {url}")
    print("Close this window to stop the app.", flush=True)
    if open_browser:
        threading.Thread(target=_open_browser_when_ready, args=(server, url), daemon=True).start()
    try:
        server.run()
    finally:
        remove_instance(marker)
        lock.close()


if __name__ == "__main__":
    main()

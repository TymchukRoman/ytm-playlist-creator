"""Locations of all user data. Nothing user-specific lives inside the repo."""
import os
from pathlib import Path

from platformdirs import user_data_dir


def data_dir() -> Path:
    override = os.environ.get("YTSORTER_DATA_DIR")
    path = Path(override) if override else Path(user_data_dir("yt-music-sorter", appauthor=False))
    # Owner-only on POSIX (mode is applied when the folder is created; best effort, Windows
    # keeps its per-user profile permissions).
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def youtube_auth_file() -> Path:
    return data_dir() / "youtube_auth.json"


def playlists_dir() -> Path:
    return data_dir() / "playlists"


def job_file() -> Path:
    return data_dir() / "job.json"


def songs_file() -> Path:
    return data_dir() / "songs.json"


def settings_file() -> Path:
    return data_dir() / "settings.json"


def claude_work_dir() -> Path:
    """An empty folder used as the Claude CLI's working directory (created on demand)."""
    path = data_dir() / "claude-work"
    path.mkdir(exist_ok=True)
    return path

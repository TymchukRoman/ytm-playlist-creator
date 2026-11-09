"""Locations of all user data. Nothing user-specific lives inside the repo."""
import os
from pathlib import Path

from platformdirs import user_data_dir


def data_dir() -> Path:
    override = os.environ.get("YTSORTER_DATA_DIR")
    path = Path(override) if override else Path(user_data_dir("yt-music-sorter", appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def youtube_auth_file() -> Path:
    return data_dir() / "youtube_auth.json"


def playlists_dir() -> Path:
    return data_dir() / "playlists"


def job_file() -> Path:
    return data_dir() / "job.json"

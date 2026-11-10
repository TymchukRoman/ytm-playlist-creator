"""The only module that talks to YouTube Music.

Safety rule: this wrapper can only create playlists and add songs. It has no way to
remove songs, unlike, rate, or delete playlists. `disconnect` only deletes the local
sign-in file.
"""
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ytmusicapi

from .models import Song

CHUNK_SIZE = 50

_AUTH_RE = re.compile(r"\b401\b|\b403\b|sign[ -]?in|authori[sz]ation|authentication", re.IGNORECASE)
_NOT_FOUND_RE = re.compile(r"\b404\b|not found|does not exist|no such playlist", re.IGNORECASE)


class YouTubeAuthError(Exception):
    """Not connected, or the YouTube Music sign-in expired."""


class YouTubeError(Exception):
    """Any other YouTube Music failure."""


class PlaylistMissingError(YouTubeError):
    """The playlist no longer exists on YouTube Music."""


def _translate(exc: Exception) -> Exception:
    if isinstance(exc, YouTubeError | YouTubeAuthError):
        return exc
    text = str(exc)
    if _AUTH_RE.search(text):
        return YouTubeAuthError(f"YouTube Music sign-in expired or invalid: {text}")
    return YouTubeError(text or exc.__class__.__name__)


class YTMusicClient:
    def __init__(self, auth_file: Path, factory: Callable[[str], Any] = ytmusicapi.YTMusic):
        self.auth_file = Path(auth_file)
        self._factory = factory
        self._api: Any = None

    def is_connected(self) -> bool:
        return self.auth_file.exists()

    def connect(self, headers_raw: str) -> None:
        """Verify new credentials in a temp file; only replace the saved sign-in on success."""
        tmp = self.auth_file.with_name(self.auth_file.name + ".new")
        try:
            content = ytmusicapi.setup(headers_raw=headers_raw)
            self._write_private(tmp, content)
            self._factory(str(tmp)).get_liked_songs(limit=1)
            os.replace(tmp, self.auth_file)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise YouTubeAuthError(
                "Those sign-in details didn't work. Copy the request headers again from your "
                "browser while signed in to music.youtube.com."
            ) from exc
        self._api = None  # rebuilt from the new credentials on next use

    @staticmethod
    def _write_private(path: Path, content: str) -> None:
        """Write owner-only (the file holds session cookies)."""
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best effort; the per-user AppData folder is already private on Windows

    def disconnect(self) -> None:
        self._api = None
        self.auth_file.unlink(missing_ok=True)

    def _client(self) -> Any:
        if not self.is_connected():
            raise YouTubeAuthError("Not connected to YouTube Music.")
        if self._api is None:
            try:
                self._api = self._factory(str(self.auth_file))
            except Exception as exc:
                raise _translate(exc) from exc
        return self._api

    def get_liked_songs(self) -> list[Song]:
        api = self._client()
        try:
            tracks = api.get_liked_songs(limit=None)["tracks"]
        except Exception as exc:
            raise _translate(exc) from exc
        songs: list[Song] = []
        seen: set[str] = set()
        for t in tracks:
            vid = t.get("videoId")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            album = t.get("album")
            songs.append(Song(
                video_id=vid,
                title=t.get("title") or "",
                artists=[a["name"] for a in (t.get("artists") or []) if a.get("name")],
                album=album.get("name") if album else None,
                duration=t.get("duration"),
            ))
        return songs

    def create_playlist(self, name: str, description: str) -> str:
        api = self._client()
        try:
            # YouTube Music rejects < and > (checked for the name before Claude runs); the
            # description shown on YouTube just drops them.
            safe_description = description.replace("<", "").replace(">", "")
            result = api.create_playlist(name, safe_description, privacy_status="PRIVATE")
        except Exception as exc:
            raise _translate(exc) from exc
        if not isinstance(result, str) or not result:
            raise YouTubeError(f"YouTube Music didn't create the playlist: {result!r}")
        return result

    def add_songs(self, playlist_id: str, video_ids: list[str],
                  on_progress: Callable[[int, int], None]) -> None:
        api = self._client()
        total = len(video_ids)
        done = 0
        for start in range(0, total, CHUNK_SIZE):
            chunk = video_ids[start:start + CHUNK_SIZE]
            try:
                result = api.add_playlist_items(playlist_id, chunk, duplicates=False)
            except Exception as exc:
                err = _translate(exc)
                if type(err) is YouTubeError and _NOT_FOUND_RE.search(str(exc)):
                    raise PlaylistMissingError(str(exc)) from exc
                raise err from exc
            status = result.get("status", "") if isinstance(result, dict) else str(result)
            if "SUCCEEDED" not in status:
                raise YouTubeError(f"YouTube Music didn't add the songs: {result!r}")
            done += len(chunk)
            on_progress(done, total)

    def get_playlist_video_ids(self, playlist_id: str) -> set[str]:
        """Read-only: the video ids already on a playlist (tracks without a videoId are skipped)."""
        api = self._client()
        try:
            tracks = api.get_playlist(playlist_id, limit=None).get("tracks") or []
        except Exception as exc:
            err = _translate(exc)
            if type(err) is YouTubeError and _NOT_FOUND_RE.search(str(exc)):
                raise PlaylistMissingError(str(exc)) from exc
            raise err from exc
        return {t["videoId"] for t in tracks if t.get("videoId")}

    def playlist_exists(self, playlist_id: str) -> bool:
        api = self._client()
        try:
            api.get_playlist(playlist_id, limit=1)
            return True
        except Exception as exc:
            err = _translate(exc)
            if type(err) is YouTubeError and _NOT_FOUND_RE.search(str(exc)):
                return False
            raise err from exc

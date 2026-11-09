"""Test doubles used by the test suite and by the app's offline/demo wiring."""
import re
from collections.abc import Callable

from .claude_cli import ClaudeStatus, ClaudeUsageLimitError
from .models import Decision, Song, Verdict
from .ytm import CHUNK_SIZE, PlaylistMissingError, YouTubeAuthError, YouTubeError

Rules = Callable[[str, Song], tuple[Verdict, str]]


def _default_rule(description: str, song: Song) -> tuple[Verdict, str]:
    haystack = f"{song.title} {' '.join(song.artists)}".lower()
    for word in re.findall(r"\w+", description.lower()):
        if word in haystack:
            return "include", f"matched '{word}'"
    return "exclude", "no match"


class FakeClassifier:
    """Stands in for ClaudeClassifier. `fail_after=N` raises a usage-limit error on batch N+1."""

    def __init__(self, rules: Rules | None = None, fail_after: int | None = None):
        self.rules = rules or _default_rule
        self.fail_after = fail_after
        self.batches = 0

    def status(self) -> ClaudeStatus:
        return ClaudeStatus(True, True, "ok")

    def classify_batch(self, description: str, songs: list[Song]) -> list[Decision]:
        if self.fail_after is not None and self.batches >= self.fail_after:
            raise ClaudeUsageLimitError("Claude usage limit reached (fake)")
        self.batches += 1
        decisions = []
        for song in songs:
            verdict, reason = self.rules(description, song)
            decisions.append(Decision(song.video_id, verdict, reason))
        return decisions


class FakeYTMusic:
    """Stands in for YTMusicClient. Records writes; has no remove/unlike methods."""

    def __init__(self, liked: list[Song], connected: bool = True):
        self.liked = list(liked)
        self.created: dict[str, dict] = {}
        self.added: dict[str, list[str]] = {}
        self.deleted_playlists: set[str] = set()
        self.fail_next_add = 0
        self.fail_add_at_chunk: int | None = None  # 0-based chunk index that fails; one-shot
        self.expire_auth_after_chunk: int | None = None  # sign-in expires after this chunk; one-shot
        self._connected = connected
        self._expired = False
        self._counter = 0

    def expire_auth(self) -> None:
        """Every method (except is_connected/disconnect) raises YouTubeAuthError until connect()."""
        self._expired = True

    def _check_auth(self) -> None:
        if self._expired or not self._connected:
            raise YouTubeAuthError("YouTube Music sign-in expired (fake)")

    def is_connected(self) -> bool:
        return self._connected

    def connect(self, headers_raw: str) -> None:
        self._connected = True
        self._expired = False

    def disconnect(self) -> None:
        self._connected = False

    def get_liked_songs(self) -> list[Song]:
        self._check_auth()
        return list(self.liked)

    def create_playlist(self, name: str, description: str) -> str:
        self._check_auth()
        self._counter += 1
        pid = f"PLfake{self._counter}"
        self.created[pid] = {"name": name, "description": description, "privacy_status": "PRIVATE"}
        return pid

    def add_songs(self, playlist_id: str, video_ids: list[str],
                  on_progress: Callable[[int, int], None]) -> None:
        self._check_auth()
        if not self.playlist_exists(playlist_id):
            raise PlaylistMissingError(f"Playlist {playlist_id} not found (fake)")
        if self.fail_next_add > 0:
            self.fail_next_add -= 1
            raise YouTubeError("Couldn't add songs (fake)")
        total = len(video_ids)
        done = 0
        for index, start in enumerate(range(0, total, CHUNK_SIZE)):
            chunk = video_ids[start:start + CHUNK_SIZE]
            self._check_auth()  # like the real API: every chunk is its own request
            if self.fail_add_at_chunk == index:
                self.fail_add_at_chunk = None
                raise YouTubeError("Couldn't add songs (fake, mid-job)")
            existing = set(self.added.get(playlist_id, []))
            if any(vid in existing for vid in chunk):  # the real client sends duplicates=False
                raise YouTubeError("Some songs are already in the playlist (fake)")
            self.added.setdefault(playlist_id, []).extend(chunk)
            done += len(chunk)
            on_progress(done, total)
            if self.expire_auth_after_chunk == index:
                self.expire_auth_after_chunk = None
                self.expire_auth()

    def get_playlist_video_ids(self, playlist_id: str) -> set[str]:
        self._check_auth()
        if not self.playlist_exists(playlist_id):
            raise PlaylistMissingError(f"Playlist {playlist_id} not found (fake)")
        return set(self.added.get(playlist_id, []))

    def playlist_exists(self, playlist_id: str) -> bool:
        self._check_auth()
        return playlist_id in self.created and playlist_id not in self.deleted_playlists

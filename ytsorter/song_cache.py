"""What earlier scans learned about each liked song, shared by all playlists.

One entry per song (YouTube video id):
- `tags`: what the song is ("k-pop", "japanese", "anime opening", "naruto"), from Claude
  plus the topic of every playlist the song was put in.
- `surely_not`: playlist topics the song was checked against and does not fit
  (a K-pop song gets "ukrainian" after a Ukrainian scan), so it isn't asked about again.
- `playlists`: ids of this app's playlists the song was added to.

A new playlist whose description is one tag (its "topic") takes songs tagged with that
topic as fitting and songs with it in `surely_not` as not fitting, and asks Claude only
about the rest. Everything else (subtle or combined descriptions) still asks Claude.
"""
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .store import read_json, write_json_atomic

TAG_MAX_LEN = 40
TAGS_PER_SONG = 8
VOCABULARY_SIZE = 150  # most common tags sent to Claude so it reuses them


def normalize_tag(tag: object) -> str:
    """Lowercase, single-spaced, no surrounding punctuation; '' when nothing useful is left."""
    if not isinstance(tag, str):
        return ""
    text = re.sub(r"\s+", " ", tag.strip().lower())
    text = text.strip(" .,;:!?\"'`()[]{}#")
    return text[:TAG_MAX_LEN].strip()


def normalize_tags(tags: object, limit: int = TAGS_PER_SONG) -> list[str]:
    if not isinstance(tags, list | tuple):
        return []
    out: list[str] = []
    for tag in tags:
        t = normalize_tag(tag)
        if t and t not in out:
            out.append(t)
    return out[:limit]


@dataclass
class SongKnowledge:
    tags: list[str] = field(default_factory=list)
    surely_not: list[str] = field(default_factory=list)
    playlists: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"tags": list(self.tags), "surely_not": list(self.surely_not), "playlists": list(self.playlists)}

    @classmethod
    def from_dict(cls, d: object) -> "SongKnowledge":
        if not isinstance(d, dict):
            return cls()
        playlists = [p for p in d.get("playlists", []) if isinstance(p, str)] if isinstance(d.get("playlists"), list) else []
        return cls(normalize_tags(d.get("tags"), limit=64), normalize_tags(d.get("surely_not"), limit=256), playlists)


def _add(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _remove(items: list[str], value: str) -> None:
    if value in items:
        items.remove(value)


class SongCache:
    """songs.json in the data folder (`path=None`: in memory only, for tests)."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._songs: dict[str, SongKnowledge] | None = None

    def _all(self) -> dict[str, SongKnowledge]:
        if self._songs is None:
            data = None
            if self.path is not None:
                try:
                    data = read_json(self.path)
                except (OSError, ValueError):
                    data = None  # unreadable: start over rather than block every scan
            songs = data.get("songs") if isinstance(data, dict) else None
            self._songs = ({k: SongKnowledge.from_dict(v) for k, v in songs.items() if isinstance(k, str)}
                           if isinstance(songs, dict) else {})
        return self._songs

    def _save(self) -> None:
        if self.path is not None:
            write_json_atomic(self.path, {"songs": {k: v.to_dict() for k, v in self._all().items()}})

    def get(self, video_id: str) -> SongKnowledge | None:
        with self._lock:
            k = self._all().get(video_id)
            return SongKnowledge.from_dict(k.to_dict()) if k else None

    def vocabulary(self, limit: int = VOCABULARY_SIZE) -> list[str]:
        """The most used tags, most common first, so Claude reuses them instead of inventing synonyms."""
        with self._lock:
            counts = Counter(t for k in self._all().values() for t in k.tags)
        return [t for t, _ in counts.most_common(limit)]

    def learn(self, video_id: str, *, tags: list[str] = (), fits: str | None = None,
              not_fits: str | None = None, playlist_id: str | None = None) -> None:
        """Merge one song's knowledge (call `save()` after a batch of these)."""
        with self._lock:
            k = self._all().setdefault(video_id, SongKnowledge())
            for t in normalize_tags(list(tags)):
                _add(k.tags, t)
            if fits:
                _add(k.tags, fits)
                _remove(k.surely_not, fits)
            if not_fits:
                _add(k.surely_not, not_fits)
                _remove(k.tags, not_fits)
            if playlist_id:
                _add(k.playlists, playlist_id)

    def forget_playlist(self, playlist_id: str) -> None:
        with self._lock:
            for k in self._all().values():
                _remove(k.playlists, playlist_id)
            self._save()

    def save(self) -> None:
        with self._lock:
            self._save()

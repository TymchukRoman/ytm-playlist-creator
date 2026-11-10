"""Plain data types shared across the app."""
from dataclasses import dataclass, field
from typing import Literal

Verdict = Literal["include", "exclude", "unsure"]
Source = Literal["claude", "user"]


@dataclass(frozen=True)
class Song:
    video_id: str
    title: str
    artists: list[str]
    album: str | None = None
    duration: str | None = None

    def to_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "artists": list(self.artists),
            "album": self.album,
            "duration": self.duration,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Song":
        return cls(
            video_id=d["video_id"],
            title=d["title"],
            artists=list(d.get("artists", [])),
            album=d.get("album"),
            duration=d.get("duration"),
        )


@dataclass
class Decision:
    video_id: str
    verdict: Verdict
    reason: str
    tags: list[str] = field(default_factory=list)  # what the song is, from Claude (see song_cache)
    cached: bool = False  # decided from what earlier scans learned, not by asking Claude now


@dataclass
class SongEntry:
    title: str
    artists: list[str]
    verdict: Verdict
    reason: str
    source: Source

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "artists": list(self.artists),
            "verdict": self.verdict,
            "reason": self.reason,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SongEntry":
        return cls(
            title=d["title"],
            artists=list(d.get("artists", [])),
            verdict=d["verdict"],
            reason=d.get("reason", ""),
            source=d["source"],
        )


@dataclass
class PlaylistRecord:
    playlist_id: str
    name: str
    description: str
    created_at: str  # ISO-8601 UTC
    updated_at: str  # ISO-8601 UTC
    included: dict[str, SongEntry] = field(default_factory=dict)
    excluded: dict[str, SongEntry] = field(default_factory=dict)
    # The one song tag this playlist's description amounts to ("k-pop"), or None when it is
    # subtler than one tag. `topic_known` False: not worked out yet (records made before tags).
    topic: str | None = None
    topic_known: bool = False

    def checked_ids(self) -> set[str]:
        return set(self.included) | set(self.excluded)

    def to_dict(self) -> dict:
        return {
            "playlist_id": self.playlist_id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "included": {k: v.to_dict() for k, v in self.included.items()},
            "excluded": {k: v.to_dict() for k, v in self.excluded.items()},
            **({"topic": self.topic} if self.topic_known else {}),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlaylistRecord":
        return cls(
            playlist_id=d["playlist_id"],
            name=d["name"],
            description=d.get("description", ""),
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            included={k: SongEntry.from_dict(v) for k, v in d.get("included", {}).items()},
            excluded={k: SongEntry.from_dict(v) for k, v in d.get("excluded", {}).items()},
            topic=d.get("topic") if isinstance(d.get("topic"), str) else None,
            topic_known="topic" in d,
        )

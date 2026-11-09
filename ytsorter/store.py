"""On-disk JSON persistence: atomic helpers and the playlist record store."""
import json
import os
import time
from pathlib import Path

from .models import PlaylistRecord

REPLACE_TRIES = 5
REPLACE_WAIT = 0.1  # seconds between tries


def write_json_atomic(path: Path, data: dict) -> None:
    """Write `data` as UTF-8 JSON via a temp file + os.replace (never a half-written file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a virus scanner / indexer briefly holds the file
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(REPLACE_WAIT)


def read_json(path: Path) -> dict | None:
    """Return parsed JSON, or None if the file does not exist."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


class RecordStore:
    """One JSON file per playlist record, keyed by YouTube playlist id."""

    def __init__(self, dir: Path):
        self.dir = Path(dir)

    def _path(self, playlist_id: str) -> Path:
        return self.dir / f"{playlist_id}.json"

    def list(self) -> list[PlaylistRecord]:
        if not self.dir.is_dir():
            return []
        records = []
        for path in self.dir.glob("*.json"):
            data = read_json(path)
            if data is not None:
                records.append(PlaylistRecord.from_dict(data))
        return sorted(records, key=lambda r: r.created_at)

    def get(self, playlist_id: str) -> PlaylistRecord | None:
        data = read_json(self._path(playlist_id))
        return PlaylistRecord.from_dict(data) if data is not None else None

    def save(self, record: PlaylistRecord) -> None:
        write_json_atomic(self._path(record.playlist_id), record.to_dict())

    def delete(self, playlist_id: str) -> None:
        """Remove only the local record file; never touches YouTube Music."""
        self._path(playlist_id).unlink(missing_ok=True)


class JobStore:
    """The single in-progress job, saved as one JSON file so it survives restarts."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self):
        """Return the saved `jobs.Job`, or None."""
        from .jobs import Job  # local import: jobs.py imports this module

        data = read_json(self.path)
        return Job.from_dict(data) if data is not None else None

    def save(self, job) -> None:
        write_json_atomic(self.path, job.to_dict())

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

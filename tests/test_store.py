import json

from ytsorter import paths
from ytsorter.models import PlaylistRecord, SongEntry
from ytsorter.store import RecordStore


def make_record(playlist_id="pl1", created_at="2026-01-01T00:00:00+00:00", included=None, excluded=None):
    return PlaylistRecord(
        playlist_id=playlist_id,
        name="Name " + playlist_id,
        description="desc",
        created_at=created_at,
        updated_at=created_at,
        included=included or {},
        excluded=excluded or {},
    )


def entry(title="T", verdict="include"):
    return SongEntry(title=title, artists=["A"], verdict=verdict, reason="r", source="claude")


def test_data_dir_honors_env(tmp_path, monkeypatch):
    monkeypatch.setenv("YTSORTER_DATA_DIR", str(tmp_path))
    assert paths.data_dir() == tmp_path
    assert paths.youtube_auth_file() == tmp_path / "youtube_auth.json"
    assert paths.playlists_dir() == tmp_path / "playlists"
    assert paths.job_file() == tmp_path / "job.json"


def test_data_dir_created_if_missing(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "data"
    monkeypatch.setenv("YTSORTER_DATA_DIR", str(target))
    assert paths.data_dir() == target
    assert target.is_dir()


def test_record_roundtrip_unicode(tmp_path):
    title = 'Гурт "Океан Ельзи" — Обійми 🎵'
    record = make_record(included={"v1": entry(title)})
    store = RecordStore(tmp_path)
    store.save(record)
    assert store.get("pl1") == record
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["included"]["v1"]["title"] == title


def test_get_missing_returns_none(tmp_path):
    assert RecordStore(tmp_path).get("nope") is None


def test_checked_ids_union():
    record = make_record(included={"a": entry()}, excluded={"b": entry(verdict="exclude")})
    assert record.checked_ids() == {"a", "b"}


def test_list_sorted_and_delete(tmp_path):
    store = RecordStore(tmp_path)
    store.save(make_record("late", created_at="2026-03-01T00:00:00+00:00"))
    store.save(make_record("early", created_at="2026-01-01T00:00:00+00:00"))
    assert [r.playlist_id for r in store.list()] == ["early", "late"]
    store.delete("early")
    assert [r.playlist_id for r in store.list()] == ["late"]
    store.delete("early")  # deleting a missing record is a no-op


def test_atomic_write_leaves_no_tmp(tmp_path):
    store = RecordStore(tmp_path)
    store.save(make_record())
    store.save(make_record())
    assert list(tmp_path.glob("*.tmp")) == []
    assert len(list(tmp_path.glob("*.json"))) == 1

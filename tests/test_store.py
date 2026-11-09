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


def test_data_dir_is_created_owner_only(tmp_path, monkeypatch):
    import os
    import stat
    import pathlib

    target = tmp_path / "fresh" / "data"
    monkeypatch.setenv("YTSORTER_DATA_DIR", str(target))
    modes = []
    real_mkdir = pathlib.Path.mkdir

    def spy(self, *args, **kwargs):
        if self == target:
            modes.append(kwargs.get("mode", args[0] if args else None))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", spy)
    paths.data_dir()
    assert modes and set(modes) == {0o700}
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) & 0o077 == 0


def test_claude_work_dir_is_an_empty_subfolder(tmp_path, monkeypatch):
    monkeypatch.setenv("YTSORTER_DATA_DIR", str(tmp_path))
    assert paths.claude_work_dir() == tmp_path / "claude-work"
    assert (tmp_path / "claude-work").is_dir()


def test_atomic_write_retries_a_briefly_locked_file(tmp_path, monkeypatch):
    from ytsorter import store as store_mod

    calls = []
    real_replace = store_mod.os.replace

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError("sharing violation")
        real_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", flaky)
    monkeypatch.setattr(store_mod.time, "sleep", lambda s: None)
    store_mod.write_json_atomic(tmp_path / "a.json", {"x": 1})
    assert len(calls) == 3
    assert json.loads((tmp_path / "a.json").read_text(encoding="utf-8")) == {"x": 1}


def test_atomic_write_gives_up_after_a_few_tries(tmp_path, monkeypatch):
    import pytest
    from ytsorter import store as store_mod

    calls = []

    def locked(src, dst):
        calls.append(dst)
        raise PermissionError("sharing violation")

    sleeps = []
    monkeypatch.setattr(store_mod.os, "replace", locked)
    monkeypatch.setattr(store_mod.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError):
        store_mod.write_json_atomic(tmp_path / "a.json", {"x": 1})
    assert len(calls) == 5
    assert sleeps == [0.1] * 4

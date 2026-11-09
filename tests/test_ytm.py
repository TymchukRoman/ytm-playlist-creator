from pathlib import Path

import pytest

import ytsorter.ytm as ytm
from ytsorter.models import Song
from ytsorter.ytm import (
    PlaylistMissingError,
    YouTubeAuthError,
    YouTubeError,
    YTMusicClient,
)


class FakeApi:
    """Stands in for ytmusicapi.YTMusic; records every call."""

    def __init__(self, tracks=None, raises: Exception | None = None, add_result=None):
        self.tracks = tracks or []
        self.raises = raises
        self.add_result = add_result if add_result is not None else {"status": "STATUS_SUCCEEDED"}
        self.liked_limits = []
        self.created = []
        self.add_calls = []

    def _maybe_raise(self):
        if self.raises:
            raise self.raises

    def get_liked_songs(self, limit=100):
        self._maybe_raise()
        self.liked_limits.append(limit)
        return {"tracks": self.tracks}

    def create_playlist(self, title, description, privacy_status="PRIVATE", **kw):
        self._maybe_raise()
        self.created.append({"title": title, "description": description, "privacy_status": privacy_status})
        return "PLnew"

    def add_playlist_items(self, playlistId, videoIds=None, source_playlist=None, duplicates=False):
        self._maybe_raise()
        self.add_calls.append({"playlist": playlistId, "ids": list(videoIds), "duplicates": duplicates})
        return self.add_result

    def get_playlist(self, playlistId, limit=100, **kw):
        self._maybe_raise()
        return {"id": playlistId, "tracks": []}


def make_client(tmp_path: Path, api: FakeApi, connected: bool = True) -> YTMusicClient:
    auth = tmp_path / "auth.json"
    if connected:
        auth.write_text("{}", encoding="utf-8")
    return YTMusicClient(auth, factory=lambda path: api)


def test_liked_songs_skips_missing_ids_and_dedupes(tmp_path):
    api = FakeApi(tracks=[
        {"videoId": "a", "title": "A", "artists": [{"name": "X"}, {"name": "Y"}],
         "album": {"name": "Alb"}, "duration": "3:00"},
        {"videoId": None, "title": "Gone", "artists": [], "album": None},
        {"videoId": "a", "title": "A again", "artists": [{"name": "Z"}], "album": None},
        {"videoId": "b", "title": "B", "artists": None, "album": None},
    ])
    songs = make_client(tmp_path, api).get_liked_songs()
    assert [s.video_id for s in songs] == ["a", "b"]
    assert songs[0] == Song("a", "A", ["X", "Y"], "Alb", "3:00")
    assert songs[1].artists == [] and songs[1].album is None
    assert api.liked_limits == [None]  # whole list, not just the first 100


def test_create_playlist_is_private(tmp_path):
    api = FakeApi()
    assert make_client(tmp_path, api).create_playlist("Name", "Desc") == "PLnew"
    assert api.created == [{"title": "Name", "description": "Desc", "privacy_status": "PRIVATE"}]


def test_create_playlist_non_string_result_is_error(tmp_path):
    api = FakeApi()
    api.create_playlist = lambda *a, **k: {"error": "nope"}
    with pytest.raises(YouTubeError):
        make_client(tmp_path, api).create_playlist("Name", "Desc")


def test_add_songs_chunks_of_50_and_reports_progress(tmp_path):
    api = FakeApi()
    progress = []
    ids = [f"v{i}" for i in range(120)]
    make_client(tmp_path, api).add_songs("PL1", ids, lambda d, t: progress.append((d, t)))
    assert [len(c["ids"]) for c in api.add_calls] == [50, 50, 20]
    assert [i for c in api.add_calls for i in c["ids"]] == ids
    assert all(c["duplicates"] is False and c["playlist"] == "PL1" for c in api.add_calls)
    assert progress == [(50, 120), (100, 120), (120, 120)]


def test_add_songs_failed_status_raises(tmp_path):
    api = FakeApi(add_result={"error": {"message": "duplicate"}})
    with pytest.raises(YouTubeError):
        make_client(tmp_path, api).add_songs("PL1", ["a"], lambda d, t: None)


def test_add_songs_missing_playlist_raises_playlist_missing(tmp_path):
    api = FakeApi(raises=Exception("Server returned HTTP 404: Not Found."))
    with pytest.raises(PlaylistMissingError):
        make_client(tmp_path, api).add_songs("PL1", ["a"], lambda d, t: None)


def test_auth_failure_maps_to_youtube_auth_error(tmp_path):
    api = FakeApi(raises=Exception("Server returned HTTP 401"))
    client = make_client(tmp_path, api)
    with pytest.raises(YouTubeAuthError):
        client.get_liked_songs()
    with pytest.raises(YouTubeAuthError):
        client.create_playlist("n", "d")
    with pytest.raises(YouTubeAuthError):
        client.add_songs("PL1", ["a"], lambda d, t: None)


def test_other_failures_map_to_youtube_error(tmp_path):
    client = make_client(tmp_path, FakeApi(raises=Exception("boom")))
    with pytest.raises(YouTubeError) as exc:
        client.get_liked_songs()
    assert not isinstance(exc.value, YouTubeAuthError)


def test_not_connected_raises_auth_error(tmp_path):
    client = make_client(tmp_path, FakeApi(), connected=False)
    assert client.is_connected() is False
    with pytest.raises(YouTubeAuthError):
        client.get_liked_songs()


def test_connect_ok_writes_via_setup_and_verifies(tmp_path, monkeypatch):
    calls = []
    auth = tmp_path / "auth.json"

    def fake_setup(filepath=None, headers_raw=None):
        calls.append((filepath, headers_raw))
        Path(filepath).write_text("{}", encoding="utf-8")
        return "{}"

    monkeypatch.setattr(ytm.ytmusicapi, "setup", fake_setup)
    api = FakeApi()
    client = YTMusicClient(auth, factory=lambda path: api)
    client.connect("raw headers")
    assert calls == [(str(auth), "raw headers")]
    assert api.liked_limits == [1]
    assert client.is_connected()


def test_connect_bad_headers_removes_file(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"

    def fake_setup(filepath=None, headers_raw=None):
        Path(filepath).write_text("{}", encoding="utf-8")
        return "{}"

    monkeypatch.setattr(ytm.ytmusicapi, "setup", fake_setup)
    client = YTMusicClient(auth, factory=lambda path: FakeApi(raises=Exception("HTTP 401")))
    with pytest.raises(YouTubeAuthError, match="didn't work"):
        client.connect("junk")
    assert not auth.exists()
    assert not client.is_connected()


def test_connect_setup_rejecting_headers_is_auth_error(tmp_path, monkeypatch):
    def fake_setup(filepath=None, headers_raw=None):
        raise Exception("Cookie missing")

    monkeypatch.setattr(ytm.ytmusicapi, "setup", fake_setup)
    client = YTMusicClient(tmp_path / "auth.json", factory=lambda path: FakeApi())
    with pytest.raises(YouTubeAuthError):
        client.connect("junk")
    assert not (tmp_path / "auth.json").exists()


def test_disconnect_deletes_auth_file(tmp_path):
    client = make_client(tmp_path, FakeApi())
    client.disconnect()
    assert not client.is_connected()
    client.disconnect()  # idempotent


def test_playlist_exists_true(tmp_path):
    assert make_client(tmp_path, FakeApi()).playlist_exists("PL1") is True


def test_playlist_exists_false_when_not_found(tmp_path):
    api = FakeApi(raises=Exception("Server returned HTTP 404: Not Found.\nPlaylist does not exist"))
    assert make_client(tmp_path, api).playlist_exists("PL1") is False


def test_playlist_exists_other_errors_propagate(tmp_path):
    with pytest.raises(YouTubeError):
        make_client(tmp_path, FakeApi(raises=Exception("boom"))).playlist_exists("PL1")
    with pytest.raises(YouTubeAuthError):
        make_client(tmp_path, FakeApi(raises=Exception("HTTP 403"))).playlist_exists("PL1")


def test_client_has_no_remove_methods():
    assert not any(n.startswith(("remove", "delete", "unlike", "rate")) for n in dir(YTMusicClient))

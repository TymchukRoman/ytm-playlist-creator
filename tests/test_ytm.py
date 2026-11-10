import os
import stat
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

    def __init__(self, tracks=None, raises: Exception | None = None, add_result=None, playlist_tracks=None):
        self.tracks = tracks or []
        self.playlist_tracks = playlist_tracks or []
        self.playlist_limits = []
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
        self.playlist_limits.append(limit)
        return {"id": playlistId, "tracks": self.playlist_tracks}


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


def test_add_songs_auth_expires_mid_job(tmp_path):
    api = FakeApi()
    real_add = api.add_playlist_items

    def flaky(playlistId, videoIds=None, source_playlist=None, duplicates=False):
        if len(api.add_calls) == 1:
            raise Exception("Server returned HTTP 401: Unauthorized")
        return real_add(playlistId, videoIds, duplicates=duplicates)

    api.add_playlist_items = flaky
    progress = []
    with pytest.raises(YouTubeAuthError):
        make_client(tmp_path, api).add_songs(
            "PL1", [f"v{i}" for i in range(120)], lambda d, t: progress.append((d, t)))
    assert progress == [(50, 120)]
    assert len(api.add_calls) == 1


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


def patch_setup(monkeypatch, content="NEWCREDS", raises: Exception | None = None):
    calls = []

    def fake_setup(filepath=None, headers_raw=None):
        calls.append((filepath, headers_raw))
        if raises:
            raise raises
        return content

    monkeypatch.setattr(ytm.ytmusicapi, "setup", fake_setup)
    return calls


def test_connect_ok_verifies_then_saves(tmp_path, monkeypatch):
    calls = patch_setup(monkeypatch, content="NEWCREDS Привет")
    auth = tmp_path / "auth.json"
    seen = []
    api = FakeApi()

    def factory(path):
        seen.append((path, Path(path).read_text(encoding="utf-8")))
        return api

    client = YTMusicClient(auth, factory=factory)
    client.connect("raw headers")
    assert calls == [(None, "raw headers")]  # setup never writes the file itself
    assert api.liked_limits == [1]
    assert seen[0][0] != str(auth) and seen[0][1].startswith("NEWCREDS")  # verified from a temp file
    assert auth.read_text(encoding="utf-8") == "NEWCREDS Привет"
    assert client.is_connected()
    assert [p.name for p in tmp_path.iterdir()] == ["auth.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_connect_writes_owner_only_file(tmp_path, monkeypatch):
    patch_setup(monkeypatch)
    auth = tmp_path / "auth.json"
    YTMusicClient(auth, factory=lambda path: FakeApi()).connect("h")
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600


def test_connect_bad_headers_leaves_no_file(tmp_path, monkeypatch):
    patch_setup(monkeypatch)
    auth = tmp_path / "auth.json"
    client = YTMusicClient(auth, factory=lambda path: FakeApi(raises=Exception("HTTP 401")))
    with pytest.raises(YouTubeAuthError, match="didn't work"):
        client.connect("junk")
    assert list(tmp_path.iterdir()) == []
    assert not client.is_connected()


def test_connect_bad_headers_keeps_existing_good_signin(tmp_path, monkeypatch):
    patch_setup(monkeypatch, content="BADCREDS")
    auth = tmp_path / "auth.json"
    auth.write_text("OLDCREDS", encoding="utf-8")
    old_api = FakeApi()

    def factory(path):
        if Path(path).read_text(encoding="utf-8") == "BADCREDS":
            return FakeApi(raises=Exception("HTTP 401"))
        return old_api

    client = YTMusicClient(auth, factory=factory)
    client.get_liked_songs()  # caches the old API object
    with pytest.raises(YouTubeAuthError):
        client.connect("junk")
    assert auth.read_text(encoding="utf-8") == "OLDCREDS"
    assert client.is_connected()
    assert [p.name for p in tmp_path.iterdir()] == ["auth.json"]
    assert client._client() is old_api


def test_connect_setup_rejecting_headers_is_auth_error(tmp_path, monkeypatch):
    patch_setup(monkeypatch, raises=Exception("Cookie missing"))
    client = YTMusicClient(tmp_path / "auth.json", factory=lambda path: FakeApi())
    with pytest.raises(YouTubeAuthError):
        client.connect("junk")
    assert list(tmp_path.iterdir()) == []


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


def test_get_playlist_video_ids_reads_all_and_skips_missing_ids(tmp_path):
    api = FakeApi(playlist_tracks=[{"videoId": "a"}, {"videoId": None}, {"title": "gone"}, {"videoId": "b"},
                                   {"videoId": "a"}])
    assert make_client(tmp_path, api).get_playlist_video_ids("PL1") == {"a", "b"}
    assert api.playlist_limits == [None]


def test_get_playlist_video_ids_not_found_is_playlist_missing(tmp_path):
    api = FakeApi(raises=Exception("Server returned HTTP 404: Not Found.\nPlaylist does not exist"))
    with pytest.raises(PlaylistMissingError):
        make_client(tmp_path, api).get_playlist_video_ids("PL1")


def test_get_playlist_video_ids_other_errors_translate(tmp_path):
    with pytest.raises(YouTubeAuthError):
        make_client(tmp_path, FakeApi(raises=Exception("HTTP 401"))).get_playlist_video_ids("PL1")
    with pytest.raises(YouTubeError) as info:
        make_client(tmp_path, FakeApi(raises=Exception("boom"))).get_playlist_video_ids("PL1")
    assert type(info.value) is YouTubeError
    with pytest.raises(YouTubeAuthError):
        offline = tmp_path / "offline"
        offline.mkdir()
        make_client(offline, FakeApi(), connected=False).get_playlist_video_ids("PL1")


def test_client_has_no_remove_methods():
    assert not any(n.startswith(("remove", "delete", "unlike", "rate")) for n in dir(YTMusicClient))


def test_create_playlist_drops_angle_brackets_from_description(tmp_path):
    api = FakeApi()
    make_client(tmp_path, api).create_playlist("Name", "songs I <3, no -> covers")
    assert api.created[0]["description"] == "songs I 3, no - covers"

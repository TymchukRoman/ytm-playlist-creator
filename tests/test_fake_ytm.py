import pytest

from ytsorter.fakes import FakeYTMusic
from ytsorter.models import Song
from ytsorter.ytm import PlaylistMissingError, YouTubeAuthError, YouTubeError

SONGS = [Song("a", "A", ["X"]), Song("b", "B", ["Y"])]


def test_create_and_add_are_recorded():
    yt = FakeYTMusic(SONGS)
    assert yt.get_liked_songs() == SONGS
    pid = yt.create_playlist("Name", "Desc")
    assert pid == "PLfake1"
    assert yt.create_playlist("Two", "") == "PLfake2"
    assert yt.created[pid] == {"name": "Name", "description": "Desc", "privacy_status": "PRIVATE"}
    progress = []
    yt.add_songs(pid, [f"v{i}" for i in range(60)], lambda d, t: progress.append((d, t)))
    assert len(yt.added[pid]) == 60
    assert progress == [(50, 60), (60, 60)]
    yt.add_songs(pid, ["z"], lambda d, t: None)
    assert yt.added[pid][-1] == "z"


def test_playlist_exists_tracks_deleted_and_unknown():
    yt = FakeYTMusic([])
    pid = yt.create_playlist("n", "d")
    assert yt.playlist_exists(pid)
    assert not yt.playlist_exists("nope")
    yt.deleted_playlists.add(pid)
    assert not yt.playlist_exists(pid)
    with pytest.raises(PlaylistMissingError):
        yt.add_songs(pid, ["a"], lambda d, t: None)


def test_expire_auth_until_reconnect():
    yt = FakeYTMusic(SONGS)
    pid = yt.create_playlist("n", "d")
    yt.expire_auth()
    assert yt.is_connected()
    for call in (
        yt.get_liked_songs,
        lambda: yt.create_playlist("n", "d"),
        lambda: yt.add_songs(pid, ["a"], lambda d, t: None),
        lambda: yt.playlist_exists(pid),
    ):
        with pytest.raises(YouTubeAuthError):
            call()
    yt.connect("headers")
    assert yt.get_liked_songs() == SONGS
    yt.add_songs(pid, ["a"], lambda d, t: None)


def test_fail_next_add_counts_down():
    yt = FakeYTMusic([])
    pid = yt.create_playlist("n", "d")
    yt.fail_next_add = 2
    for _ in range(2):
        with pytest.raises(YouTubeError):
            yt.add_songs(pid, ["a"], lambda d, t: None)
    assert yt.added.get(pid, []) == []
    yt.add_songs(pid, ["a"], lambda d, t: None)
    assert yt.added[pid] == ["a"]


def test_disconnect_and_no_remove_methods():
    yt = FakeYTMusic([])
    yt.disconnect()
    assert not yt.is_connected()
    assert not any(n.startswith(("remove", "delete", "unlike", "rate")) for n in dir(FakeYTMusic))


def test_duplicate_in_chunk_fails_and_adds_nothing_from_that_chunk():
    yt = FakeYTMusic([])
    pid = yt.create_playlist("n", "d")
    yt.add_songs(pid, ["a", "b"], lambda d, t: None)
    progress = []
    ids = [f"v{i}" for i in range(50)] + ["x", "b", "y"]  # second chunk contains existing b
    with pytest.raises(YouTubeError):
        yt.add_songs(pid, ids, lambda d, t: progress.append((d, t)))
    assert progress == [(50, 53)]
    assert yt.added[pid] == ["a", "b"] + [f"v{i}" for i in range(50)]


def test_fail_add_at_chunk_is_partial_and_one_shot():
    yt = FakeYTMusic([])
    pid = yt.create_playlist("n", "d")
    yt.fail_add_at_chunk = 1
    progress = []
    ids = [f"v{i}" for i in range(120)]
    with pytest.raises(YouTubeError):
        yt.add_songs(pid, ids, lambda d, t: progress.append((d, t)))
    assert progress == [(50, 120)]
    assert yt.added[pid] == ids[:50]
    assert yt.fail_add_at_chunk is None
    yt.add_songs(pid, ids[50:], lambda d, t: None)
    assert yt.added[pid] == ids


def test_get_playlist_video_ids_matches_added_and_errors():
    yt = FakeYTMusic([])
    pid = yt.create_playlist("n", "d")
    assert yt.get_playlist_video_ids(pid) == set()
    yt.add_songs(pid, ["a", "b"], lambda d, t: None)
    assert yt.get_playlist_video_ids(pid) == {"a", "b"}
    with pytest.raises(PlaylistMissingError):
        yt.get_playlist_video_ids("nope")
    yt.deleted_playlists.add(pid)
    with pytest.raises(PlaylistMissingError):
        yt.get_playlist_video_ids(pid)
    yt.deleted_playlists.clear()
    yt.expire_auth()
    with pytest.raises(YouTubeAuthError):
        yt.get_playlist_video_ids(pid)


def test_expire_auth_after_chunk_stops_the_next_chunk():
    yt = FakeYTMusic([])
    pid = yt.create_playlist("n", "d")
    yt.expire_auth_after_chunk = 0
    progress = []
    ids = [f"v{i}" for i in range(120)]
    with pytest.raises(YouTubeAuthError):
        yt.add_songs(pid, ids, lambda d, t: progress.append((d, t)))
    assert progress == [(50, 120)]
    assert yt.added[pid] == ids[:50]
    assert yt.expire_auth_after_chunk is None
    yt.connect("headers")
    yt.add_songs(pid, ids[50:], lambda d, t: None)
    assert yt.added[pid] == ids

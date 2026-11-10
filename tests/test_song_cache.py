import json

import pytest

from ytsorter.song_cache import SongCache, SongKnowledge, normalize_tag, normalize_tags


@pytest.mark.parametrize("raw,clean", [("K-Pop", "k-pop"), ("  Anime   Opening. ", "anime opening"),
                                       ("#ukrainian", "ukrainian"), ("", ""), (None, ""), (5, ""),
                                       ("x" * 60, "x" * 40)])
def test_normalize_tag(raw, clean):
    assert normalize_tag(raw) == clean


def test_normalize_tags_dedupes_and_limits():
    assert normalize_tags(["A", "a", "", "b"] + [f"t{i}" for i in range(20)]) == ["a", "b", "t0", "t1", "t2", "t3", "t4", "t5"]
    assert normalize_tags("not a list") == []


def test_learn_fits_and_not_fits_are_exclusive(tmp_path):
    cache = SongCache(tmp_path / "songs.json")
    cache.learn("v1", tags=["Japanese"], not_fits="anime opening")
    assert cache.get("v1") == SongKnowledge(["japanese"], ["anime opening"], [])
    cache.learn("v1", fits="anime opening", playlist_id="PL1")  # the user ticked it in: a correction
    assert cache.get("v1") == SongKnowledge(["japanese", "anime opening"], [], ["PL1"])
    cache.learn("v1", not_fits="anime opening")
    assert cache.get("v1").tags == ["japanese"] and cache.get("v1").surely_not == ["anime opening"]


def test_saves_and_reloads(tmp_path):
    cache = SongCache(tmp_path / "songs.json")
    cache.learn("v1", tags=["k-pop"], playlist_id="PL1")
    cache.learn("v2", not_fits="k-pop")
    cache.save()
    again = SongCache(tmp_path / "songs.json")
    assert again.get("v1") == SongKnowledge(["k-pop"], [], ["PL1"])
    assert again.get("v2") == SongKnowledge([], ["k-pop"], [])
    data = json.loads((tmp_path / "songs.json").read_text(encoding="utf-8"))
    assert data["songs"]["v1"] == {"tags": ["k-pop"], "surely_not": [], "playlists": ["PL1"]}


@pytest.mark.parametrize("content", ["nope", "[]", '{"songs": 3}', '{"songs": {"v1": "bad"}}'])
def test_bad_file_starts_over(tmp_path, content):
    (tmp_path / "songs.json").write_text(content, encoding="utf-8")
    cache = SongCache(tmp_path / "songs.json")
    assert cache.get("v9") is None
    assert cache.vocabulary() == []


def test_vocabulary_most_common_first():
    cache = SongCache(None)
    cache.learn("a", tags=["k-pop", "korean"])
    cache.learn("b", tags=["k-pop"])
    cache.learn("c", tags=["rock"], not_fits="k-pop")
    assert cache.vocabulary()[0] == "k-pop"
    assert set(cache.vocabulary()) == {"k-pop", "korean", "rock"}
    assert cache.vocabulary(limit=1) == ["k-pop"]


def test_get_returns_a_copy():
    cache = SongCache(None)
    cache.learn("a", tags=["rock"])
    cache.get("a").tags.append("pop")
    assert cache.get("a").tags == ["rock"]


def test_forget_playlist_removes_it_everywhere(tmp_path):
    cache = SongCache(tmp_path / "songs.json")
    cache.learn("a", playlist_id="PL1")
    cache.learn("b", playlist_id="PL2")
    cache.forget_playlist("PL1")
    assert cache.get("a").playlists == [] and cache.get("b").playlists == ["PL2"]
    assert SongCache(tmp_path / "songs.json").get("a").playlists == []

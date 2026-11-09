import time

from ytsorter import demo_data
from ytsorter.demo_data import SAMPLE_SONGS, TAGS, DemoClassifier, demo_rule
from ytsorter.models import Song


def by_tag(tag):
    return [s for s in SAMPLE_SONGS if TAGS[s.video_id] == tag]


def test_sample_songs_are_about_three_hundred_unique_and_tagged():
    assert 280 <= len(SAMPLE_SONGS) <= 330
    assert len({s.video_id for s in SAMPLE_SONGS}) == len(SAMPLE_SONGS)
    assert all(isinstance(s, Song) and s.title and s.artists for s in SAMPLE_SONGS)
    assert set(TAGS.values()) == {"anime-opening", "anime-other", "k-pop", "rock", "ukrainian", "other"}
    assert set(TAGS) == {s.video_id for s in SAMPLE_SONGS}
    assert all(len(by_tag(t)) >= 15 for t in set(TAGS.values()))


def test_rule_opening_includes_openings_excludes_other_and_flags_a_few_unsure():
    verdicts = {s.video_id: demo_rule("anime openings", s)[0] for s in SAMPLE_SONGS}
    openings = by_tag("anime-opening")
    included = [s for s in openings if verdicts[s.video_id] == "include"]
    assert len(included) >= 0.85 * len(openings)
    assert all(TAGS[s.video_id] in ("anime-opening", "anime-other") for s in SAMPLE_SONGS
               if verdicts[s.video_id] in ("include", "unsure"))
    assert any(verdicts[s.video_id] == "unsure" for s in by_tag("anime-other"))
    assert all(verdicts[s.video_id] == "exclude" for s in SAMPLE_SONGS
               if TAGS[s.video_id] in ("k-pop", "rock", "ukrainian", "other"))
    unsure = sum(v == "unsure" for v in verdicts.values())
    assert 0 < unsure <= 0.1 * len(SAMPLE_SONGS)


def test_rule_keywords_map_to_tags():
    cases = {
        "k-pop": "k-pop", "kpop songs": "k-pop", "Korean music": "k-pop",
        "rock": "rock", "Ukrainian songs": "ukrainian", "українські пісні": "ukrainian",
        "anime": "anime-opening",
    }
    for description, tag in cases.items():
        songs = by_tag(tag)
        verdicts = [demo_rule(description, s)[0] for s in songs]
        assert verdicts.count("include") >= 0.8 * len(songs), description
    # "anime" in general also takes the non-opening anime songs
    others = [demo_rule("anime", s)[0] for s in by_tag("anime-other")]
    assert others.count("include") >= 0.8 * len(others)


def test_rule_is_deterministic_and_gives_reasons():
    for s in SAMPLE_SONGS[:50]:
        a = demo_rule("rock", s)
        assert a == demo_rule("rock", s)
        assert a[1]


def test_rule_without_known_keyword_falls_back_to_word_match():
    song = Song("x", "Midnight City", ["M83"])
    assert demo_rule("midnight", song)[0] == "include"
    assert demo_rule("zzz", song)[0] == "exclude"


def test_demo_classifier_sleeps_per_batch_only_when_configured(monkeypatch):
    songs = SAMPLE_SONGS[:3]
    monkeypatch.setattr(demo_data, "BATCH_DELAY", 0.05)
    start = time.perf_counter()
    result = DemoClassifier().classify_batch("rock", songs)
    assert time.perf_counter() - start >= 0.045
    assert [d.video_id for d in result] == [s.video_id for s in songs]
    monkeypatch.setattr(demo_data, "BATCH_DELAY", 0)
    start = time.perf_counter()
    DemoClassifier().classify_batch("rock", songs)
    assert time.perf_counter() - start < 0.04

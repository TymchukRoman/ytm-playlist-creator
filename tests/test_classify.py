import pytest

from ytsorter.claude_cli import ClaudeUsageLimitError
from ytsorter.classify import BATCH_SIZE, classify_pending
from ytsorter.fakes import FakeClassifier
from ytsorter.models import Decision, Song


def make_songs(n: int) -> list[Song]:
    return [Song(f"v{i}", f"Title {i}", ["Artist"]) for i in range(n)]


class RecordingClassifier(FakeClassifier):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sizes: list[int] = []

    def classify_batch(self, description, songs):
        result = super().classify_batch(description, songs)
        self.sizes.append(len(songs))
        return result


def test_batch_size_constant():
    assert BATCH_SIZE == 100


def test_batches_of_100():
    classifier = RecordingClassifier()
    decisions: dict[str, Decision] = {}
    progress: list[tuple[int, int]] = []
    classify_pending("title", make_songs(250), decisions, classifier,
                     lambda done, total: progress.append((done, total)))
    assert classifier.sizes == [100, 100, 50]
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert len(decisions) == 250


def test_skips_already_decided():
    songs = make_songs(150)
    decisions = {s.video_id: Decision(s.video_id, "include", "earlier") for s in songs[:100]}
    classifier = RecordingClassifier()
    progress: list[tuple[int, int]] = []
    classify_pending("title", songs, decisions, classifier,
                     lambda done, total: progress.append((done, total)))
    assert classifier.sizes == [50]
    assert progress == [(1, 1)]
    assert len(decisions) == 150
    assert decisions["v0"].reason == "earlier"


def test_limit_mid_run_keeps_finished_batches():
    classifier = FakeClassifier(fail_after=1)
    decisions: dict[str, Decision] = {}
    progress: list[tuple[int, int]] = []
    with pytest.raises(ClaudeUsageLimitError):
        classify_pending("title", make_songs(250), decisions, classifier,
                         lambda done, total: progress.append((done, total)))
    assert len(decisions) == 100
    assert progress == [(1, 3)]
    assert set(decisions) == {f"v{i}" for i in range(100)}


def test_resume_after_limit_continues_with_remaining():
    songs = make_songs(250)
    decisions: dict[str, Decision] = {}
    with pytest.raises(ClaudeUsageLimitError):
        classify_pending("title", songs, decisions, FakeClassifier(fail_after=1), lambda d, t: None)
    classifier = RecordingClassifier()
    progress: list[tuple[int, int]] = []
    classify_pending("title", songs, decisions, classifier,
                     lambda done, total: progress.append((done, total)))
    assert classifier.sizes == [100, 50]
    assert progress == [(1, 2), (2, 2)]
    assert len(decisions) == 250


def test_nothing_pending_makes_no_calls():
    songs = make_songs(3)
    decisions = {s.video_id: Decision(s.video_id, "exclude", "x") for s in songs}
    classifier = RecordingClassifier()
    progress: list[tuple[int, int]] = []
    classify_pending("title", songs, decisions, classifier,
                     lambda done, total: progress.append((done, total)))
    assert classifier.sizes == []
    assert progress == []

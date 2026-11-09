import pytest

from ytsorter.claude_cli import ClaudeAuthError
from ytsorter.fakes import FakeClassifier, FakeYTMusic
from ytsorter.jobs import Job, JobBusyError, JobRunner, JobStateError
from ytsorter.models import PlaylistRecord, Song, SongEntry
from ytsorter.store import JobStore, RecordStore

USAGE_MSG = "Claude usage limit reached. Your progress is saved — press Resume later."
CLAUDE_AUTH_MSG = "Claude isn't logged in. Run `claude` once to sign in, then press Resume."
YT_AUTH_MSG = "YouTube Music sign-in expired — reconnect, then press Resume."
CLOSED_MSG = "The app was closed during this step — press Resume."
NO_NEW = "No new liked songs since last update"


def song(vid: str) -> Song:
    return Song(vid, f"Title {vid} «ё»", ["Артист"], "Album", "3:21")


def songs(*vids: str) -> list[Song]:
    return [song(v) for v in vids]


def make_songs(n: int) -> list[Song]:
    return [song(f"v{i}") for i in range(n)]


class RecordingClassifier(FakeClassifier):
    """Verdicts by video id (default exclude); remembers every id it was asked about."""

    def __init__(self, verdicts: dict[str, str] | None = None, **kwargs):
        verdicts = verdicts or {}
        super().__init__(rules=lambda d, s: (verdicts.get(s.video_id, "exclude"), f"why {s.video_id}"),
                         **kwargs)
        self.sent: list[str] = []

    def classify_batch(self, description, batch):
        result = super().classify_batch(description, batch)
        self.sent.extend(s.video_id for s in batch)
        return result


class SpyJobStore(JobStore):
    def __init__(self, path):
        super().__init__(path)
        self.progress: list[tuple] = []

    def save(self, job):
        p = job.progress
        self.progress.append((p.stage, p.message, p.current, p.total))
        super().save(job)


class Deferred:
    """run_in_background that queues work until run() is called."""

    def __init__(self):
        self.queue = []

    def __call__(self, fn):
        self.queue.append(fn)

    def run(self):
        while self.queue:
            self.queue.pop(0)()


def sync(fn):
    fn()


@pytest.fixture
def stores(tmp_path):
    return RecordStore(tmp_path / "playlists"), SpyJobStore(tmp_path / "job.json")


def make_runner(stores, ytm, classifier, run=sync):
    records, jobs = stores
    return JobRunner(records, jobs, ytm, classifier, run_in_background=run)


def seed_playlist(fake, records, pid, name, included=(), excluded=(), user_excluded=()):
    """A playlist this tool made earlier: on YouTube (fake) and in the records."""
    fake.created[pid] = {"name": name, "description": name.lower(), "privacy_status": "PRIVATE"}
    fake.added[pid] = list(included)

    def entry(vid, verdict, source):
        return SongEntry(f"Title {vid}", ["A"], verdict, "earlier", source)

    rec = PlaylistRecord(pid, name, name.lower(), "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
                         included={v: entry(v, "include", "claude") for v in included},
                         excluded={**{v: entry(v, "exclude", "claude") for v in excluded},
                                   **{v: entry(v, "include", "user") for v in user_excluded}})
    records.save(rec)
    return rec


# --- Create -------------------------------------------------------------------------------

def test_create_flow_review_then_approve(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "b", "c", "d"))
    clf = RecordingClassifier({"a": "include", "b": "include", "c": "unsure"})
    runner = make_runner(stores, fake, clf)

    runner.start_create("Rock", "rock songs")
    job = runner.current()
    assert job.status == "review"
    assert job.review_index == 0
    assert fake.created == {} and fake.added == {}
    assert records.list() == []
    assert set(job.targets[0].decisions) == {"a", "b", "c", "d"}

    runner.approve(["a", "b", "not-a-liked-song"])
    assert len(fake.created) == 1
    pid = next(iter(fake.created))
    assert fake.created[pid] == {"name": "Rock", "description": "rock songs", "privacy_status": "PRIVATE"}
    assert fake.added == {pid: ["a", "b"]}
    rec = records.get(pid)
    assert set(rec.included) == {"a", "b"}
    assert set(rec.excluded) == {"c", "d"}
    assert rec.name == "Rock" and rec.description == "rock songs"
    assert rec.included["a"].title == "Title a «ё»" and rec.included["a"].artists == ["Артист"]
    assert runner.current().status == "done"


def test_create_progress_messages(stores):
    _, jobs = stores
    fake = FakeYTMusic(make_songs(120))
    runner = make_runner(stores, fake, RecordingClassifier())
    runner.start_create("Rock", "rock")
    assert ("fetch", "Fetching your liked songs…", None, None) in jobs.progress
    assert ("classify", "Asking Claude which songs fit… batch 1 of 2", 1, 2) in jobs.progress
    assert ("classify", "Asking Claude which songs fit… batch 2 of 2", 2, 2) in jobs.progress
    runner.approve([f"v{i}" for i in range(60)])
    assert ("add", "Adding songs… 50 of 60", 50, 60) in jobs.progress
    assert ("add", "Adding songs… 60 of 60", 60, 60) in jobs.progress


def test_cancel_writes_nothing(stores):
    records, jobs = stores
    fake = FakeYTMusic(songs("a", "b"))
    runner = make_runner(stores, fake, RecordingClassifier({"a": "include"}))
    runner.start_create("Rock", "rock")
    runner.cancel()
    assert fake.created == {}
    assert fake.added == {}
    assert records.list() == []
    assert runner.current() is None
    assert jobs.load() is None


def test_unchecking_include_marks_user_source(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "b"))
    runner = make_runner(stores, fake, RecordingClassifier({"a": "include", "b": "include"}))
    runner.start_create("Rock", "rock")
    runner.approve(["a"])
    rec = records.list()[0]
    assert rec.included["a"].source == "claude"
    b = rec.excluded["b"]
    assert (b.source, b.verdict, b.reason) == ("user", "include", "why b")


def test_checking_unsure_marks_user_source(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "b", "c"))
    runner = make_runner(stores, fake, RecordingClassifier({"a": "unsure", "b": "unsure"}))
    runner.start_create("Rock", "rock")
    runner.approve(["a"])
    rec = records.list()[0]
    a = rec.included["a"]
    assert (a.source, a.verdict, a.reason) == ("user", "unsure", "why a")
    assert rec.excluded["b"].source == "claude"
    assert rec.excluded["b"].verdict == "unsure"
    assert rec.excluded["c"].source == "claude"


def test_create_with_nothing_approved_rejected(stores):
    fake = FakeYTMusic(songs("a"))
    runner = make_runner(stores, fake, RecordingClassifier())
    runner.start_create("Rock", "rock")
    with pytest.raises(ValueError):
        runner.approve([])
    assert runner.current().status == "review"
    assert fake.created == {}


# --- Update -------------------------------------------------------------------------------

def test_update_only_new_songs(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "b", "c", "d"))
    seed_playlist(fake, records, "PL1", "Rock", included=["a"], excluded=["b"])
    clf = RecordingClassifier({"c": "include", "d": "include"})
    runner = make_runner(stores, fake, clf)

    runner.start_update(["PL1"])
    assert runner.current().status == "review"
    assert clf.sent == ["c", "d"]
    assert fake.added["PL1"] == ["a"]

    runner.approve(["c"])
    assert fake.added["PL1"] == ["a", "c"]
    assert fake.created.keys() == {"PL1"}  # nothing new created
    rec = records.get("PL1")
    assert rec.checked_ids() == {"a", "b", "c", "d"}
    assert set(rec.included) == {"a", "c"}
    assert rec.excluded["d"].source == "user"
    assert rec.updated_at > "2026-01-01T00:00:00+00:00"
    assert rec.created_at == "2026-01-01T00:00:00+00:00"


def test_user_decisions_never_reclassified(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "b", "c"))
    seed_playlist(fake, records, "PL1", "Rock", included=["a"], user_excluded=["b"])
    clf = RecordingClassifier({"b": "include", "c": "include"})
    runner = make_runner(stores, fake, clf)
    runner.start_update(["PL1"])
    assert clf.sent == ["c"]
    runner.approve(["c"])
    rec = records.get("PL1")
    assert rec.excluded["b"].source == "user"
    assert fake.added["PL1"] == ["a", "c"]


def test_update_no_new_songs_notice(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "b"))
    before = seed_playlist(fake, records, "PL1", "Rock", included=["a"], excluded=["b"])
    clf = RecordingClassifier()
    runner = make_runner(stores, fake, clf)
    runner.start_update(["PL1"])
    job = runner.current()
    assert job.status == "done"
    assert job.notice == NO_NEW
    assert clf.sent == []
    assert fake.added == {"PL1": ["a"]}
    assert records.get("PL1") == before


def test_update_empty_approve_records_all_excluded(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "b", "c"))
    seed_playlist(fake, records, "PL1", "Rock", included=["a"])
    runner = make_runner(stores, fake, RecordingClassifier({"b": "include"}))
    runner.start_update(["PL1"])
    runner.approve([])
    rec = records.get("PL1")
    assert set(rec.excluded) == {"b", "c"}
    assert rec.excluded["b"].source == "user"
    assert fake.added["PL1"] == ["a"]
    assert runner.current().status == "done"


def test_update_all_reviews_each_playlist_in_turn(stores):
    records, jobs = stores
    fake = FakeYTMusic(songs("a", "b", "c"))
    seed_playlist(fake, records, "PL1", "Rock", included=["a"])
    second = seed_playlist(fake, records, "PL2", "Jazz", included=["a"])
    runner = make_runner(stores, fake, RecordingClassifier({"b": "include", "c": "include"}))

    runner.start_update(["PL1", "PL2"])
    job = runner.current()
    assert job.status == "review" and job.review_index == 0
    assert [t.name for t in job.targets] == ["Rock", "Jazz"]
    assert ("classify", "Rock: Asking Claude which songs fit… batch 1 of 1", 1, 1) in jobs.progress
    assert ("classify", "Jazz: Asking Claude which songs fit… batch 1 of 1", 1, 1) in jobs.progress

    runner.approve(["b"])
    job = runner.current()
    assert job.status == "review" and job.review_index == 1

    runner.skip()
    assert runner.current().status == "done"
    assert fake.added == {"PL1": ["a", "b"], "PL2": ["a"]}
    assert records.get("PL2") == second
    assert records.get("PL1").checked_ids() == {"a", "b", "c"}


def test_update_all_skips_playlists_without_new_songs(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "b"))
    seed_playlist(fake, records, "PL1", "Rock", included=["a"])
    seed_playlist(fake, records, "PL2", "Jazz", included=["a"], excluded=["b"])
    runner = make_runner(stores, fake, RecordingClassifier())
    runner.start_update(["PL1", "PL2"])
    job = runner.current()
    assert job.status == "review" and job.review_index == 0
    assert job.targets[1].skipped is True
    assert NO_NEW in job.notice and "Jazz" in job.notice
    runner.approve([])
    assert runner.current().status == "done"


def test_skip_not_allowed_on_create(stores):
    runner = make_runner(stores, FakeYTMusic(songs("a")), RecordingClassifier())
    runner.start_create("Rock", "rock")
    with pytest.raises(JobStateError):
        runner.skip()


def test_update_missing_playlist_fails_clearly(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "b"))
    seed_playlist(fake, records, "PLgone", "Rock", included=["a"])
    assert fake.playlist_exists("PLgone")
    fake.deleted_playlists = {"PLgone"}  # the user deleted it on YouTube Music
    clf = RecordingClassifier()
    runner = make_runner(stores, fake, clf)
    runner.start_update(["PLgone"])
    job = runner.current()
    assert job.status == "failed"
    assert job.error == "The playlist “Rock” no longer exists on YouTube Music."
    assert job.missing_playlist_id == "PLgone"
    assert list(fake.created) == ["PLgone"]  # not recreated
    assert fake.added == {"PLgone": ["a"]}
    assert clf.sent == []

    runner.forget_playlist("PLgone")
    runner.cancel()
    assert records.list() == []
    assert runner.current() is None


# --- Pause / resume -------------------------------------------------------------------------

def test_usage_limit_pauses_and_resume_after_restart(stores):
    fake = FakeYTMusic(make_songs(250))
    runner = make_runner(stores, fake, FakeClassifier(fail_after=1))
    runner.start_create("Rock", "rock")
    job = runner.current()
    assert job.status == "paused"
    assert job.error == USAGE_MSG
    assert len(job.targets[0].decisions) == 100

    clf = RecordingClassifier()
    runner2 = make_runner(stores, fake, clf)
    assert runner2.current().status == "paused"
    runner2.resume()
    job = runner2.current()
    assert job.status == "review"
    assert job.error is None
    assert clf.sent == [f"v{i}" for i in range(100, 250)]
    assert len(job.targets[0].decisions) == 250
    assert fake.created == {}


def test_claude_not_logged_in_pauses(stores):
    class AuthFailing(FakeClassifier):
        def classify_batch(self, description, batch):
            raise ClaudeAuthError("not logged in")

    runner = make_runner(stores, FakeYTMusic(songs("a")), AuthFailing())
    runner.start_create("Rock", "rock")
    job = runner.current()
    assert (job.status, job.error) == ("paused", CLAUDE_AUTH_MSG)


def test_unexpected_error_fails_with_message(stores):
    class Broken(FakeClassifier):
        def classify_batch(self, description, batch):
            raise RuntimeError("disk on fire")

    runner = make_runner(stores, FakeYTMusic(songs("a")), Broken())
    runner.start_create("Rock", "rock")
    job = runner.current()
    assert (job.status, job.error) == ("failed", "disk on fire")


def test_youtube_auth_expired_during_fetch_pauses(stores):
    fake = FakeYTMusic(songs("a"))
    fake.expire_auth()
    runner = make_runner(stores, fake, RecordingClassifier())
    runner.start_create("Rock", "rock")
    assert (runner.current().status, runner.current().error) == ("paused", YT_AUTH_MSG)
    fake.connect("headers")
    runner.resume()
    assert runner.current().status == "review"


def test_youtube_auth_expired_pauses(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "b", "c"))
    runner = make_runner(stores, fake, RecordingClassifier({"a": "include", "b": "include"}))
    runner.start_create("Rock", "rock")
    fake.expire_auth()
    runner.approve(["a", "b"])
    job = runner.current()
    assert job.status == "paused"
    assert job.error == YT_AUTH_MSG
    assert job.targets[0].approved_ids == ["a", "b"]
    assert fake.created == {} and records.list() == []

    fake.connect("headers")
    runner2 = make_runner(stores, fake, RecordingClassifier())  # app restarted meanwhile
    runner2.resume()  # no re-approve needed
    assert runner2.current().status == "done"
    assert len(fake.created) == 1
    pid = next(iter(fake.created))
    assert fake.added == {pid: ["a", "b"]}
    assert set(records.get(pid).included) == {"a", "b"}


def test_add_failure_after_create_does_not_duplicate_playlist(stores):
    records, _ = stores
    all_songs = make_songs(120)
    ids = [s.video_id for s in all_songs]
    fake = FakeYTMusic(all_songs)
    runner = make_runner(stores, fake, RecordingClassifier({v: "include" for v in ids}))
    runner.start_create("Rock", "rock")
    fake.fail_add_at_chunk = 1
    runner.approve(ids)
    job = runner.current()
    assert job.status == "failed"
    pid = next(iter(fake.created))
    assert job.targets[0].created_playlist_id == pid
    assert job.targets[0].added_ids == ids[:50]
    assert set(records.get(pid).included) == set(ids[:50])  # record saved with the playlist id

    runner.resume()
    assert runner.current().status == "done"
    assert len(fake.created) == 1
    assert sorted(fake.added[pid]) == sorted(ids)
    assert len(fake.added[pid]) == 120
    assert set(records.get(pid).included) == set(ids)


def test_add_failure_right_after_create_reuses_playlist(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "b"))
    runner = make_runner(stores, fake, RecordingClassifier({"a": "include"}))
    runner.start_create("Rock", "rock")
    fake.fail_next_add = 1
    runner.approve(["a"])
    assert runner.current().status == "failed"
    pid = next(iter(fake.created))
    assert records.get(pid) is not None
    runner.resume()
    assert runner.current().status == "done"
    assert list(fake.created) == [pid]
    assert fake.added == {pid: ["a"]}


def test_app_closed_during_step_becomes_paused(stores):
    fake = FakeYTMusic(songs("a"))
    deferred = Deferred()
    runner = make_runner(stores, fake, RecordingClassifier(), run=deferred)
    runner.start_create("Rock", "rock")
    assert runner.current().status == "running"
    # app closed before the background step ran; a new process loads the saved job
    runner2 = make_runner(stores, fake, RecordingClassifier())
    job = runner2.current()
    assert (job.status, job.error) == ("paused", CLOSED_MSG)
    runner2.resume()
    assert runner2.current().status == "review"


def test_app_closed_while_applying_resumes_add(stores):
    fake = FakeYTMusic(songs("a"))
    deferred = Deferred()
    runner = make_runner(stores, fake, RecordingClassifier({"a": "include"}), run=deferred)
    runner.start_create("Rock", "rock")
    deferred.run()
    runner.approve(["a"])
    assert runner.current().status == "applying"
    runner2 = make_runner(stores, fake, RecordingClassifier())
    assert runner2.current().status == "paused"
    runner2.resume()
    assert runner2.current().status == "done"
    assert len(fake.created) == 1


# --- Busy / cancel / snapshot -----------------------------------------------------------------

def test_busy_rejects_second_job(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a"))
    seed_playlist(fake, records, "PL1", "Rock")
    deferred = Deferred()
    runner = make_runner(stores, fake, RecordingClassifier({"a": "include"}), run=deferred)
    runner.start_create("Rock", "rock")
    assert runner.current().status == "running"
    with pytest.raises(JobBusyError):
        runner.start_create("Other", "other")
    with pytest.raises(JobBusyError):
        runner.start_update(["PL1"])

    deferred.run()
    assert runner.current().status == "review"
    with pytest.raises(JobBusyError):  # review also blocks until cancelled or finished
        runner.start_create("Other", "other")

    runner.approve(["a"])
    assert runner.current().status == "applying"
    with pytest.raises(JobBusyError):
        runner.start_update(["PL1"])
    deferred.run()
    assert runner.current().status == "done"
    runner.start_update(["PL1"])  # a finished job no longer blocks
    deferred.run()
    assert runner.current().kind == "update"


def test_cancel_while_running_stops_at_next_batch(stores):
    records, jobs = stores
    fake = FakeYTMusic(make_songs(300))
    holder = {}

    class CancellingClassifier(RecordingClassifier):
        def classify_batch(self, description, batch):
            result = super().classify_batch(description, batch)
            if self.batches == 1:
                holder["runner"].cancel()  # user presses Cancel while batch 1 is in flight
            return result

    clf = CancellingClassifier()
    runner = make_runner(stores, fake, clf)
    holder["runner"] = runner
    runner.start_create("Rock", "rock")
    assert clf.batches == 1
    assert runner.current() is None
    assert jobs.load() is None
    assert fake.created == {} and records.list() == []


def test_actions_without_matching_job_raise(stores):
    runner = make_runner(stores, FakeYTMusic(songs("a")), RecordingClassifier())
    with pytest.raises(JobStateError):
        runner.approve(["a"])
    with pytest.raises(JobStateError):
        runner.resume()
    runner.cancel()  # no job: harmless
    runner.start_create("Rock", "rock")
    with pytest.raises(JobStateError):
        runner.resume()  # review is not resumable


def test_current_returns_snapshot(stores):
    runner = make_runner(stores, FakeYTMusic(songs("a")), RecordingClassifier())
    runner.start_create("Rock", "rock")
    snap = runner.current()
    snap.status = "done"
    snap.targets[0].songs.clear()
    job = runner.current()
    assert job.status == "review"
    assert len(job.targets[0].songs) == 1


def test_job_roundtrip_unicode(stores):
    _, jobs = stores
    fake = FakeYTMusic([Song("x1", 'Гурт "Океан Ельзи" — Обійми 🎵', ["Океан Ельзи"]), song("b")])
    runner = make_runner(stores, fake, RecordingClassifier({"x1": "include"}))
    runner.start_create("Українське", "український рок")
    saved = jobs.load()
    assert isinstance(saved, Job)
    assert saved == runner.current()
    assert Job.from_dict(saved.to_dict()) == saved
    assert saved.targets[0].songs[0].title == 'Гурт "Океан Ельзи" — Обійми 🎵'


def test_only_writes_to_record_playlist_ids(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "b"))
    runner = make_runner(stores, fake, RecordingClassifier({"a": "include", "c": "include"}))
    runner.start_create("Rock", "rock")
    runner.approve(["a"])
    fake.liked = songs("a", "b", "c", "d")
    pid = records.list()[0].playlist_id
    runner.start_update([pid])
    runner.approve(["c", "d"])
    record_ids = {r.playlist_id for r in records.list()}
    assert set(fake.added) <= record_ids
    assert set(fake.created) <= record_ids
    assert fake.added[pid] == ["a", "c", "d"]


def test_default_runs_in_background_thread(stores):
    import time

    records, jobs = stores
    fake = FakeYTMusic(make_songs(250))
    runner = JobRunner(records, jobs, fake, RecordingClassifier({"v1": "include"}))
    runner.start_create("Rock", "rock")
    deadline = time.monotonic() + 10
    while runner.current().status == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.current().status == "review"
    runner.approve(["v1"])
    while runner.current().status == "applying" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.current().status == "done"
    assert list(fake.added.values()) == [["v1"]]


# --- Fix round 1: songs already on the playlist (R8), crash windows, record invariants ---------

def test_update_song_already_on_playlist_is_not_re_added(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "c", "d"))
    seed_playlist(fake, records, "PL1", "Rock", included=["a"])
    fake.added["PL1"].append("c")  # the user hand-added liked song c on YouTube Music
    runner = make_runner(stores, fake, RecordingClassifier({"c": "include", "d": "include"}))
    runner.start_update(["PL1"])
    runner.approve(["c", "d"])
    job = runner.current()
    assert job.status == "done", job.error
    assert fake.added["PL1"] == ["a", "c", "d"]
    rec = records.get("PL1")
    assert set(rec.included) == {"a", "c", "d"}
    assert rec.included["c"].source == "claude"


def test_resume_after_lost_chunk_save_adds_no_duplicates(stores):
    records, _ = stores
    all_songs = make_songs(150)
    ids = [s.video_id for s in all_songs]
    fake = FakeYTMusic(all_songs)
    runner = make_runner(stores, fake, RecordingClassifier({v: "include" for v in ids}))
    runner.start_create("Rock", "rock")
    fake.fail_add_at_chunk = 1
    runner.approve(ids)
    assert runner.current().status == "failed"
    pid = next(iter(fake.created))
    # Simulated crash: YouTube accepted chunk 1, but neither the job nor the record saw it.
    fake.added[pid].extend(ids[50:100])
    assert runner.current().targets[0].added_ids == ids[:50]

    runner2 = make_runner(stores, fake, RecordingClassifier())
    runner2.resume()
    assert runner2.current().status == "done", runner2.current().error
    assert len(fake.created) == 1
    assert sorted(fake.added[pid]) == sorted(ids)
    assert set(records.get(pid).included) == set(ids)


def test_created_playlist_id_saved_before_record(stores, tmp_path):
    _, jobs = stores

    class FlakyRecords(RecordStore):
        fail_once = True

        def save(self, record):
            if self.fail_once:
                self.fail_once = False
                raise OSError("disk hiccup")
            super().save(record)

    records = FlakyRecords(tmp_path / "playlists")
    fake = FakeYTMusic(songs("a"))
    runner = JobRunner(records, jobs, fake, RecordingClassifier({"a": "include"}), run_in_background=sync)
    runner.start_create("Rock", "rock")
    runner.approve(["a"])
    job = runner.current()
    assert job.status == "failed"
    pid = next(iter(fake.created))
    assert jobs.load().targets[0].created_playlist_id == pid

    runner2 = JobRunner(records, jobs, fake, RecordingClassifier(), run_in_background=sync)
    runner2.resume()
    assert runner2.current().status == "done", runner2.current().error
    assert list(fake.created) == [pid]
    assert fake.added == {pid: ["a"]}
    assert set(records.get(pid).included) == {"a"}


def test_song_never_in_both_included_and_excluded(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "c", "d"))
    seed_playlist(fake, records, "PL1", "Rock", included=["a"])
    runner = make_runner(stores, fake, RecordingClassifier({"c": "include"}))
    runner.start_update(["PL1"])
    # The record changes while the review is open (e.g. another run): c excluded, d included.
    rec = records.get("PL1")
    rec.excluded["c"] = SongEntry("Title c", ["A"], "exclude", "earlier", "user")
    rec.included["d"] = SongEntry("Title d", ["A"], "include", "earlier", "claude")
    records.save(rec)
    runner.approve(["c"])
    rec = records.get("PL1")
    assert not set(rec.included) & set(rec.excluded)
    assert "c" in rec.included and "d" in rec.excluded


def test_youtube_auth_expires_mid_add_pauses_and_resumes(stores):
    records, _ = stores
    all_songs = make_songs(120)
    ids = [s.video_id for s in all_songs]
    fake = FakeYTMusic(all_songs)
    runner = make_runner(stores, fake, RecordingClassifier({v: "include" for v in ids}))
    runner.start_create("Rock", "rock")
    fake.expire_auth_after_chunk = 0
    runner.approve(ids)
    job = runner.current()
    assert (job.status, job.error) == ("paused", YT_AUTH_MSG)
    pid = next(iter(fake.created))
    assert fake.added[pid] == ids[:50]

    fake.connect("headers")
    runner.resume()
    assert runner.current().status == "done", runner.current().error
    assert len(fake.created) == 1
    assert sorted(fake.added[pid]) == sorted(ids)
    assert len(fake.added[pid]) == 120
    assert set(records.get(pid).included) == set(ids)


# --- Names YouTube Music refuses / forgetting a playlist the job uses ------------------------

NAME_MSG = "Playlist names can't contain < or > and must be 150 characters or fewer."


@pytest.mark.parametrize("name", ["Rock <3", "a>b", "x" * 151])
def test_start_create_rejects_names_youtube_music_refuses(stores, name):
    clf = RecordingClassifier()
    runner = make_runner(stores, FakeYTMusic(songs("a")), clf)
    with pytest.raises(ValueError) as exc:
        runner.start_create(name, "rock")
    assert str(exc.value) == NAME_MSG
    assert runner.current() is None and clf.sent == []


def test_start_create_accepts_a_150_character_name(stores):
    runner = make_runner(stores, FakeYTMusic(songs("a")), RecordingClassifier())
    runner.start_create("x" * 150, "rock")
    assert runner.current().status == "review"


def test_forget_refused_for_a_playlist_the_current_job_uses(stores):
    records, _ = stores
    fake = FakeYTMusic(songs("a", "b"))
    seed_playlist(fake, records, "PL1", "Rock")
    seed_playlist(fake, records, "PL2", "Jazz")
    seed_playlist(fake, records, "PL3", "Pop")
    runner = make_runner(stores, fake, RecordingClassifier())
    runner.start_update(["PL1", "PL2"])
    for status in ("review", "paused", "failed"):
        runner._job.status = status
        for pid in ("PL1", "PL2"):
            with pytest.raises(JobBusyError):
                runner.forget_playlist(pid)
    assert records.get("PL1") is not None and records.get("PL2") is not None
    runner.forget_playlist("PL3")  # not part of the job
    assert records.get("PL3") is None
    runner._job.status = "done"
    runner.forget_playlist("PL1")
    assert records.get("PL1") is None

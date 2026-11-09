"""Create / Update / Update all jobs: fetch likes, classify, review, then write to YouTube Music.

Safety rules enforced here:
- Nothing is written to YouTube Music before `approve()`; the only writes are
  `create_playlist` and `add_songs`, and `add_songs` only targets the record's playlist id
  or the playlist this job just created.
- Every step is saved to disk, so a pause (usage limit, sign-in expired) or an app restart
  continues where it stopped without redoing finished batches or creating a second playlist.
"""
import copy
import math
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from .classify import BATCH_SIZE, classify_pending
from .claude_cli import UNANSWERED_REASON, ClaudeAuthError, ClaudeUsageLimitError
from .models import Decision, PlaylistRecord, Song, SongEntry
from .store import JobStore, RecordStore
from .ytm import PlaylistMissingError, YouTubeAuthError

JobStatus = Literal["running", "review", "applying", "paused", "failed", "done"]

USAGE_LIMIT_MSG = "Claude usage limit reached. Your progress is saved — press Resume later."
CLAUDE_AUTH_MSG = "Claude isn't logged in. Open a terminal, run `claude`, and log in, then press Resume."
YOUTUBE_AUTH_MSG = "YouTube Music sign-in expired — reconnect, then press Resume."
APP_CLOSED_MSG = "The app was closed during this step — press Resume."
NO_NEW_SONGS = "No new liked songs since last update"
NO_LIKED_SONGS = "You don't have any liked songs yet."
FETCH_MSG = "Fetching your liked songs…"
CLASSIFY_MSG = "Asking Claude which songs fit…"
NAME_MAX = 150  # YouTube Music's limit for a playlist title
BAD_NAME_MSG = "Playlist names can't contain < or > and must be 150 characters or fewer."
JOB_WORKING_MSG = "A job is working right now — wait for it to finish or cancel it first."
IN_JOB_MSG = "This playlist is part of the current job — finish or cancel that first."


def check_playlist_name(name: str) -> None:
    """Raise ValueError for a name YouTube Music would refuse (only at approve time, otherwise)."""
    if "<" in name or ">" in name or len(name) > NAME_MAX:
        raise ValueError(BAD_NAME_MSG)


def missing_playlist_message(name: str) -> str:
    return f"The playlist “{name}” no longer exists on YouTube Music."


def skipped_missing_message(name: str) -> str:
    return f"The playlist “{name}” no longer exists on YouTube Music, so it was skipped."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Progress:
    stage: str  # fetch | classify | review | create | add | done
    message: str
    current: int | None = None  # None = indeterminate bar
    total: int | None = None

    def to_dict(self) -> dict:
        return {"stage": self.stage, "message": self.message, "current": self.current, "total": self.total}

    @classmethod
    def from_dict(cls, d: dict) -> "Progress":
        return cls(d["stage"], d["message"], d.get("current"), d.get("total"))


@dataclass
class Target:
    """One playlist being created (playlist_id=None) or updated within a job."""

    playlist_id: str | None
    name: str
    description: str
    songs: list[Song] = field(default_factory=list)  # songs to review (Update: only new ones)
    decisions: dict[str, Decision] = field(default_factory=dict)  # Claude's verdicts by video id
    approved: bool = False  # applied: written to YouTube and to the record
    skipped: bool = False  # left unchanged (no new songs, the user pressed Skip, or missing)
    missing: bool = False  # Update all: deleted on YouTube Music, so skipped
    approved_ids: list[str] | None = None  # the user's choice, saved before any write
    created_playlist_id: str | None = None  # Create: set as soon as the playlist exists
    added_ids: list[str] = field(default_factory=list)  # approved ids already added on YouTube

    def is_classified(self) -> bool:
        return all(s.video_id in self.decisions for s in self.songs)

    def to_dict(self) -> dict:
        return {
            "playlist_id": self.playlist_id,
            "name": self.name,
            "description": self.description,
            "songs": [s.to_dict() for s in self.songs],
            "decisions": {k: {"verdict": d.verdict, "reason": d.reason} for k, d in self.decisions.items()},
            "approved": self.approved,
            "skipped": self.skipped,
            "missing": self.missing,
            "approved_ids": self.approved_ids,
            "created_playlist_id": self.created_playlist_id,
            "added_ids": list(self.added_ids),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Target":
        return cls(
            playlist_id=d.get("playlist_id"),
            name=d["name"],
            description=d.get("description", ""),
            songs=[Song.from_dict(s) for s in d.get("songs", [])],
            decisions={k: Decision(k, v["verdict"], v.get("reason", "")) for k, v in d.get("decisions", {}).items()},
            approved=d.get("approved", False),
            skipped=d.get("skipped", False),
            missing=d.get("missing", False),
            approved_ids=d.get("approved_ids"),
            created_playlist_id=d.get("created_playlist_id"),
            added_ids=list(d.get("added_ids", [])),
        )


@dataclass
class Job:
    id: str
    kind: Literal["create", "update"]
    status: JobStatus
    targets: list[Target]
    review_index: int  # the target being reviewed / applied
    progress: Progress
    error: str | None = None
    notice: str | None = None
    # A target deleted on YouTube: the failure of a single-playlist job, or (Update all) the
    # skipped playlist to offer "Forget" for while the job goes on.
    missing_playlist_id: str | None = None
    fetched: bool = False  # liked songs fetched and split into targets

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "targets": [t.to_dict() for t in self.targets],
            "review_index": self.review_index,
            "progress": self.progress.to_dict(),
            "error": self.error,
            "notice": self.notice,
            "missing_playlist_id": self.missing_playlist_id,
            "fetched": self.fetched,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls(
            id=d["id"],
            kind=d["kind"],
            status=d["status"],
            targets=[Target.from_dict(t) for t in d.get("targets", [])],
            review_index=d.get("review_index", 0),
            progress=Progress.from_dict(d["progress"]),
            error=d.get("error"),
            notice=d.get("notice"),
            missing_playlist_id=d.get("missing_playlist_id"),
            fetched=d.get("fetched", False),
        )


class JobBusyError(Exception):
    """Another job is running or waiting for the user (review / paused / failed)."""


class JobStateError(Exception):
    """The action doesn't fit the current job (none, or wrong status)."""


class _Cancelled(Exception):
    """The job was cancelled (or replaced) while a background step was running."""


class _PlaylistGone(Exception):
    def __init__(self, target: Target):
        super().__init__(target.name)
        self.target = target


def _start_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


class JobRunner:
    """Runs one job at a time. API-facing methods may be called from any thread.

    `current()` returns a deep copy of the job (a consistent snapshot); changing it has no effect.
    """

    def __init__(self, records: RecordStore, jobs: JobStore, ytm, classifier,
                 run_in_background: Callable[[Callable[[], None]], None] = _start_thread):
        self.records = records
        self.jobs = jobs
        self.ytm = ytm
        self.classifier = classifier
        self._run_in_background = run_in_background
        self._lock = threading.RLock()
        self._job: Job | None = jobs.load()
        if self._job is not None and self._job.status in ("running", "applying"):
            self._job.status = "paused"
            self._job.error = APP_CLOSED_MSG
            jobs.save(self._job)

    # --- API ------------------------------------------------------------------------------

    def current(self) -> Job | None:
        with self._lock:
            return copy.deepcopy(self._job)

    def start_create(self, name: str, description: str) -> Job:
        check_playlist_name(name)
        return self._start("create", [Target(None, name, description)])

    def start_update(self, playlist_ids: list[str]) -> Job:
        if not playlist_ids:
            raise ValueError("There are no playlists to update.")
        targets = []
        for pid in dict.fromkeys(playlist_ids):
            record = self.records.get(pid)
            if record is None:
                raise KeyError(pid)
            targets.append(Target(pid, record.name, record.description))
        return self._start("update", targets)

    def approve(self, video_ids: list[str]) -> None:
        with self._lock:
            job = self._require("review")
            target = job.targets[job.review_index]
            wanted = set(video_ids)
            ids = [s.video_id for s in target.songs if s.video_id in wanted]  # unknown ids ignored
            if target.playlist_id is None and not ids:
                raise ValueError("Pick at least one song to create the playlist.")
            target.approved_ids = ids
            job.status = "applying"
            self.jobs.save(job)
        self._run_in_background(lambda: self._work(job))

    def skip(self) -> None:
        with self._lock:
            job = self._require("review")
            if job.kind != "update":
                raise JobStateError("Only an update can skip a playlist.")
            job.targets[job.review_index].skipped = True
            self._advance(job)

    def cancel(self) -> None:
        """Discard the job. Never writes to YouTube; a running step stops at its next checkpoint."""
        with self._lock:
            self._job = None
            self.jobs.clear()

    def resume(self) -> None:
        with self._lock:
            job = self._require("paused", "failed")
            job.status = "applying" if self._apply_pending(job) else "running"
            job.error = None
            if not self._missing_is_skipped(job):
                job.missing_playlist_id = None  # it is retried (and reported again if still gone)
            self.jobs.save(job)
        self._run_in_background(lambda: self._work(job))

    def forget_playlist(self, playlist_id: str) -> None:
        """Remove the local record only (the playlist was deleted on YouTube Music).

        Refused (JobBusyError) while a job is working, or while an unfinished job still needs
        this playlist, except when it is the one the job found missing (the Forget offer).
        """
        with self._lock:
            job = self._job
            if job is not None and job.status in ("running", "applying"):
                raise JobBusyError(JOB_WORKING_MSG)
            if (job is not None and job.status != "done" and playlist_id != job.missing_playlist_id
                    and any(t.playlist_id == playlist_id for t in job.targets)):
                raise JobBusyError(IN_JOB_MSG)
            self.records.delete(playlist_id)
            if job is not None and job.missing_playlist_id == playlist_id and self._missing_is_skipped(job):
                # Update all goes on; offer the next deleted playlist, if any (no cancel).
                job.missing_playlist_id = next(
                    (t.playlist_id for t in job.targets if t.missing and t.playlist_id != playlist_id
                     and self.records.get(t.playlist_id) is not None), None)
                self.jobs.save(job)

    # --- helpers (callers of _commit/_advance hold the lock) ------------------------------

    def _start(self, kind: Literal["create", "update"], targets: list[Target]) -> Job:
        with self._lock:
            job = self._job
            if job is not None and job.status != "done":
                if job.status in ("running", "applying"):
                    raise JobBusyError("Another job is already running.")
                raise JobBusyError("Finish or cancel the current job first.")
            job = Job(uuid.uuid4().hex, kind, "running", targets, 0, Progress("fetch", FETCH_MSG))
            self._job = job
            self.jobs.save(job)
            snapshot = copy.deepcopy(job)
        self._run_in_background(lambda: self._work(job))
        return snapshot

    def _require(self, *statuses: str) -> Job:
        job = self._job
        if job is None or job.status not in statuses:
            raise JobStateError("There's no job waiting for this action.")
        return job

    def _commit(self, job: Job) -> None:
        if self._job is not job:
            raise _Cancelled()
        self.jobs.save(job)

    @staticmethod
    def _missing_is_skipped(job: Job) -> bool:
        """The missing playlist was skipped (Update all) rather than being what stopped the job."""
        return job.missing_playlist_id is not None and any(
            t.missing and t.playlist_id == job.missing_playlist_id for t in job.targets)

    def _skip_missing(self, job: Job, t: Target) -> None:
        """Update all: leave a deleted playlist alone and carry on with the others."""
        t.skipped = t.missing = True
        if job.missing_playlist_id is None:
            job.missing_playlist_id = t.playlist_id
        text = skipped_missing_message(t.name)
        if not job.notice:
            job.notice = text
        else:
            job.notice = job.notice + (" " if job.notice.endswith((".", "!", "?")) else ". ") + text
        self._commit(job)

    @staticmethod
    def _apply_pending(job: Job) -> bool:
        if job.review_index >= len(job.targets):
            return False
        t = job.targets[job.review_index]
        return t.approved_ids is not None and not t.approved and not t.skipped

    def _advance(self, job: Job) -> None:
        """Move to the next target needing review, or finish."""
        nxt = next((i for i, t in enumerate(job.targets) if not t.skipped and not t.approved), None)
        if nxt is None:
            job.status = "done"
            job.progress = Progress("done", "All done.")
        else:
            job.review_index = nxt
            job.status = "review"
            job.progress = Progress("review", "Ready for your review.")
        self._commit(job)

    def _stop(self, job: Job, status: JobStatus, error: str, missing_id: str | None = None) -> None:
        with self._lock:
            if self._job is not job:
                return
            job.status = status
            job.error = error
            if missing_id is not None:  # otherwise keep a skipped missing playlist's id
                job.missing_playlist_id = missing_id
            self.jobs.save(job)

    # --- background work --------------------------------------------------------------------

    def _work(self, job: Job) -> None:
        try:
            if self._apply_pending(job):
                target = job.targets[job.review_index]
                try:
                    self._apply(job, target)
                except _PlaylistGone:
                    if len(job.targets) == 1 or target.playlist_id is None:
                        raise
                    with self._lock:  # deleted after the review: skip it, go on with the others
                        self._skip_missing(job, target)
                with self._lock:
                    self._advance(job)
            else:
                self._classify(job)
        except _Cancelled:
            pass
        except ClaudeUsageLimitError:
            self._stop(job, "paused", USAGE_LIMIT_MSG)
        except ClaudeAuthError:
            self._stop(job, "paused", CLAUDE_AUTH_MSG)
        except YouTubeAuthError:
            self._stop(job, "paused", YOUTUBE_AUTH_MSG)
        except _PlaylistGone as gone:
            t = gone.target
            self._stop(job, "failed", missing_playlist_message(t.name), t.playlist_id or t.created_playlist_id)
        except Exception as exc:  # noqa: BLE001 — shown to the user; work so far is saved
            self._stop(job, "failed", str(exc) or exc.__class__.__name__)

    def _fetch(self, job: Job) -> None:
        liked = self.ytm.get_liked_songs()
        with self._lock:
            if self._job is not job:
                raise _Cancelled()
            for t in job.targets:
                if t.playlist_id is None:
                    t.songs = list(liked)
                else:
                    record = self.records.get(t.playlist_id)
                    if record is None:
                        raise _PlaylistGone(t)
                    checked = record.checked_ids()
                    t.songs = [s for s in liked if s.video_id not in checked]
                t.skipped = not t.songs
            job.fetched = True
            skipped = [t.name for t in job.targets if t.skipped]
            if len(skipped) == len(job.targets):
                job.notice = NO_LIKED_SONGS if job.kind == "create" else NO_NEW_SONGS
                job.status = "done"
                job.progress = Progress("done", job.notice)
            elif skipped:
                job.notice = f"{NO_NEW_SONGS}: {', '.join(skipped)}"
            self._commit(job)

    def _classify(self, job: Job) -> None:
        if not job.fetched:
            with self._lock:
                job.progress = Progress("fetch", FETCH_MSG)
                self._commit(job)
            self._fetch(job)
            if job.status == "done":
                return
        many = len(job.targets) > 1
        for t in job.targets:
            if t.skipped or t.is_classified():
                continue
            if t.playlist_id is not None and not self.ytm.playlist_exists(t.playlist_id):
                if not many:
                    raise _PlaylistGone(t)
                with self._lock:
                    if self._job is not job:
                        raise _Cancelled()
                    t.songs = []  # never reviewed: not counted in "Playlist i of n"
                    self._skip_missing(job, t)
                continue
            prefix = f"{t.name}: " if many else ""
            with self._lock:
                work = dict(t.decisions)  # classify_pending mutates this copy, not the shared job
                pending = sum(1 for s in t.songs if s.video_id not in work)
                job.progress = Progress("classify", prefix + CLASSIFY_MSG, 0, math.ceil(pending / BATCH_SIZE))
                self._commit(job)

            def on_batch_done(i: int, n: int, t: Target = t, work: dict = work, prefix: str = prefix) -> None:
                with self._lock:
                    if self._job is not job:
                        raise _Cancelled()
                    t.decisions = dict(work)
                    job.progress = Progress("classify", f"{prefix}{CLASSIFY_MSG} batch {i} of {n}", i, n)
                    self._commit(job)

            classify_pending(t.description, t.songs, work, self.classifier, on_batch_done)
        with self._lock:
            self._advance(job)  # -> review of the first target not skipped

    def _entry(self, t: Target, s: Song, checked: bool) -> SongEntry:
        d = t.decisions.get(s.video_id) or Decision(s.video_id, "unsure", UNANSWERED_REASON)
        claude_default = d.verdict == "include"
        source = "claude" if checked == claude_default else "user"
        return SongEntry(s.title, list(s.artists), d.verdict, d.reason, source)

    @staticmethod
    def _unanswered(t: Target, s: Song) -> bool:
        d = t.decisions.get(s.video_id)
        return d is None or d.reason == UNANSWERED_REASON

    @staticmethod
    def _put(record: PlaylistRecord, video_id: str, entry: SongEntry, checked: bool) -> None:
        """A song is in exactly one of included / excluded."""
        (record.included if checked else record.excluded)[video_id] = entry
        (record.excluded if checked else record.included).pop(video_id, None)

    def _apply(self, job: Job, t: Target) -> None:
        """Write the approved songs of `t`, resumably (R7/R8): never a second playlist, never a re-add."""
        approved = list(t.approved_ids or [])
        fresh = False
        if t.playlist_id is None and t.created_playlist_id is None:
            with self._lock:
                job.progress = Progress("create", "Creating playlist on YouTube Music…")
                self._commit(job)
            new_id = self.ytm.create_playlist(t.name, t.description)
            fresh = True
            with self._lock:  # persist the id before anything else, so a resume never creates another
                t.created_playlist_id = new_id
                if self._job is job:
                    self.jobs.save(job)
            now = _now()
            # Record too, so a retry (or a cancel) still knows this playlist is ours.
            self.records.save(PlaylistRecord(new_id, t.name, t.description, now, now))
            with self._lock:
                self._commit(job)
        pid = t.playlist_id or t.created_playlist_id
        record = self.records.get(pid)
        if record is None and t.playlist_id is None:  # crashed between saving the job and the record
            now = _now()
            record = PlaylistRecord(pid, t.name, t.description, now, now)
            self.records.save(record)
        if record is None:
            raise LookupError(f"“{t.name}” is no longer in your list of playlists, so nothing was added.")
        try:
            # R8: songs already on the playlist (hand-added, or a chunk whose save was lost) count as added.
            on_playlist = set() if fresh else self.ytm.get_playlist_video_ids(pid)
        except PlaylistMissingError as exc:
            raise _PlaylistGone(t) from exc
        by_id = {s.video_id: s for s in t.songs}
        already = set(t.added_ids) | set(record.included) | on_playlist
        remaining = [v for v in approved if v not in already]
        total, base = len(approved), len(approved) - len(remaining)
        with self._lock:
            recorded = set(t.added_ids)
            t.added_ids.extend(v for v in approved if v in already and v not in recorded)
            job.progress = Progress("add", f"Adding songs… {base} of {total}", base, total)
            self._commit(job)
        sent = 0

        def on_progress(done: int, _total: int) -> None:
            nonlocal sent
            newly, sent = remaining[sent:done], done
            for v in newly:  # these are on YouTube now: record them even if the job was cancelled
                self._put(record, v, self._entry(t, by_id[v], True), True)
            self.records.save(record)
            with self._lock:
                if self._job is not job:
                    raise _Cancelled()
                t.added_ids.extend(newly)
                job.progress = Progress("add", f"Adding songs… {base + done} of {total}", base + done, total)
                self._commit(job)

        if remaining:
            try:
                self.ytm.add_songs(pid, remaining, on_progress)
            except PlaylistMissingError as exc:
                raise _PlaylistGone(t) from exc
        chosen = set(approved)
        for s in t.songs:
            checked = s.video_id in chosen
            if not checked and self._unanswered(t, s):
                continue  # Claude never judged it: leave it unrecorded so the next Update asks again
            self._put(record, s.video_id, self._entry(t, s, checked), checked)
        record.updated_at = _now()
        self.records.save(record)
        with self._lock:
            if self._job is not job:
                raise _Cancelled()
            t.approved = True

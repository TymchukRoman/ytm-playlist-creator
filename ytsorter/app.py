"""The local JSON API (FastAPI) over the job runner, plus the static UI at `/`.

Every error leaves as `{"error": "<plain-language message>"}`; tracebacks stay in the log.
"""
import dataclasses
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import demo_data
from .claude_cli import ClaudeClassifier
from .fakes import FakeYTMusic
from .jobs import Job, JobBusyError, JobRunner, JobStateError, Target
from .models import Song
from .paths import data_dir, job_file, playlists_dir, youtube_auth_file
from .store import JobStore, RecordStore
from .ytm import YouTubeAuthError, YTMusicClient

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
STATUS_TTL = 10.0  # seconds the Claude CLI check (a subprocess) is reused
_PLAYLIST_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")  # ids double as filenames: no path tricks


class ConnectBody(BaseModel):
    headers: str


class CreateBody(BaseModel):
    name: str
    description: str


class ApproveBody(BaseModel):
    video_ids: list[str]


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _song_json(song: Song, reason: str) -> dict:
    return {"video_id": song.video_id, "title": song.title, "artists": list(song.artists),
            "album": song.album, "reason": reason}


def _review_json(job: Job) -> dict:
    """The target waiting for the user's decision, its songs split by Claude's verdict."""
    target: Target = job.targets[job.review_index]
    sections: dict[str, list[dict]] = {"include": [], "unsure": [], "exclude": []}
    for song in target.songs:
        decision = target.decisions.get(song.video_id)
        verdict, reason = (decision.verdict, decision.reason) if decision else ("unsure", "")
        sections[verdict if verdict in sections else "unsure"].append(_song_json(song, reason))
    shown = [i for i, t in enumerate(job.targets) if t.songs]  # playlists without new songs are skipped
    index = shown.index(job.review_index) + 1 if job.review_index in shown else 1
    return {"target_name": target.name, "index": index, "count": max(len(shown), 1), **sections}


def _job_json(job: Job | None) -> dict | None:
    if job is None:
        return None
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "progress": job.progress.to_dict(),
        "error": job.error,
        "notice": job.notice,
        "missing_playlist_id": job.missing_playlist_id,
        "targets": [{"playlist_id": t.playlist_id, "name": t.name, "skipped": t.skipped,
                     "approved": t.approved} for t in job.targets],
        "review": _review_json(job) if job.status == "review" else None,
    }


def create_app(runner: JobRunner, ytm, classifier, records: RecordStore, *, demo: bool = False,
               clock: Callable[[], float] = time.monotonic) -> FastAPI:
    app = FastAPI(title="YT Music Sorter", docs_url=None, redoc_url=None, openapi_url=None)

    # --- errors: always plain JSON ------------------------------------------------------------

    @app.exception_handler(JobBusyError)
    async def _busy(_: Request, exc: JobBusyError):
        return _error(409, str(exc))

    @app.exception_handler(JobStateError)
    async def _state(_: Request, exc: JobStateError):
        return _error(409, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, __: RequestValidationError):
        return _error(400, "That request was missing something or had the wrong format.")

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        message = "Not found." if exc.status_code == 404 else str(exc.detail)
        return _error(exc.status_code, message)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        log.error("Unhandled error", exc_info=exc)
        return _error(500, "Something went wrong inside the app. Please try again.")

    def known_playlist(playlist_id: str) -> None:
        """404 unless this is an id of a saved record (validated before it is used anywhere)."""
        if not _PLAYLIST_ID.match(playlist_id) or records.get(playlist_id) is None:
            raise StarletteHTTPException(404, "That playlist isn't known to this app.")

    # --- status -------------------------------------------------------------------------------

    claude_cache: dict = {"at": None, "value": None}
    claude_lock = threading.Lock()

    def claude_status(refresh: bool) -> dict:
        with claude_lock:
            now = clock()
            stale = claude_cache["at"] is None or now - claude_cache["at"] >= STATUS_TTL
            if refresh or stale:
                claude_cache["value"] = dataclasses.asdict(classifier.status())
                claude_cache["at"] = now
            return claude_cache["value"]

    @app.get("/api/status")
    def status(refresh: int = 0):
        return {"claude": claude_status(bool(refresh)),
                "youtube": {"connected": ytm.is_connected()},
                "demo": demo}

    @app.post("/api/youtube/connect")
    def connect(body: ConnectBody):
        if not body.headers.strip():
            return _error(400, "Paste the request headers from your browser first.")
        try:
            ytm.connect(body.headers)
        except YouTubeAuthError as exc:
            return _error(400, str(exc))
        return {"connected": True}

    @app.post("/api/youtube/disconnect")
    def disconnect():
        ytm.disconnect()
        return {"connected": False}

    # --- playlists ----------------------------------------------------------------------------

    @app.get("/api/playlists")
    def playlists():
        return [{"playlist_id": r.playlist_id, "name": r.name, "description": r.description,
                 "song_count": len(r.included), "updated_at": r.updated_at}
                for r in records.list()]

    @app.post("/api/playlists")
    def create_playlist(body: CreateBody):
        name, description = body.name.strip(), body.description.strip()
        if not name:
            return _error(400, "Give the playlist a name.")
        if not description:
            return _error(400, "Describe which songs belong in the playlist.")
        runner.start_create(name, description)
        return {"started": True}

    @app.post("/api/playlists/{playlist_id}/update")
    def update_playlist(playlist_id: str):
        known_playlist(playlist_id)
        runner.start_update([playlist_id])
        return {"started": True}

    @app.post("/api/update-all")
    def update_all():
        ids = [r.playlist_id for r in records.list()]
        if not ids:
            return _error(400, "You don't have any playlists to update yet.")
        runner.start_update(ids)
        return {"started": True}

    @app.delete("/api/playlists/{playlist_id}")
    def forget_playlist(playlist_id: str):
        known_playlist(playlist_id)
        runner.forget_playlist(playlist_id)
        return {"forgotten": True}

    # --- the job ------------------------------------------------------------------------------

    @app.get("/api/job")
    def job():
        return _job_json(runner.current())

    @app.post("/api/job/approve")
    def approve(body: ApproveBody):
        try:
            runner.approve(body.video_ids)
        except ValueError as exc:
            return _error(400, str(exc))
        return {"ok": True}

    @app.post("/api/job/skip")
    def skip():
        runner.skip()
        return {"ok": True}

    @app.post("/api/job/cancel")
    def cancel():
        runner.cancel()
        return {"ok": True}

    @app.post("/api/job/resume")
    def resume():
        runner.resume()
        return {"ok": True}

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                   include_in_schema=False)
    def unknown_api(rest: str):
        return _error(404, "Not found.")

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def build_default_app() -> FastAPI:
    """The real app: YouTube Music + Claude CLI, data in the per-user folder.

    `YTSORTER_DEMO=1`: sample songs and a pretend Claude, with records kept apart in `demo/`.
    """
    demo = os.environ.get("YTSORTER_DEMO") == "1"
    if demo:
        base = data_dir() / "demo"
        records, jobs = RecordStore(base / "playlists"), JobStore(base / "job.json")
        ytm, classifier = FakeYTMusic(list(demo_data.SAMPLE_SONGS)), demo_data.DemoClassifier()
    else:
        records, jobs = RecordStore(playlists_dir()), JobStore(job_file())
        ytm, classifier = YTMusicClient(youtube_auth_file()), ClaudeClassifier()
    runner = JobRunner(records, jobs, ytm, classifier)  # real background threads
    return create_app(runner, ytm, classifier, records, demo=demo)

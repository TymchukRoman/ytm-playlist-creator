"""The local JSON API (FastAPI) over the job runner, plus the static UI at `/`.

Every error leaves as `{"error": "<plain-language message>"}`; tracebacks stay in the log.
"""
import dataclasses
import logging
import os
import re
import shutil
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
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import demo_data
from .claude_cli import ClaudeClassifier
from .fakes import FakeYTMusic
from .jobs import Job, JobBusyError, JobRunner, JobStateError, Target
from .models import Song
from .paths import data_dir, job_file, playlists_dir, settings_file, youtube_auth_file
from .settings import BAD_SETTINGS_MSG, ClaudeSettings, SettingsStore, options_json
from .store import JobStore, RecordStore
from .ytm import YouTubeAuthError, YTMusicClient

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
STATUS_TTL = 10.0  # seconds the Claude CLI check (a subprocess) is reused
_PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")  # ids double as filenames: no path tricks


class ConnectBody(BaseModel):
    headers: str


class CreateBody(BaseModel):
    name: str
    description: str


class ApproveBody(BaseModel):
    video_ids: list[str]


class SettingsBody(BaseModel):
    model: str
    effort: str


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


ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
_BLOCKED = "Request blocked: it didn't come from this app's page."
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _same_origin_ok(request: Request) -> bool:
    """Writes must come from this app's own page (CSRF); plain clients send neither header."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if origin is not None:
        host = request.headers.get("host", "")
        port = host.rpartition(":")[2] if ":" in host else ""
        suffix = f":{port}" if port else ""
        return origin in (f"http://127.0.0.1{suffix}", f"http://localhost{suffix}")
    return True


def create_app(runner: JobRunner, ytm, classifier, records: RecordStore, *, demo: bool = False,
               clock: Callable[[], float] = time.monotonic, settings: SettingsStore | None = None) -> FastAPI:
    settings = settings or SettingsStore(None)
    app = FastAPI(title="YT Music Sorter", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _block_cross_origin_writes(request: Request, call_next):
        if (request.method in _WRITE_METHODS and request.url.path.startswith("/api")
                and not _same_origin_ok(request)):
            return _error(403, _BLOCKED)
        return await call_next(request)

    # Added last = outermost: a DNS-rebinding page (foreign Host header) is refused before anything else.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

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
        if not _PLAYLIST_ID.fullmatch(playlist_id) or records.get(playlist_id) is None:
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
            return _error(400, "Paste what you copied from your browser first.")
        try:
            ytm.connect(body.headers)
        except YouTubeAuthError as exc:
            return _error(400, str(exc))
        return {"connected": True}

    @app.post("/api/youtube/disconnect")
    def disconnect():
        current = runner.current()
        if current is not None and current.status in ("running", "applying"):
            return _error(409, "Wait for the current job to finish or cancel it first.")
        ytm.disconnect()
        return {"connected": False}

    # --- Claude model and effort ---------------------------------------------------------------

    @app.get("/api/settings")
    def get_settings():
        return {**dataclasses.asdict(settings.load()), **options_json()}

    @app.put("/api/settings")
    def put_settings(body: SettingsBody):
        try:
            choice = ClaudeSettings(body.model, body.effort)
        except ValueError:
            return _error(400, BAD_SETTINGS_MSG)
        settings.save(choice)
        return dataclasses.asdict(choice)

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
        try:
            runner.start_create(name, description)
        except ValueError as exc:  # a name YouTube Music would refuse
            return _error(400, str(exc))
        return {"started": True}

    @app.post("/api/playlists/{playlist_id}/update")
    def update_playlist(playlist_id: str):
        known_playlist(playlist_id)
        try:
            runner.start_update([playlist_id])
        except KeyError:  # forgotten between the check and the start
            return _error(404, "That playlist isn't known to this app.")
        return {"started": True}

    @app.post("/api/update-all")
    def update_all():
        ids = [r.playlist_id for r in records.list()]
        if not ids:
            return _error(400, "You don't have any playlists to update yet.")
        try:
            runner.start_update(ids)
        except KeyError:
            return _error(404, "That playlist isn't known to this app.")
        return {"started": True}

    @app.delete("/api/playlists/{playlist_id}")
    def forget_playlist(playlist_id: str):
        known_playlist(playlist_id)
        runner.forget_playlist(playlist_id)  # JobBusyError (409) while a job works on / needs it
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
        shutil.rmtree(base, ignore_errors=True)  # demo only: records must match the fresh fake library
        records, jobs = RecordStore(base / "playlists"), JobStore(base / "job.json")
        settings = SettingsStore(base / "settings.json")
        ytm, classifier = FakeYTMusic(list(demo_data.SAMPLE_SONGS)), demo_data.DemoClassifier()
    else:
        records, jobs = RecordStore(playlists_dir()), JobStore(job_file())
        settings = SettingsStore(settings_file())
        ytm, classifier = YTMusicClient(youtube_auth_file()), ClaudeClassifier(settings=settings.load)
    runner = JobRunner(records, jobs, ytm, classifier)  # real background threads
    return create_app(runner, ytm, classifier, records, demo=demo, settings=settings)

"""Decide which songs fit a playlist description by calling the Claude CLI (`claude -p`)."""
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from . import paths
from .models import Decision, Song
from .settings import ClaudeSettings

# runner(args, stdin_text) for a classify call; runner(args, stdin_text, timeout=...) for the status check.
Runner = Callable[..., subprocess.CompletedProcess]

UNANSWERED_REASON = "Claude didn't answer for this song"
UNREADABLE_MSG = "Claude's answer couldn't be read. Your progress is saved — press Resume to try again."
TIMEOUT_MSG = "Claude took too long to answer. Your progress is saved — press Resume to try again."
TOO_OLD_MSG = "Your Claude Code is too old for this app. Run `claude update`, then try again."
LOGIN_STEPS = "Open a terminal, run `claude`, and log in"
NOT_LOGGED_IN_MSG = f"Claude isn't logged in. {LOGIN_STEPS}, then press Check again."
STATUS_SLOW_MSG = (f"Checking whether Claude is logged in took too long. {LOGIN_STEPS} if you haven't yet, "
                   "then press Check again.")
CLASSIFY_TIMEOUT = 300  # seconds for one batch
STATUS_TIMEOUT = 20  # seconds for `claude auth status`
_JSON_SHAPE = '{"decisions":[{"n":<int>,"verdict":"include|exclude|unsure","reason":"..."}]}'
_VERDICTS = ("include", "exclude", "unsure")

SYSTEM_PROMPT = (
    "You sort a user's liked songs into a playlist they described. "
    "Judge only from the metadata given for each song and your own knowledge of the music. "
    "If you cannot tell whether a song fits, answer 'unsure' rather than guess. "
    "Keep each reason to 15 words or fewer. "
    "Return exactly one decision per song number."
)

SCHEMA_JSON = (
    '{"type":"object","properties":{"decisions":{"type":"array","items":{"type":"object",'
    '"properties":{"n":{"type":"integer"},"verdict":{"enum":["include","exclude","unsure"]},'
    '"reason":{"type":"string"}},"required":["n","verdict","reason"]}}},"required":["decisions"]}'
)


class ClaudeError(Exception):
    """The Claude CLI failed or returned something unusable."""


class ClaudeTemporaryError(ClaudeError):
    """A hiccup worth retrying later (unreadable answer, timeout): the job pauses, not fails."""


class ClaudeUsageLimitError(ClaudeError):
    """The Claude usage / rate limit was hit."""


class ClaudeAuthError(ClaudeError):
    """The Claude CLI is not logged in (or its session expired)."""


@dataclass(frozen=True)
class ClaudeStatus:
    installed: bool
    logged_in: bool
    message: str


def default_runner(args: list[str], stdin_text: str, timeout: float = CLASSIFY_TIMEOUT) -> subprocess.CompletedProcess:
    # cwd is an empty folder of our own: no project CLAUDE.md / settings apply, and the
    # folder holding the YouTube sign-in file is never Claude's working directory.
    return subprocess.run(
        args,
        input=stdin_text,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=paths.claude_work_dir(),
    )


def _build_prompt(description: str, songs: list[Song]) -> str:
    items = [
        {"n": i, "title": s.title, "artists": s.artists, "album": s.album, "duration": s.duration}
        for i, s in enumerate(songs, start=1)
    ]
    return (
        f"Playlist description: {description}\n\n"
        "For each numbered song below, decide whether it belongs in that playlist "
        "(include, exclude or unsure) and give a short reason. "
        f"Answer with only a JSON object of this exact shape: {_JSON_SHAPE}\n"
        "Songs as a JSON array:\n"
        f"{json.dumps(items, ensure_ascii=False)}"
    )


def _parse_json_text(text: str) -> object | None:
    """json.loads that tolerates a ```json fence or prose around the object."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates = [text]
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def _looks_too_old(text: str) -> bool:
    """An older Claude Code doesn't know one of the options or commands this app uses."""
    lowered = text.lower()
    return "unknown option" in lowered or "unknown command" in lowered


def _raise_if_too_old(text: str) -> None:
    if _looks_too_old(text):
        raise ClaudeError(TOO_OLD_MSG)


def _raise_for_error_envelope(envelope: dict) -> None:
    text = str(envelope.get("result") or "").strip()
    _raise_if_too_old(text)
    lowered = text.lower()
    if "limit" in lowered:
        raise ClaudeUsageLimitError(text)
    if "authenticate" in lowered or "log in" in lowered or "login" in lowered:
        raise ClaudeAuthError(text)
    raise ClaudeError(text or "Claude CLI reported an error")


def _extract_decisions(proc: subprocess.CompletedProcess) -> list | None:
    """Return the raw decision list, or None if the response is unusable.

    Raises ClaudeError (or a subclass) for error envelopes and for a failed process
    whose stdout carries no envelope at all.
    """
    envelope = _parse_json_text(proc.stdout or "")
    if not isinstance(envelope, dict):
        if proc.returncode != 0:
            _raise_if_too_old(f"{proc.stderr or ''} {proc.stdout or ''}")
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            raise ClaudeError(f"Claude CLI exited with code {proc.returncode}: {tail}")
        return None
    if envelope.get("is_error"):
        _raise_for_error_envelope(envelope)

    payload = envelope.get("structured_output")
    if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
        result = envelope.get("result")
        payload = _parse_json_text(result) if isinstance(result, str) else None
    if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
        return None
    return payload["decisions"]


def _valid_decisions(raw: list, count: int) -> dict[int, tuple[str, str]]:
    """Map n -> (verdict, reason), keeping the first of duplicates, dropping invalid entries."""
    found: dict[int, tuple[str, str]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        n, verdict = item.get("n"), item.get("verdict")
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= count:
            continue
        if verdict not in _VERDICTS or n in found:
            continue
        found[n] = (verdict, str(item.get("reason") or ""))
    return found


class ClaudeClassifier:
    def __init__(self, runner: Runner = default_runner, executable: str | None = None, model: str = "sonnet",
                 settings: Callable[[], ClaudeSettings] | None = None):
        """`settings`, when given, is read on every call, so a changed choice applies to the next batch."""
        self._runner = runner
        self._executable = executable
        self._settings = settings or (lambda: ClaudeSettings(model))

    def _exe(self) -> str | None:
        return self._executable or shutil.which("claude")

    def _run(self, args: list[str], stdin_text: str) -> subprocess.CompletedProcess:
        try:
            return self._runner(args, stdin_text)
        except subprocess.TimeoutExpired as e:
            raise ClaudeTemporaryError(TIMEOUT_MSG) from e
        except OSError as e:
            raise ClaudeError(f"Could not run the Claude CLI: {e}") from e

    def status(self) -> ClaudeStatus:
        exe = self._exe()
        if not exe:
            return ClaudeStatus(False, False, "Claude CLI not found. Install Claude Code and make sure `claude` is on your PATH.")
        try:
            proc = self._runner([exe, "auth", "status", "--json"], "", timeout=STATUS_TIMEOUT)
        except subprocess.TimeoutExpired:
            return ClaudeStatus(True, False, STATUS_SLOW_MSG)
        except OSError as e:
            return ClaudeStatus(True, False, f"Could not check whether Claude is logged in: {e}")
        info = _parse_json_text(proc.stdout or "")
        if isinstance(info, dict) and info.get("loggedIn") is True:
            return ClaudeStatus(True, True, "Claude is logged in.")
        if not isinstance(info, dict) and _looks_too_old(f"{proc.stderr or ''} {proc.stdout or ''}"):
            return ClaudeStatus(True, False, TOO_OLD_MSG)
        return ClaudeStatus(True, False, NOT_LOGGED_IN_MSG)

    def _ask(self, exe: str, description: str, songs: list[Song]) -> dict[int, tuple[str, str]]:
        """One CLI call for `songs`; returns decisions keyed by 1-based position (possibly empty)."""
        choice = self._settings()
        args = [
            exe, "-p", "--tools", "", "--no-session-persistence", "--output-format", "json",
            "--model", choice.model, "--system-prompt", SYSTEM_PROMPT, "--json-schema", SCHEMA_JSON,
            "--setting-sources", "project", "--strict-mcp-config",
        ]  # fmt: skip
        if choice.effort != "default":
            args += ["--effort", choice.effort]
        proc = self._run(args, _build_prompt(description, songs))
        raw = _extract_decisions(proc)
        return _valid_decisions(raw, len(songs)) if raw is not None else {}

    def classify_batch(self, description: str, songs: list[Song]) -> list[Decision]:
        """Exactly one Decision per song, in input order.

        Songs Claude skipped (or a wholly unusable reply) are retried once; any still
        missing become `unsure` (UNANSWERED_REASON). If not one song got a real answer,
        ClaudeTemporaryError(UNREADABLE_MSG) is raised instead, so the job stops for a Resume rather
        than filling the review with guesses. CLI error envelopes (usage limit, auth) raise
        immediately.
        """
        if not songs:
            return []
        exe = self._exe()
        if not exe:
            raise ClaudeError("Claude CLI is not installed (`claude` not found on PATH)")

        decided: dict[int, tuple[str, str]] = {}  # index into `songs` -> (verdict, reason)
        pending = list(range(len(songs)))
        for _attempt in range(2):
            answers = self._ask(exe, description, [songs[i] for i in pending])
            for n, answer in answers.items():
                decided[pending[n - 1]] = answer
            pending = [i for i in pending if i not in decided]
            if not pending:
                break
        if not decided:
            raise ClaudeTemporaryError(UNREADABLE_MSG)

        return [
            Decision(song.video_id, *decided[i]) if i in decided else Decision(song.video_id, "unsure", UNANSWERED_REASON)
            for i, song in enumerate(songs)
        ]

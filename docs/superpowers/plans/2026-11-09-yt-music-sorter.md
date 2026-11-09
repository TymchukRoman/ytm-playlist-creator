# YT Music Sorter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local browser app that creates and updates YouTube Music playlists from the user's liked songs, with Claude (via the Claude CLI) deciding which songs fit a plain-language description and the user approving every change.

**Architecture:** A Python package `ytsorter` runs a FastAPI server on `127.0.0.1`, serving a no-build vanilla-JS page. Long operations run as a single background job whose state is persisted to disk after every step (so it survives a Claude usage limit or app restart). The browser polls the job for progress and review data. YouTube Music access (`ytmusicapi`) and Claude (`claude -p` subprocess) sit behind small interfaces with fakes, so all logic is testable and a demo mode runs without accounts.

**Tech Stack:** Python ≥ 3.11, uv, FastAPI + uvicorn, ytmusicapi, platformdirs, pytest + httpx (TestClient). Frontend: plain HTML/CSS/JS, no build step.

**Spec:** `docs/superpowers/specs/2026-11-09-yt-music-sorter-design.md`

## Global Constraints

- Never remove songs from any playlist; never change likes. The only YouTube Music writes are `create_playlist` and `add_playlist_items`.
- Only write to playlist IDs present in the tool's own records (or the one just created).
- New playlists are created with privacy `PRIVATE`.
- Nothing is written to YouTube Music until the user approves the review screen (Create and Update alike).
- User decisions (made on the review screen) are final: later runs never override them.
- Claude judges from metadata (title, artists, album, duration) + own knowledge only: `claude -p` runs with `--tools ""`. Verdicts: `include` / `exclude` / `unsure`.
- All user data (sign-in, records, job state) lives in the per-user app-data folder from `platformdirs.user_data_dir("yt-music-sorter")`, overridable by env `YTSORTER_DATA_DIR`. Nothing user-specific is written inside the repo.
- Server binds to `127.0.0.1` only. Only one job runs at a time.
- UI language: English. Button names: **Create**, **Update**, **Update all**.
- Target: smooth with ≥ 5,000 liked songs.
- Every subprocess / file read-write uses `encoding="utf-8"` (titles are often Cyrillic, Japanese, Korean).

## Review Focus

1. **YouTube sign-in expires mid-job** → job stops with "YouTube Music sign-in expired — reconnect", keeps finished work, and can be resumed after reconnecting. (Test in Task 5.)
2. **Claude returns malformed or partial output for a batch** → retry the batch once; songs still missing become `unsure` with reason "Claude didn't answer for this song". (Test in Task 2.)
3. **Claude usage limit hit mid-classification** → job becomes `paused` with a plain message; finished batches are kept on disk; Resume after an app restart continues from the next batch. (Test in Task 4 and Task 5.)
4. **Liked list contains duplicates or items without a `videoId`** (unavailable / removed tracks) → skipped and de-duplicated before classification, never sent to Claude or YouTube. (Test in Task 3.)
5. **Titles with quotes, emoji, newlines, non-Latin scripts** → the prompt goes through stdin as UTF-8 and the JSON round-trips intact on Windows. (Test in Task 2.)

---

## File Structure

```
yt-music-sorter/
  pyproject.toml            # uv project, deps, pytest config, `ytsorter` script entry
  .gitignore
  start.bat / start.sh      # one-click start (Task 8)
  README.md                 # non-technical guide (Task 8)
  ytsorter/
    __init__.py
    __main__.py             # pick port, start uvicorn, open browser (Task 6)
    paths.py                # data_dir(), file locations (Task 1)
    models.py               # Song, Decision, SongEntry, PlaylistRecord (Task 1)
    store.py                # RecordStore, JobStore: atomic JSON files (Task 1)
    claude_cli.py           # ClaudeClassifier: status check + classify_batch (Task 2)
    ytm.py                  # YTMusicClient wrapper over ytmusicapi (Task 3)
    fakes.py                # FakeYTMusic, FakeClassifier for tests + demo mode (Tasks 2–3)
    classify.py             # resumable batched classification (Task 4)
    jobs.py                 # Job model + JobRunner: create/update/update-all/approve/cancel/resume (Task 5)
    app.py                  # FastAPI routes (Task 6)
    static/index.html, app.js, style.css, guide/   # UI (Task 7)
  tests/
    test_store.py, test_claude_cli.py, test_ytm.py, test_classify.py, test_jobs.py, test_app.py
```

---

### Task 1: Project scaffold, data paths, models, record store

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `ytsorter/__init__.py`, `ytsorter/paths.py`, `ytsorter/models.py`, `ytsorter/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces:
  - `paths.data_dir() -> Path` (env `YTSORTER_DATA_DIR` wins; else `platformdirs.user_data_dir("yt-music-sorter", appauthor=False)`; created if missing). `paths.youtube_auth_file() -> Path` = `data_dir()/"youtube_auth.json"`. `paths.playlists_dir() -> Path` = `data_dir()/"playlists"`. `paths.job_file() -> Path` = `data_dir()/"job.json"`.
  - `models.Song(video_id: str, title: str, artists: list[str], album: str | None, duration: str | None)` — frozen dataclass, `to_dict()` / `Song.from_dict(d)`.
  - `models.Verdict = Literal["include", "exclude", "unsure"]`; `models.Source = Literal["claude", "user"]`.
  - `models.Decision(video_id: str, verdict: Verdict, reason: str)`.
  - `models.SongEntry(title: str, artists: list[str], verdict: Verdict, reason: str, source: Source)`.
  - `models.PlaylistRecord(playlist_id: str, name: str, description: str, created_at: str, updated_at: str, included: dict[str, SongEntry], excluded: dict[str, SongEntry])` with `checked_ids() -> set[str]` (keys of included ∪ excluded), `to_dict()`, `from_dict()`. Timestamps ISO-8601 UTC strings.
  - `store.RecordStore(dir: Path)`: `list() -> list[PlaylistRecord]` (sorted by `created_at`), `get(playlist_id) -> PlaylistRecord | None`, `save(record) -> None`, `delete(playlist_id) -> None` (removes only the local record file).
  - `store.write_json_atomic(path: Path, data: dict) -> None` / `store.read_json(path) -> dict | None` — write to `path.with_suffix(".tmp")` then `os.replace`.

- [ ] **Step 1:** `uv init --package --name ytsorter` style `pyproject.toml` by hand: `requires-python = ">=3.11"`, deps `fastapi`, `uvicorn`, `ytmusicapi`, `platformdirs`; dev group `pytest`, `httpx`; `[project.scripts] ytsorter = "ytsorter.__main__:main"`; `[tool.pytest.ini_options] testpaths = ["tests"]`. `.gitignore`: `.venv/`, `__pycache__/`, `.superpowers/`, `*.tmp`. Run `uv sync` → succeeds.
- [ ] **Step 2: Write failing tests** in `tests/test_store.py` (use `monkeypatch.setenv("YTSORTER_DATA_DIR", str(tmp_path))`):
  - `test_data_dir_honors_env` → `paths.data_dir() == tmp_path`.
  - `test_record_roundtrip_unicode` → save a record whose song title is `'Гурт "Океан Ельзи" — Обійми 🎵'`, `get()` returns an equal record; file is valid UTF-8 JSON.
  - `test_checked_ids_union` → included `{"a"}`, excluded `{"b"}` → `{"a", "b"}`.
  - `test_list_sorted_and_delete` → two records, `list()` ordered by `created_at`; `delete()` removes one.
  - `test_atomic_write_leaves_no_tmp` → after `save`, no `*.tmp` in dir.
- [ ] **Step 3:** Run `uv run pytest tests/test_store.py -v` → FAIL (import errors).
- [ ] **Step 4:** Implement `paths.py`, `models.py`, `store.py` to the interfaces above.
- [ ] **Step 5:** Run `uv run pytest tests/test_store.py -v` → PASS.
- [ ] **Step 6:** Commit: `git add -A && git commit -m "feat: project scaffold, models, record store"`.

---

### Task 2: Claude CLI classifier

**Files:**
- Create: `ytsorter/claude_cli.py`, `ytsorter/fakes.py` (FakeClassifier part)
- Test: `tests/test_claude_cli.py`

**Interfaces:**
- Consumes: `models.Song`, `models.Decision`.
- Produces:
  - `claude_cli.ClaudeStatus(installed: bool, logged_in: bool, message: str)`.
  - `class ClaudeError(Exception)`, `class ClaudeUsageLimitError(ClaudeError)`, `class ClaudeAuthError(ClaudeError)`.
  - `Runner = Callable[[list[str], str], subprocess.CompletedProcess]` — `(args, stdin_text) -> CompletedProcess`; default runs `subprocess.run(args, input=stdin_text, capture_output=True, encoding="utf-8", timeout=300)`.
  - `class ClaudeClassifier(runner: Runner = default_runner, executable: str | None = None, model: str = "sonnet")`:
    - `status() -> ClaudeStatus` — `executable or shutil.which("claude")`; missing → `installed=False`. Else run `[exe, "auth", "status", "--json"]`, parse `loggedIn`.
    - `classify_batch(description: str, songs: list[Song]) -> list[Decision]` — exactly one Decision per input song, in input order.
  - `fakes.FakeClassifier(rules: Callable[[str, Song], tuple[Verdict, str]] | None = None)` implementing `status()` (always ok) and `classify_batch` (default rule: include if any word of the description appears in title/artists, else exclude), plus `fail_after: int | None` to raise `ClaudeUsageLimitError` after N batches (for tests of pause/resume).

Command line (exact):
```
[exe, "-p", "--tools", "", "--no-session-persistence", "--output-format", "json",
 "--model", model, "--system-prompt", SYSTEM_PROMPT, "--json-schema", SCHEMA_JSON]
```
with the user prompt on stdin. Songs are numbered `1..n` in the prompt (short ids, mapped back to video IDs); the prompt contains the description and a JSON array of `{"n", "title", "artists", "album", "duration"}`.

`SCHEMA_JSON`: `{"type":"object","properties":{"decisions":{"type":"array","items":{"type":"object","properties":{"n":{"type":"integer"},"verdict":{"enum":["include","exclude","unsure"]},"reason":{"type":"string"}},"required":["n","verdict","reason"]}}},"required":["decisions"]}`.

`SYSTEM_PROMPT` must say: you sort a user's liked songs into a playlist they described; judge only from given metadata and your own knowledge; answer `unsure` rather than guess; reason ≤ 15 words; return one decision per song number.

Parsing the CLI's JSON envelope: if `is_error` is true, classify `result` text — contains "limit" (case-insensitive, e.g. "usage limit", "rate limit") → `ClaudeUsageLimitError`; contains "authenticate" or "log in"/"login" → `ClaudeAuthError`; else `ClaudeError`. Otherwise take `structured_output` if present, else `json.loads(result)` (tolerate a ```json fence). Non-zero exit with unparseable stdout → `ClaudeError` with stderr tail.

- [ ] **Step 1: Write failing tests** using a fake runner that records `(args, stdin)` and returns a canned `CompletedProcess`:
  - `test_command_disables_tools_and_sends_prompt_on_stdin` → args contain `"--tools", ""` and `"--json-schema"`; stdin contains the description and every song title.
  - `test_maps_numbers_back_to_video_ids` → envelope `{"is_error": false, "structured_output": {"decisions": [{"n":2,"verdict":"exclude","reason":"x"},{"n":1,"verdict":"include","reason":"y"}]}}` → `[Decision("vid1","include","y"), Decision("vid2","exclude","x")]`.
  - `test_falls_back_to_result_text_json` → no `structured_output`, `result` = fenced JSON → parsed.
  - `test_retries_once_then_marks_missing_unsure` → first response omits song 2, second response (retry with only song 2) is malformed → song 2 is `Decision("vid2","unsure","Claude didn't answer for this song")`; runner called exactly twice.
  - `test_usage_limit_error` → envelope `{"is_error": true, "result": "Claude AI usage limit reached|1794240000"}` → raises `ClaudeUsageLimitError`.
  - `test_auth_error` → `result: "Failed to authenticate: OAuth session expired"` → `ClaudeAuthError`.
  - `test_unicode_roundtrip` → song title `'Kick Back "チェンソーマン" 🎸\nline2'` appears unchanged in stdin and the returned reason with Cyrillic text is preserved.
  - `test_status_not_installed` → `executable=None` and `shutil.which` monkeypatched to `None` → `ClaudeStatus(False, False, ...)`.
  - `test_status_logged_in` → runner returns `{"loggedIn": true}` → `logged_in=True`.
- [ ] **Step 2:** Run `uv run pytest tests/test_claude_cli.py -v` → FAIL.
- [ ] **Step 3:** Implement `claude_cli.py` and `FakeClassifier` in `fakes.py`.
- [ ] **Step 4:** Run tests → PASS.
- [ ] **Step 5: Smoke check against the real CLI** (needs a logged-in `claude`): `uv run python -c "from ytsorter.claude_cli import *; from ytsorter.models import Song; print(ClaudeClassifier().classify_batch('anime openings', [Song('a','Gurenge',['LiSA'],None,None)]))"` → one `include` decision. If the envelope uses a different key than `structured_output`, fix the parser and add a test for it. If `claude` isn't logged in, report that and skip.
- [ ] **Step 6:** Commit `feat: Claude CLI classifier`.

---

### Task 3: YouTube Music client wrapper

**Files:**
- Create: `ytsorter/ytm.py`, extend `ytsorter/fakes.py` (FakeYTMusic)
- Test: `tests/test_ytm.py`

**Interfaces:**
- Consumes: `models.Song`, `paths.youtube_auth_file()`.
- Produces:
  - `class YouTubeAuthError(Exception)` (not connected / sign-in expired), `class YouTubeError(Exception)`, `class PlaylistMissingError(YouTubeError)`.
  - `class YTMusicClient(auth_file: Path, factory: Callable[[str], Any] = ytmusicapi.YTMusic)`:
    - `is_connected() -> bool` — auth file exists.
    - `connect(headers_raw: str) -> None` — `ytmusicapi.setup(filepath=str(auth_file), headers_raw=headers_raw)`, then verifies with `get_liked_songs(limit=1)`; on failure deletes the file and raises `YouTubeAuthError("Those sign-in details didn't work…")`.
    - `disconnect() -> None` — deletes auth file.
    - `get_liked_songs() -> list[Song]` — `get_liked_songs(limit=None)["tracks"]`; skips tracks with no `videoId`; de-duplicates by `videoId` keeping first; `artists` = names; `album` = `album["name"]` or None.
    - `create_playlist(name: str, description: str) -> str` — `create_playlist(name, description, privacy_status="PRIVATE")`; non-string result → `YouTubeError`.
    - `add_songs(playlist_id: str, video_ids: list[str], on_progress: Callable[[int, int], None]) -> None` — chunks of 50 via `add_playlist_items(playlist_id, chunk, duplicates=False)`; calls `on_progress(done, total)` after each chunk.
    - `playlist_exists(playlist_id: str) -> bool` — `get_playlist(playlist_id, limit=1)`; exception whose message indicates not found → False.
    - Any ytmusicapi exception mentioning 401/403/"sign in"/"authorization" → `YouTubeAuthError`; others → `YouTubeError`.
  - `fakes.FakeYTMusic(liked: list[Song])` with the same public methods, recording `created: dict[str, dict]` and `added: dict[str, list[str]]`, plus `deleted_playlists: set[str]` and `expire_auth()` for tests. No remove methods exist on either class.

- [ ] **Step 1: Write failing tests** with a fake ytmusicapi object passed via `factory`:
  - `test_liked_songs_skips_missing_ids_and_dedupes` → tracks `[{videoId:"a"}, {videoId:None}, {videoId:"a"}, {videoId:"b"}]` → ids `["a","b"]`.
  - `test_create_playlist_is_private` → fake records `privacy_status == "PRIVATE"`.
  - `test_add_songs_chunks_of_50_and_reports_progress` → 120 ids → 3 calls of sizes 50/50/20, progress `(50,120),(100,120),(120,120)`, every call `duplicates=False`.
  - `test_auth_failure_maps_to_youtube_auth_error` → fake raises `Exception("Server returned HTTP 401")` → `YouTubeAuthError`.
  - `test_connect_bad_headers_removes_file` → setup ok, verify raises → file gone, `YouTubeAuthError`.
  - `test_playlist_exists_false_when_not_found`.
  - `test_client_has_no_remove_methods` → `not any(n.startswith(("remove","delete","unlike","rate")) for n in dir(YTMusicClient))` (disconnect is allowed — it touches only the local file).
- [ ] **Step 2:** Run `uv run pytest tests/test_ytm.py -v` → FAIL.
- [ ] **Step 3:** Implement `ytm.py` and `FakeYTMusic`.
- [ ] **Step 4:** Run tests → PASS.
- [ ] **Step 5:** Commit `feat: YouTube Music client wrapper`.

---

### Task 4: Resumable batched classification

**Files:**
- Create: `ytsorter/classify.py`
- Test: `tests/test_classify.py`

**Interfaces:**
- Consumes: `ClaudeClassifier.classify_batch`, `Song`, `Decision`, `ClaudeError` family.
- Produces:
  - `BATCH_SIZE = 100`.
  - `classify_pending(description: str, songs: list[Song], decisions: dict[str, Decision], classifier, on_batch_done: Callable[[int, int], None]) -> None` — classifies songs whose `video_id` is not in `decisions`, in batches of `BATCH_SIZE`, in input order; after each batch, merges results into `decisions` and calls `on_batch_done(batches_done, batches_total)` (the caller persists the job there). Exceptions from the classifier propagate after earlier batches have been merged and reported.

- [ ] **Step 1: Write failing tests** with `FakeClassifier`:
  - `test_batches_of_100` → 250 songs → classifier called with sizes 100/100/50; `on_batch_done` sees `(1,3),(2,3),(3,3)`.
  - `test_skips_already_decided` → 150 songs, 100 pre-decided → one call of 50.
  - `test_limit_mid_run_keeps_finished_batches` → `fail_after=1`, 250 songs → raises `ClaudeUsageLimitError`; `decisions` has exactly 100 entries.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: resumable batched classification`.

---

### Task 5: Jobs — Create, Update, Update all, review, approve, resume

**Files:**
- Create: `ytsorter/jobs.py`; extend `ytsorter/store.py` with `JobStore(path: Path)`: `load() -> Job | None`, `save(job)`, `clear()`.
- Test: `tests/test_jobs.py`

**Interfaces:**
- Consumes: `RecordStore`, `JobStore`, `YTMusicClient` (or Fake), classifier, `classify_pending`, models.
- Produces:
  - `JobStatus = Literal["running", "review", "applying", "paused", "failed", "done"]`.
  - `Progress(stage: str, message: str, current: int | None, total: int | None)` — `current=None` means indeterminate bar.
  - `Target(playlist_id: str | None, name: str, description: str, songs: list[Song], decisions: dict[str, Decision], approved: bool = False, skipped: bool = False)` — `playlist_id=None` for Create.
  - `Job(id: str, kind: Literal["create", "update"], status: JobStatus, targets: list[Target], review_index: int, progress: Progress, error: str | None, notice: str | None)` with `to_dict()/from_dict()`.
  - `class JobBusyError(Exception)`.
  - `class JobRunner(records: RecordStore, jobs: JobStore, ytm, classifier, run_in_background: Callable[[Callable[[], None]], None] = <thread starter>)`:
    - `current() -> Job | None`.
    - `start_create(name: str, description: str) -> Job`
    - `start_update(playlist_ids: list[str]) -> Job` (Update = one id; Update all = all record ids)
    - `approve(video_ids: list[str]) -> None` — applies the current review target (see below), then moves to the next target needing review, or to `done`.
    - `skip() -> None` — Update all only: leave this playlist unchanged, move on.
    - `cancel() -> None` — discards the job (`jobs.clear()`); no YouTube writes, records unchanged.
    - `resume() -> None` — for `paused`/`failed` jobs: continue from saved state.
    - `forget_playlist(playlist_id: str) -> None` — `records.delete` (used when the playlist was deleted on YouTube Music).
  - Tests pass `run_in_background=lambda fn: fn()` so jobs run synchronously.

Behavior (what the tests pin):
- Starting while a job is `running`/`applying` → `JobBusyError`. A job in `review`/`paused`/`failed` also blocks new jobs until cancelled or finished (the UI offers Cancel).
- Background work: progress `("fetch", "Fetching your liked songs…", None, None)`; then for Update, each target's `songs` = liked songs not in `record.checked_ids()`; targets with no new songs get `skipped=True` and the notice "No new liked songs since last update" (if all are skipped → status `done` with that notice). Before classifying an Update target, `ytm.playlist_exists`; if False → status `failed`, error `"The playlist “<name>” no longer exists on YouTube Music."` (UI offers Forget / Cancel).
- Classification progress: `("classify", "Asking Claude which songs fit… batch i of n", i, n)`; for Update all prefix `"<name>: "`. The job is saved after every batch.
- `ClaudeUsageLimitError` → status `paused`, error "Claude usage limit reached. Your progress is saved — press Resume later."; `ClaudeAuthError` → `paused`, "Claude isn't logged in. Run `claude` once to sign in, then press Resume."; `YouTubeAuthError` → `paused`, "YouTube Music sign-in expired — reconnect, then press Resume."; other errors → `failed` with the message.
- After classification → status `review`, `review_index` = first target not skipped.
- `approve(video_ids)` for target T: allowed ids = T's songs; unknown ids ignored. Status `applying`. Create: `create_playlist(name, description)` then `add_songs`. Update: `add_songs(record.playlist_id, ...)` (never any other id). Progress `("add", "Adding songs… d of t", d, t)`. Then write record: approved ids → `included`; all other T songs → `excluded`. `source="user"` when the user's choice differs from Claude's default (default = checked iff verdict `include`), else `"claude"`. Entry keeps Claude's verdict and reason. Update `updated_at`. If the playlist was created but adding fails, the record is saved first with the playlist id (so a retry targets the same playlist, never a second one).
- When nothing approved on Create (empty `video_ids`) → API rejects (Task 6); `approve([])` on Update is allowed and records all as excluded.
- `JobRunner.__init__` loads any saved job; a job saved as `running`/`applying` (app was closed) becomes `paused` with "The app was closed during this step — press Resume."

- [ ] **Step 1: Write failing tests** (FakeYTMusic + FakeClassifier + tmp data dir):
  - `test_create_flow_review_then_approve` → after `start_create`, status `review`, nothing in `fake.created`; `approve([...two ids])` → one private playlist with exactly those ids; record saved with 2 included, rest excluded.
  - `test_cancel_writes_nothing` → `start_create` → `cancel()` → `fake.created == {}`, `records.list() == []`, `current() is None`.
  - `test_unchecking_include_marks_user_source` and `test_checking_unsure_marks_user_source`.
  - `test_update_only_new_songs` → record has a,b; likes a,b,c,d → classifier sees only c,d; after approve, `fake.added[pid]` gains only approved of c,d; record now contains a,b,c,d.
  - `test_user_decisions_never_reclassified` → a song the user excluded is not sent to Claude on Update.
  - `test_update_no_new_songs_notice`.
  - `test_update_all_reviews_each_playlist_in_turn` → two records; first `approve`, second `skip` → only first playlist gets songs; second record unchanged.
  - `test_update_missing_playlist_fails_clearly` → `fake.deleted_playlists = {pid}` → status `failed`, error names the playlist; `fake.created == {}` (not recreated).
  - `test_usage_limit_pauses_and_resume_after_restart` → `fail_after=1` with 250 songs → `paused`; new `JobRunner` on same dirs (classifier no longer failing) → `current().status == "paused"`; `resume()` → `review`; classifier received only the remaining 150.
  - `test_youtube_auth_expired_pauses` → `fake.expire_auth()` before approve → `paused` with the reconnect message; after fix, `resume()` completes the add.
  - `test_busy_rejects_second_job`.
  - `test_add_failure_after_create_does_not_duplicate_playlist` → add fails once, resume → still exactly one created playlist.
  - `test_only_writes_to_record_playlist_ids` → after a full Create + Update run, every key of `fake.added` is a record's `playlist_id`.
- [ ] **Step 2:** Run `uv run pytest tests/test_jobs.py -v` → FAIL.
- [ ] **Step 3:** Implement `JobStore` and `jobs.py`.
- [ ] **Step 4:** Run the whole suite `uv run pytest -v` → PASS.
- [ ] **Step 5:** Commit `feat: job runner for create/update with review and resume`.

---

### Task 6: Web API and launcher

**Files:**
- Create: `ytsorter/app.py`, `ytsorter/__main__.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: everything above.
- Produces `create_app(runner: JobRunner, ytm, classifier, records: RecordStore) -> FastAPI` and `build_default_app() -> FastAPI` (real clients; if env `YTSORTER_DEMO=1`, FakeYTMusic with ~300 built-in sample songs across anime, K-pop, rock and Ukrainian, and FakeClassifier). Static files served from `ytsorter/static` at `/`.

Routes (JSON):

| Method & path | Body | Returns / effect |
|---|---|---|
| `GET /api/status` | — | `{"claude": {"installed","logged_in","message"}, "youtube": {"connected": bool}, "demo": bool}` |
| `POST /api/youtube/connect` | `{"headers": str}` | 200 `{"connected": true}` or 400 `{"error": msg}` |
| `POST /api/youtube/disconnect` | — | `{"connected": false}` |
| `GET /api/playlists` | — | `[{"playlist_id","name","description","song_count","updated_at"}]` |
| `POST /api/playlists` | `{"name","description"}` | starts Create; 400 if name or description blank; 409 if busy |
| `POST /api/playlists/{id}/update` | — | starts Update; 404 unknown id; 409 busy |
| `POST /api/update-all` | — | starts Update all; 400 if no playlists; 409 busy |
| `DELETE /api/playlists/{id}` | — | `forget_playlist` (local record only) |
| `GET /api/job` | — | `null` or job JSON incl. `progress`, `error`, `notice`, and when `review`: `{"review": {"target_name", "index", "count", "include": [...], "unsure": [...], "exclude": [...]}}`, each song `{video_id,title,artists,album,reason}` |
| `POST /api/job/approve` | `{"video_ids": [str]}` | 400 if Create and empty |
| `POST /api/job/skip` · `/cancel` · `/resume` | — | as JobRunner |

`__main__.main()`: find a free port on `127.0.0.1` (try 8765, else OS-assigned), start uvicorn with `build_default_app()` bound to `127.0.0.1`, open `http://127.0.0.1:<port>/` with `webbrowser.open` after startup, print the URL and "Close this window to stop the app."

- [ ] **Step 1: Write failing tests** with `TestClient` and fakes (synchronous runner):
  - `test_status_reports_both_services`, `test_create_then_review_payload_has_three_sections`, `test_approve_empty_create_rejected`, `test_busy_returns_409`, `test_update_unknown_playlist_404`, `test_connect_bad_headers_400_message`, `test_server_binds_localhost_only` (assert the uvicorn config host in `__main__` is `"127.0.0.1"` via a factored `server_config(port) -> uvicorn.Config`).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run full suite → PASS.
- [ ] **Step 5:** Manual: `YTSORTER_DEMO=1 uv run ytsorter` → browser opens, `GET /api/status` shows `demo: true`.
- [ ] **Step 6:** Commit `feat: web API and launcher`.

---

### Task 7: Browser UI

**Files:**
- Create: `ytsorter/static/index.html`, `ytsorter/static/app.js`, `ytsorter/static/style.css`, `ytsorter/static/guide/` (images for the sign-in guide)

**Interfaces:**
- Consumes: Task 6 routes only.

Screens (one page, JS switches views; polls `GET /api/job` every 1 s while a job exists, `GET /api/status` on load):
1. **Setup** (shown when Claude is missing/logged out or YouTube not connected): two checklist rows with green ✓ / red ✗.
   - Claude: plain steps — install Claude Code (link to official install page), open a terminal, run `claude` once and log in, then press "Check again".
   - YouTube Music: numbered guide for the browser-headers method for Chrome/Edge and Firefox (tabs): open music.youtube.com signed in → open developer tools (F12) → Network tab → filter `browse` → click a request → copy request headers (Chrome: "Copy" → "Copy request headers"; Firefox: "Raw" toggle → copy) → paste into the big text box → **Connect**. Each step has an image slot in `static/guide/`. A note: "These details let this app act as you on YouTube Music. They stay on this computer only."
2. **Main**: Create form (description textarea with placeholder `e.g. Anime openings only — no endings or soundtracks`, name input, **Create**); "Your playlists" list with **Update** per row, **Update all** at top, and a small "Forget" action on rows whose playlist is missing.
3. **Progress**: stage message + progress bar (indeterminate stripe when `current` is null), **Cancel** button.
4. **Review**: header "Review “<name>”" (and "Playlist i of n" for Update all); three sections — **Included** (all checked), **Unsure** (unchecked, highlighted, reason shown), **Not included** (collapsed `<details>`, unchecked, reason shown); "Select all / none" per section; live count on the approve button: Create → "Create playlist with N songs"; Update → "Add N songs"; plus **Cancel** (and **Skip this playlist** for Update all). Approve disabled at N = 0 for Create.
5. **Paused / Failed**: the plain-language error, **Resume** and **Cancel** (and **Reconnect YouTube Music** when the error is about sign-in; **Forget this playlist** when missing). **Done**: notice + back to main.

Accessibility/usability: real `<button>`/`<label>`/`<input type=checkbox>` elements, keyboard usable, text ≥ 16px, works at 360 px width, light and dark (`prefers-color-scheme`). No external scripts or fonts (works offline except the YouTube/Claude calls).

- [ ] **Step 1:** Build the pages against demo mode: `YTSORTER_DEMO=1 uv run ytsorter`.
- [ ] **Step 2: Verify in the browser** (built-in browser pane): walk Create → review (uncheck one, check one unsure) → approve → playlist row appears with the right count; Update with no new songs shows the notice; Update all walks two reviews with Skip; Cancel during review returns to main with nothing created. Check at 360 px wide and in dark mode. Console has no errors.
- [ ] **Step 3:** Commit `feat: browser UI`.

---

### Task 8: One-click start, README, sign-in guide images

**Files:**
- Create: `start.bat`, `start.sh`, `README.md`; fill `ytsorter/static/guide/*.png`

**Interfaces:** none (packaging).

- `start.bat`: `cd /d "%~dp0"`; if `uv` not found, install it with `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` and add `%USERPROFILE%\.local\bin` to PATH for the session; then `uv run --frozen ytsorter`; on error `pause` so the window doesn't vanish.
- `start.sh` (chmod +x): `cd "$(dirname "$0")"`; if no `uv`, `curl -LsSf https://astral.sh/uv/install.sh | sh` and `export PATH="$HOME/.local/bin:$PATH"`; `exec uv run --frozen ytsorter`. Commit `uv.lock`.
- `README.md` (non-technical first): what it does (one paragraph + screenshot of review screen); requirements (Claude Code installed and logged in; a YouTube Music account); Download (green "Code" button → "Download ZIP" → unzip; or `git clone`); Start (double-click `start.bat` on Windows; macOS/Linux: `./start.sh`); first-time setup walkthrough (mirrors the in-app guide); making your first playlist; updating; "Your data" section (where files live per OS, how to disconnect); safety rules (never removes, never touches other playlists, private playlists); troubleshooting (sign-in expired, Claude usage limit, port in use, Windows SmartScreen on `start.bat`); updating the app (download again — data is kept). Developer section: `uv run pytest`, `YTSORTER_DEMO=1`.
- Guide images: capture from demo mode for app screens; the YouTube developer-tools steps are captured by the owner during Task 9 (their account) and saved to `static/guide/`. Until then, the guide shows text steps only (no broken images).

- [ ] **Step 1:** On Windows, run `start.bat` from a fresh clone of the repo in a temp folder → app opens in demo-less mode at the Setup screen.
- [ ] **Step 2:** Run `bash start.sh` (Git Bash) → same result.
- [ ] **Step 3:** Commit `feat: start scripts and README`.

---

### Task 9: End-to-end check with the real accounts (with the user)

- [ ] **Step 1:** User starts the app with `start.bat`, follows the in-app guide to connect YouTube Music; capture the dev-tools screenshots into `static/guide/` along the way.
- [ ] **Step 2:** Create a small playlist (e.g. "anime openings") → check review sections make sense → approve → confirm in YouTube Music the playlist is private and contains exactly the approved songs.
- [ ] **Step 3:** Like 2–3 new songs on YouTube Music → **Update** → only the new songs appear in review → approve → confirm they were added and nothing was removed.
- [ ] **Step 4:** Record any surprises (envelope format, ytmusicapi errors, timings for the full library) and fix them with tests.
- [ ] **Step 5:** Commit `chore: guide screenshots and fixes from real-account test`.

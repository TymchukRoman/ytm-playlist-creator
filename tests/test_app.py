import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from ytsorter import __main__ as launcher
from ytsorter import demo_data
from ytsorter.app import build_default_app, create_app
from ytsorter.claude_cli import ClaudeStatus
from ytsorter.fakes import FakeClassifier, FakeYTMusic
from ytsorter.jobs import JobRunner
from ytsorter.models import PlaylistRecord, Song, SongEntry
from ytsorter.store import JobStore, RecordStore
from ytsorter.ytm import YouTubeAuthError


BASE = "http://127.0.0.1:8765"


def song(vid: str) -> Song:
    return Song(vid, f"Title {vid} «ё»", ["Артист"], "Album", "3:21")


def make_songs(n: int) -> list[Song]:
    return [song(f"v{i}") for i in range(n)]


class CountingClassifier(FakeClassifier):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        return ClaudeStatus(True, self.status_calls > 1, f"check {self.status_calls}")


class RejectingYTM(FakeYTMusic):
    def connect(self, headers_raw: str) -> None:
        raise YouTubeAuthError("Those sign-in details didn't work. Copy the headers again.")


def verdicts(mapping):
    return lambda d, s: (mapping.get(s.video_id, "exclude"), f"why {s.video_id}")


def seed_playlist(fake, records, pid, name, included=()):
    fake.created[pid] = {"name": name, "description": name.lower(), "privacy_status": "PRIVATE"}
    fake.added[pid] = list(included)
    rec = PlaylistRecord(pid, name, name.lower(), "2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00",
                         included={v: SongEntry(f"Title {v}", ["A"], "include", "earlier", "claude")
                                   for v in included})
    records.save(rec)


class Env:
    def __init__(self, tmp_path, liked=None, classifier=None, ytm=None, clock=None):
        self.records = RecordStore(tmp_path / "playlists")
        self.jobs = JobStore(tmp_path / "job.json")
        self.ytm = ytm or FakeYTMusic(liked if liked is not None else make_songs(6))
        self.classifier = classifier or FakeClassifier(rules=verdicts({"v0": "include", "v1": "include",
                                                                       "v2": "unsure"}))
        self.runner = JobRunner(self.records, self.jobs, self.ytm, self.classifier,
                                run_in_background=lambda fn: fn())
        kwargs = {"clock": clock} if clock else {}
        self.app = create_app(self.runner, self.ytm, self.classifier, self.records, **kwargs)
        self.client = TestClient(self.app, base_url=BASE)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def create_playlist(env, name="Openings", description="anime openings"):
    return env.client.post("/api/playlists", json={"name": name, "description": description})


# --- status ---------------------------------------------------------------------------------

def test_status_reports_both_services(env):
    r = env.client.get("/api/status")
    assert r.status_code == 200
    assert r.json() == {
        "claude": {"installed": True, "logged_in": True, "message": "ok"},
        "youtube": {"connected": True},
        "demo": False,
    }


def test_status_is_cached_and_refresh_forces_a_recheck(tmp_path):
    now = [100.0]
    classifier = CountingClassifier()
    env = Env(tmp_path, classifier=classifier, clock=lambda: now[0])
    assert env.client.get("/api/status").json()["claude"]["message"] == "check 1"
    now[0] += 5
    assert env.client.get("/api/status").json()["claude"]["message"] == "check 1"
    assert classifier.status_calls == 1
    r = env.client.get("/api/status?refresh=1")
    assert r.json()["claude"] == {"installed": True, "logged_in": True, "message": "check 2"}
    now[0] += 11
    assert env.client.get("/api/status").json()["claude"]["message"] == "check 3"


def test_status_youtube_connected_is_live_not_cached(tmp_path):
    env = Env(tmp_path, ytm=FakeYTMusic([], connected=False))
    assert env.client.get("/api/status").json()["youtube"] == {"connected": False}
    env.ytm.connect("x")
    assert env.client.get("/api/status").json()["youtube"] == {"connected": True}


# --- YouTube connection -----------------------------------------------------------------------

def test_connect_ok(tmp_path):
    env = Env(tmp_path, ytm=FakeYTMusic([], connected=False))
    r = env.client.post("/api/youtube/connect", json={"headers": "cookie: abc"})
    assert r.status_code == 200 and r.json() == {"connected": True}
    assert env.ytm.is_connected()


def test_connect_bad_headers_400_message(tmp_path):
    env = Env(tmp_path, ytm=RejectingYTM([], connected=False))
    r = env.client.post("/api/youtube/connect", json={"headers": "garbage"})
    assert r.status_code == 400
    assert r.json() == {"error": "Those sign-in details didn't work. Copy the headers again."}


def test_connect_blank_headers_400(env):
    r = env.client.post("/api/youtube/connect", json={"headers": "   "})
    assert r.status_code == 400
    assert "error" in r.json()


def test_disconnect(env):
    r = env.client.post("/api/youtube/disconnect")
    assert r.json() == {"connected": False}
    assert not env.ytm.is_connected()


# --- playlists ----------------------------------------------------------------------------------

def test_playlists_list(env):
    seed_playlist(env.ytm, env.records, "PL1", "Rock", included=["v0", "v1"])
    assert env.client.get("/api/playlists").json() == [
        {"playlist_id": "PL1", "name": "Rock", "description": "rock", "song_count": 2,
         "updated_at": "2026-02-01T00:00:00+00:00"}
    ]


def test_playlists_empty(env):
    assert env.client.get("/api/playlists").json() == []


def test_forget_playlist_removes_only_the_record(env):
    seed_playlist(env.ytm, env.records, "PL1", "Rock", included=["v0"])
    r = env.client.delete("/api/playlists/PL1")
    assert r.status_code == 200
    assert env.records.get("PL1") is None
    assert "PL1" in env.ytm.created  # YouTube untouched
    assert env.client.delete("/api/playlists/PL1").status_code == 404


# --- create / review ----------------------------------------------------------------------------

def test_job_is_null_without_a_job(env):
    r = env.client.get("/api/job")
    assert r.status_code == 200 and r.json() is None


def test_create_then_review_payload_has_three_sections(env):
    r = create_playlist(env)
    assert r.status_code == 200, r.text
    job = env.client.get("/api/job").json()
    assert job["kind"] == "create"
    assert job["status"] == "review"
    assert job["error"] is None and job["notice"] is None and job["missing_playlist_id"] is None
    assert job["progress"] == {"stage": "review", "message": "Ready for your review.",
                               "current": None, "total": None}
    review = job["review"]
    assert review["target_name"] == "Openings"
    assert (review["index"], review["count"]) == (1, 1)
    ids = lambda key: [s["video_id"] for s in review[key]]  # noqa: E731
    assert ids("include") == ["v0", "v1"]
    assert ids("unsure") == ["v2"]
    assert ids("exclude") == ["v3", "v4", "v5"]
    assert review["include"][0] == {"video_id": "v0", "title": "Title v0 «ё»", "artists": ["Артист"],
                                    "album": "Album", "reason": "why v0"}
    assert env.ytm.created == {}  # nothing written before approval


def test_job_review_is_null_when_not_reviewing(env):
    env.ytm.fail_next_add = 0
    env.runner.cancel()
    assert env.client.get("/api/job").json() is None
    r = env.client.post("/api/playlists", json={"name": "A", "description": "b"})
    assert r.status_code == 200
    env.client.post("/api/job/approve", json={"video_ids": ["v0"]})
    job = env.client.get("/api/job").json()
    assert job["status"] == "done" and job["review"] is None
    assert job["progress"]["stage"] == "done"


@pytest.mark.parametrize("body", [
    {"name": "  ", "description": "x"},
    {"name": "x", "description": ""},
    {"name": "x"},
    {},
])
def test_create_blank_or_missing_fields_400(env, body):
    r = env.client.post("/api/playlists", json=body)
    assert r.status_code == 400
    assert isinstance(r.json()["error"], str) and r.json()["error"]
    assert env.client.get("/api/job").json() is None


def test_create_not_json_400(env):
    r = env.client.post("/api/playlists", content="not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert list(r.json()) == ["error"]


def test_approve_creates_playlist_with_only_chosen_songs(env):
    create_playlist(env)
    r = env.client.post("/api/job/approve", json={"video_ids": ["v0", "v2"]})
    assert r.status_code == 200
    (pid,) = env.ytm.created
    assert env.ytm.added[pid] == ["v0", "v2"]
    assert env.ytm.created[pid]["privacy_status"] == "PRIVATE"
    assert env.client.get("/api/job").json()["status"] == "done"
    assert env.client.get("/api/playlists").json()[0]["song_count"] == 2


def test_approve_empty_create_rejected(env):
    create_playlist(env)
    r = env.client.post("/api/job/approve", json={"video_ids": []})
    assert r.status_code == 400
    assert r.json()["error"]
    assert env.ytm.created == {}
    assert env.client.get("/api/job").json()["status"] == "review"  # still waiting


def test_approve_without_job_409(env):
    r = env.client.post("/api/job/approve", json={"video_ids": ["v0"]})
    assert r.status_code == 409 and r.json()["error"]


def test_approve_bad_body_400(env):
    create_playlist(env)
    r = env.client.post("/api/job/approve", json={"video_ids": "v0"})
    assert r.status_code == 400


def test_busy_returns_409(env):
    create_playlist(env)  # now waiting for review
    r = create_playlist(env, name="Second")
    assert r.status_code == 409
    assert r.json() == {"error": "Finish or cancel the current job first."}
    seed_playlist(env.ytm, env.records, "PL1", "Rock")
    assert env.client.post("/api/playlists/PL1/update").status_code == 409
    assert env.client.post("/api/update-all").status_code == 409


def test_cancel_clears_the_job(env):
    create_playlist(env)
    assert env.client.post("/api/job/cancel").status_code == 200
    assert env.client.get("/api/job").json() is None
    assert env.ytm.created == {}


def test_skip_on_create_409(env):
    create_playlist(env)
    assert env.client.post("/api/job/skip").status_code == 409


def test_resume_after_usage_limit(tmp_path):
    env = Env(tmp_path, liked=make_songs(250), classifier=FakeClassifier(fail_after=1))
    create_playlist(env, description="v1")
    job = env.client.get("/api/job").json()
    assert job["status"] == "paused"
    assert "usage limit" in job["error"]
    assert job["progress"]["stage"] == "classify"
    assert job["progress"]["current"] == 1 and job["progress"]["total"] == 3
    assert job["review"] is None
    env.classifier.fail_after = None
    assert env.client.post("/api/job/resume").status_code == 200
    assert env.client.get("/api/job").json()["status"] == "review"


def test_resume_without_paused_job_409(env):
    assert env.client.post("/api/job/resume").status_code == 409


# --- update -------------------------------------------------------------------------------------

def test_update_unknown_playlist_404(env):
    r = env.client.post("/api/playlists/PLnope/update")
    assert r.status_code == 404 and r.json()["error"]
    assert env.client.get("/api/job").json() is None


@pytest.mark.parametrize("pid", ["...", "%2E%2E", "a%5Cb", "..%5Cjob", "x.json", "%00"])
def test_playlist_ids_are_validated_before_use(env, tmp_path, pid):
    (tmp_path / "job.json").write_text('{"secret": 1}', encoding="utf-8")
    assert env.client.post(f"/api/playlists/{pid}/update").status_code == 404
    assert env.client.delete(f"/api/playlists/{pid}").status_code == 404
    assert (tmp_path / "job.json").exists()


def test_update_flow_with_skip(env):
    seed_playlist(env.ytm, env.records, "PL1", "Rock", included=["v0"])
    r = env.client.post("/api/playlists/PL1/update")
    assert r.status_code == 200
    job = env.client.get("/api/job").json()
    assert job["kind"] == "update" and job["status"] == "review"
    assert job["review"]["target_name"] == "Rock"
    all_ids = [s["video_id"] for k in ("include", "unsure", "exclude") for s in job["review"][k]]
    assert sorted(all_ids) == ["v1", "v2", "v3", "v4", "v5"]  # v0 was already checked
    assert env.client.post("/api/job/skip").status_code == 200
    assert env.client.get("/api/job").json()["status"] == "done"
    assert env.ytm.added["PL1"] == ["v0"]


def test_update_all_without_playlists_400(env):
    r = env.client.post("/api/update-all")
    assert r.status_code == 400 and r.json()["error"]


def test_update_all_reviews_each_playlist(env):
    seed_playlist(env.ytm, env.records, "PL1", "Rock")
    seed_playlist(env.ytm, env.records, "PL2", "Pop")
    assert env.client.post("/api/update-all").status_code == 200
    job = env.client.get("/api/job").json()
    assert (job["review"]["index"], job["review"]["count"]) == (1, 2)
    env.client.post("/api/job/approve", json={"video_ids": ["v0"]})
    job = env.client.get("/api/job").json()
    assert (job["review"]["index"], job["review"]["count"]) == (2, 2)
    assert job["review"]["target_name"] == "Pop"
    env.client.post("/api/job/approve", json={"video_ids": []})  # an Update may approve nothing
    assert env.client.get("/api/job").json()["status"] == "done"
    assert env.ytm.added["PL1"] == ["v0"]


def test_missing_playlist_id_is_exposed_and_forgettable(env):
    seed_playlist(env.ytm, env.records, "PL1", "Rock")
    env.ytm.deleted_playlists.add("PL1")
    assert env.client.post("/api/playlists/PL1/update").status_code == 200
    job = env.client.get("/api/job").json()
    assert job["status"] == "failed"
    assert job["missing_playlist_id"] == "PL1"
    assert "no longer exists" in job["error"]
    assert env.client.delete("/api/playlists/PL1").status_code == 200
    assert env.client.post("/api/job/cancel").status_code == 200
    assert env.client.get("/api/playlists").json() == []


def test_done_with_notice_when_nothing_new(env):
    seed_playlist(env.ytm, env.records, "PL1", "Rock", included=[s.video_id for s in env.ytm.liked])
    env.client.post("/api/playlists/PL1/update")
    job = env.client.get("/api/job").json()
    assert job["status"] == "done"
    assert job["notice"] == "No new liked songs since last update"


# --- errors never leak ----------------------------------------------------------------------------

def test_unexpected_errors_are_plain_json_without_traceback(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(env.runner, "start_create", boom)
    client = TestClient(env.app, base_url=BASE, raise_server_exceptions=False)
    r = client.post("/api/playlists", json={"name": "A", "description": "b"})
    assert r.status_code == 500
    assert list(r.json()) == ["error"]
    assert "secret" not in r.text and "Traceback" not in r.text


def test_unknown_api_route_is_json_404(env):
    r = env.client.get("/api/nope")
    assert r.status_code == 404 and list(r.json()) == ["error"]


# --- static + launcher ------------------------------------------------------------------------------

def test_index_html_is_served(env):
    r = env.client.get("/")
    assert r.status_code == 200
    assert "YT Music Sorter" in r.text


def test_server_binds_localhost_only(tmp_path):
    env = Env(tmp_path)
    config = launcher.server_config(8765, app=env.app)
    assert config.host == "127.0.0.1"
    assert config.port == 8765


def test_pick_port_prefers_default_and_falls_back_when_taken():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        port = launcher.pick_port(taken)
        assert port != taken and 0 < port < 65536
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))  # really free
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert launcher.pick_port(free) == free


def test_main_prints_url_and_opens_browser_unless_disabled(monkeypatch, capsys, tmp_path):
    started = threading.Event()
    opened: list[str] = []

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = False

        def run(self):
            self.started = True
            started.set()

    monkeypatch.setenv("YTSORTER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("YTSORTER_DEMO", "1")
    monkeypatch.delenv("YTSORTER_NO_BROWSER", raising=False)
    monkeypatch.setattr(launcher.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(launcher, "pick_port", lambda preferred=8765: 9999)
    launcher.main()
    out = capsys.readouterr().out
    assert "http://127.0.0.1:9999/" in out
    assert "Close this window to stop the app." in out
    deadline = time.time() + 3
    while not opened and time.time() < deadline:
        time.sleep(0.02)
    assert opened == ["http://127.0.0.1:9999/"]

    opened.clear()
    monkeypatch.setenv("YTSORTER_NO_BROWSER", "1")
    launcher.main()
    time.sleep(0.2)
    assert opened == []


# --- default wiring / demo ---------------------------------------------------------------------------

def test_default_app_demo_mode_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setenv("YTSORTER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("YTSORTER_DEMO", "1")
    monkeypatch.setattr(demo_data, "BATCH_DELAY", 0)
    client = TestClient(build_default_app(), base_url=BASE)
    assert client.get("/api/status").json() == {
        "claude": {"installed": True, "logged_in": True, "message": "ok"},
        "youtube": {"connected": True},
        "demo": True,
    }
    assert client.post("/api/playlists", json={"name": "Openings", "description": "anime openings"}
                       ).status_code == 200
    deadline = time.time() + 10  # real background thread
    job = client.get("/api/job").json()
    while job["status"] == "running" and time.time() < deadline:
        time.sleep(0.02)
        job = client.get("/api/job").json()
    assert job["status"] == "review"
    review = job["review"]
    assert len(review["include"]) > 20
    assert review["unsure"]
    client.post("/api/job/approve", json={"video_ids": [s["video_id"] for s in review["include"]]})
    deadline = time.time() + 10
    while client.get("/api/job").json()["status"] != "done" and time.time() < deadline:
        time.sleep(0.02)
    assert client.get("/api/playlists").json()[0]["song_count"] == len(review["include"])
    # demo records live apart from real ones
    assert (tmp_path / "demo" / "playlists").is_dir()
    assert not (tmp_path / "playlists").exists()
    assert (tmp_path / "demo" / "job.json").exists()


def test_default_app_real_mode_uses_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("YTSORTER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("YTSORTER_DEMO", raising=False)
    client = TestClient(build_default_app(), base_url=BASE)
    assert client.get("/api/status").json()["demo"] is False
    assert client.get("/api/status").json()["youtube"] == {"connected": False}
    assert client.get("/api/playlists").json() == []


# --- fix round 1: CSRF / DNS rebinding, races, demo reset --------------------------------------------

def test_foreign_host_header_is_rejected(env):
    r = env.client.get("/api/status", headers={"host": "evil.example"})
    assert r.status_code == 400
    r = env.client.post("/api/update-all", headers={"host": "evil.example:8765"})
    assert r.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8765", "localhost", "localhost:54321"])
def test_local_host_headers_are_accepted(env, host):
    assert env.client.get("/api/status", headers={"host": host}).status_code == 200


def test_cross_origin_post_is_blocked_and_starts_nothing(env):
    r = env.client.post("/api/playlists", json={"name": "A", "description": "b"},
                        headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.json() == {"error": "Request blocked: it didn't come from this app's page."}
    assert env.client.get("/api/job").json() is None
    assert env.client.post("/api/job/cancel", headers={"origin": "null"}).status_code == 403
    assert env.client.delete("/api/playlists/PL1", headers={"origin": "http://127.0.0.1:9"}).status_code == 403


def test_other_port_on_localhost_origin_is_blocked(env):
    r = env.client.post("/api/job/cancel", headers={"origin": "http://localhost:9999"})
    assert r.status_code == 403


def test_cross_site_fetch_metadata_is_blocked(env):
    for site in ("cross-site", "same-site"):
        r = env.client.post("/api/update-all", headers={"sec-fetch-site": site})
        assert r.status_code == 403, site
    assert env.client.post("/api/job/cancel", headers={"sec-fetch-site": "same-origin"}).status_code == 200
    assert env.client.post("/api/job/cancel", headers={"sec-fetch-site": "none"}).status_code == 200


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8765", "http://localhost:8765"])
def test_same_origin_post_is_allowed(env, origin):
    r = env.client.post("/api/playlists", json={"name": "A", "description": "b"}, headers={"origin": origin})
    assert r.status_code == 200
    assert env.client.get("/api/job").json()["status"] == "review"


def test_reads_with_foreign_origin_but_valid_host_still_work(env):
    r = env.client.get("/api/status", headers={"origin": "https://evil.example",
                                               "sec-fetch-site": "cross-site"})
    assert r.status_code == 200


def test_update_race_unknown_record_is_404(env, monkeypatch):
    seed_playlist(env.ytm, env.records, "PL1", "Rock")

    def gone(ids):
        raise KeyError(ids[0])

    monkeypatch.setattr(env.runner, "start_update", gone)
    assert env.client.post("/api/playlists/PL1/update").status_code == 404
    assert env.client.post("/api/update-all").status_code == 404


def test_playlist_id_must_fully_match(env):
    seed_playlist(env.ytm, env.records, "PL1", "Rock")
    assert env.client.delete("/api/playlists/PL1%0A").status_code == 404
    assert env.records.get("PL1") is not None


def test_forget_blocked_while_a_job_is_running_or_applying(tmp_path):
    from ytsorter.jobs import Job, Progress
    env = Env(tmp_path)
    seed_playlist(env.ytm, env.records, "PL1", "Rock")
    env.runner._run_in_background = lambda fn: None  # job stays "running"
    env.client.post("/api/playlists", json={"name": "A", "description": "b"})
    assert env.client.get("/api/job").json()["status"] == "running"
    r = env.client.delete("/api/playlists/PL1")
    assert r.status_code == 409 and r.json()["error"]
    assert env.records.get("PL1") is not None
    env.runner._job.status = "applying"
    assert env.client.delete("/api/playlists/PL1").status_code == 409
    for status in ("review", "paused", "failed"):
        env.runner._job.status = status
        assert env.client.delete("/api/playlists/PL1").status_code == 200, status
        seed_playlist(env.ytm, env.records, "PL1", "Rock")
    env.runner.cancel()
    assert env.client.delete("/api/playlists/PL1").status_code == 200


def test_demo_mode_starts_from_a_clean_demo_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("YTSORTER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("YTSORTER_DEMO", "1")
    stale = tmp_path / "demo" / "playlists"
    stale.mkdir(parents=True)
    (stale / "PLold.json").write_text("{}", encoding="utf-8")
    (tmp_path / "demo" / "job.json").write_text("{}", encoding="utf-8")
    real = tmp_path / "playlists"
    real.mkdir()
    (real / "PLreal.json").write_text("{}", encoding="utf-8")
    client = TestClient(build_default_app(), base_url=BASE)
    assert client.get("/api/playlists").json() == []
    assert client.get("/api/job").json() is None
    assert not (stale / "PLold.json").exists()
    assert (real / "PLreal.json").exists()  # real data is never touched


def test_real_mode_never_deletes_anything(monkeypatch, tmp_path):
    monkeypatch.setenv("YTSORTER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("YTSORTER_DEMO", raising=False)
    keep = [tmp_path / "demo" / "playlists" / "PLdemo.json", tmp_path / "playlists" / "PLreal.json"]
    for path in keep:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    build_default_app()
    assert all(path.exists() for path in keep)


# --- names YouTube Music refuses / forgetting a playlist the job uses -------------------------

NAME_MSG = "Playlist names can't contain < or > and must be 150 characters or fewer."


@pytest.mark.parametrize("name", ["Rock <3", "a>b", "x" * 151])
def test_create_with_a_name_youtube_music_refuses_is_400(env, name):
    r = create_playlist(env, name=name)
    assert r.status_code == 400
    assert r.json() == {"error": NAME_MSG}
    assert env.client.get("/api/job").json() is None


def test_forget_blocked_for_a_target_of_the_current_job(env):
    seed_playlist(env.ytm, env.records, "PL1", "Rock")
    seed_playlist(env.ytm, env.records, "PL2", "Jazz")
    assert env.client.post("/api/update-all").status_code == 200
    assert env.client.get("/api/job").json()["status"] == "review"
    for status in ("review", "paused", "failed"):
        env.runner._job.status = status
        for pid in ("PL1", "PL2"):
            r = env.client.delete(f"/api/playlists/{pid}")
            assert r.status_code == 409 and r.json()["error"], (status, pid)
    assert env.records.get("PL1") is not None and env.records.get("PL2") is not None
    env.runner._job.status = "done"
    assert env.client.delete("/api/playlists/PL1").status_code == 200

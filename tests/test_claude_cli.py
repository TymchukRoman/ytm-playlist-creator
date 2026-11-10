import json
import subprocess

import pytest

from ytsorter import claude_cli
from ytsorter.claude_cli import (
    ClaudeAuthError,
    ClaudeClassifier,
    ClaudeError,
    ClaudeStatus,
    ClaudeTemporaryError,
    ClaudeUsageLimitError,
)
from ytsorter.fakes import FakeClassifier
from ytsorter.models import Decision, Song


def proc(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def envelope(decisions, **extra) -> str:
    return json.dumps({"is_error": False, "structured_output": {"decisions": decisions}, **extra})


class FakeRunner:
    """Records (args, stdin) and replays canned CompletedProcess objects in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[list[str], str]] = []

        self.timeouts: list = []

    def __call__(self, args, stdin_text, timeout=None):
        self.calls.append((args, stdin_text))
        self.timeouts.append(timeout)
        return self.responses.pop(0)


def songs2():
    return [Song("vid1", "Gurenge", ["LiSA"], None, "3:56"), Song("vid2", "Smells Like Teen Spirit", ["Nirvana"])]


def classifier(runner, **kw):
    return ClaudeClassifier(runner=runner, executable="claude-exe", **kw)


def prompt_songs(stdin: str) -> list[dict]:
    """The JSON array of songs embedded in the prompt (its last line)."""
    return json.loads(stdin.strip().splitlines()[-1])


def test_command_disables_tools_and_sends_prompt_on_stdin():
    runner = FakeRunner(proc(envelope([{"n": 1, "verdict": "include", "reason": "a"}, {"n": 2, "verdict": "exclude", "reason": "b"}])))
    classifier(runner, model="haiku").classify_batch("anime openings", songs2())

    args, stdin = runner.calls[0]
    assert args[0] == "claude-exe"
    assert args[1:3] == ["-p", "--tools"] and args[3] == ""
    assert "--no-session-persistence" in args
    assert args[args.index("--output-format") + 1] == "json"
    assert args[args.index("--model") + 1] == "haiku"
    assert args[args.index("--system-prompt") + 1] == claude_cli.SYSTEM_PROMPT
    assert "--json-schema" in args
    assert args[args.index("--json-schema") + 1] == claude_cli.SCHEMA_JSON
    # Ruling R1: don't load the user's plugins/hooks/MCP/CLAUDE.md on every batch call.
    assert args[args.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in args
    assert "anime openings" in stdin
    assert "Gurenge" in stdin and "Smells Like Teen Spirit" in stdin
    assert [s["n"] for s in prompt_songs(stdin)] == [1, 2]


def test_schema_is_valid_json_with_expected_shape():
    schema = json.loads(claude_cli.SCHEMA_JSON)
    item = schema["properties"]["decisions"]["items"]
    assert item["required"] == ["n", "verdict", "reason"]
    assert item["properties"]["verdict"]["enum"] == ["include", "exclude", "unsure"]


def test_maps_numbers_back_to_video_ids():
    runner = FakeRunner(proc(envelope([{"n": 2, "verdict": "exclude", "reason": "x"}, {"n": 1, "verdict": "include", "reason": "y"}])))
    result = classifier(runner).classify_batch("d", songs2())
    assert result == [Decision("vid1", "include", "y"), Decision("vid2", "exclude", "x")]


def test_falls_back_to_result_text_json():
    text = '```json\n{"decisions": [{"n": 1, "verdict": "include", "reason": "a"}, {"n": 2, "verdict": "unsure", "reason": "b"}]}\n```'
    runner = FakeRunner(proc(json.dumps({"is_error": False, "result": text})))
    result = classifier(runner).classify_batch("d", songs2())
    assert result == [Decision("vid1", "include", "a"), Decision("vid2", "unsure", "b")]


def test_retries_once_then_marks_missing_unsure():
    runner = FakeRunner(
        proc(envelope([{"n": 1, "verdict": "include", "reason": "y"}])),
        proc(json.dumps({"is_error": False, "result": "sorry, no JSON here"})),
    )
    result = classifier(runner).classify_batch("d", songs2())
    assert result == [Decision("vid1", "include", "y"), Decision("vid2", "unsure", "Claude didn't answer for this song")]
    assert len(runner.calls) == 2
    # The retry only carries the song that was missing, renumbered from 1.
    retry_songs = prompt_songs(runner.calls[1][1])
    assert [(s["n"], s["title"]) for s in retry_songs] == [(1, "Smells Like Teen Spirit")]


def test_retry_fills_in_missing_songs():
    runner = FakeRunner(
        proc(envelope([{"n": 1, "verdict": "include", "reason": "y"}])),
        proc(envelope([{"n": 1, "verdict": "exclude", "reason": "z"}])),
    )
    result = classifier(runner).classify_batch("d", songs2())
    assert result == [Decision("vid1", "include", "y"), Decision("vid2", "exclude", "z")]
    assert len(runner.calls) == 2


def test_unparseable_first_response_retries_whole_batch():
    runner = FakeRunner(
        proc("this is not json at all"),
        proc(envelope([{"n": 1, "verdict": "include", "reason": "a"}, {"n": 2, "verdict": "exclude", "reason": "b"}])),
    )
    result = classifier(runner).classify_batch("d", songs2())
    assert result == [Decision("vid1", "include", "a"), Decision("vid2", "exclude", "b")]
    assert [s["n"] for s in prompt_songs(runner.calls[1][1])] == [1, 2]


UNREADABLE_MSG = "Claude's answer couldn't be read. Your progress is saved — press Resume to try again."


def test_everything_unparseable_twice_raises_instead_of_all_unsure():
    runner = FakeRunner(proc("garbage"), proc("more garbage"))
    with pytest.raises(ClaudeError) as exc:
        classifier(runner).classify_batch("d", songs2())
    assert str(exc.value) == UNREADABLE_MSG
    assert isinstance(exc.value, ClaudeTemporaryError)  # the job pauses for Resume
    assert not isinstance(exc.value, (ClaudeAuthError, ClaudeUsageLimitError))
    assert len(runner.calls) == 2


def test_no_valid_decision_in_either_reply_raises():
    runner = FakeRunner(proc(envelope([{"n": 9, "verdict": "include", "reason": "x"}])), proc(envelope([])))
    with pytest.raises(ClaudeError, match="couldn't be read"):
        classifier(runner).classify_batch("d", songs2())


def test_prompt_describes_the_json_shape_for_the_text_fallback():
    runner = FakeRunner(proc(envelope([{"n": 1, "verdict": "include", "reason": "a"}, {"n": 2, "verdict": "exclude", "reason": "b"}])))
    classifier(runner).classify_batch("d", songs2())
    stdin = runner.calls[0][1]
    assert '{"decisions":[{"n":<int>,"verdict":"include|exclude|unsure","reason":"..."}]}' in stdin


@pytest.mark.parametrize("structured", ["not an object", {"other": 1}, {"decisions": "nope"}, None])
def test_invalid_structured_output_falls_back_to_result_text(structured):
    text = '{"decisions": [{"n": 1, "verdict": "include", "reason": "a"}, {"n": 2, "verdict": "exclude", "reason": "b"}]}'
    runner = FakeRunner(proc(json.dumps({"is_error": False, "structured_output": structured, "result": text})))
    result = classifier(runner).classify_batch("d", songs2())
    assert result == [Decision("vid1", "include", "a"), Decision("vid2", "exclude", "b")]
    assert len(runner.calls) == 1


def test_nonzero_exit_with_valid_success_envelope_is_parsed():
    runner = FakeRunner(proc(envelope([{"n": 1, "verdict": "include", "reason": "a"}, {"n": 2, "verdict": "unsure", "reason": "b"}]),
                             returncode=1, stderr="some warning"))
    result = classifier(runner).classify_batch("d", songs2())
    assert result == [Decision("vid1", "include", "a"), Decision("vid2", "unsure", "b")]
    assert len(runner.calls) == 1


TOO_OLD_MSG = "Your Claude Code is too old for this app. Run `claude update`, then try again."


@pytest.mark.parametrize("out", [proc("", returncode=1, stderr="error: unknown option '--json-schema'"),
                                 proc(json.dumps({"is_error": True, "result": "Error: Unknown option: --tools"}))])
def test_old_claude_code_unknown_option_says_update(out):
    with pytest.raises(ClaudeError) as exc:
        classifier(FakeRunner(out)).classify_batch("d", songs2())
    assert str(exc.value) == TOO_OLD_MSG


def test_out_of_range_duplicate_and_invalid_decisions_are_ignored():
    runner = FakeRunner(
        proc(
            envelope(
                [
                    {"n": 1, "verdict": "include", "reason": "first"},
                    {"n": 1, "verdict": "exclude", "reason": "dupe"},
                    {"n": 99, "verdict": "include", "reason": "out of range"},
                    {"n": 2, "verdict": "maybe", "reason": "bad verdict"},
                ]
            )
        ),
        proc(envelope([{"n": 1, "verdict": "exclude", "reason": "retry"}])),
    )
    result = classifier(runner).classify_batch("d", songs2())
    assert result == [Decision("vid1", "include", "first"), Decision("vid2", "exclude", "retry")]


def test_empty_batch_makes_no_call():
    runner = FakeRunner()
    assert classifier(runner).classify_batch("d", []) == []
    assert runner.calls == []


def test_usage_limit_error():
    runner = FakeRunner(proc(json.dumps({"is_error": True, "result": "Claude AI usage limit reached|1794240000"})))
    with pytest.raises(ClaudeUsageLimitError):
        classifier(runner).classify_batch("d", songs2())
    assert len(runner.calls) == 1  # error envelopes are not retried


def test_auth_error():
    runner = FakeRunner(proc(json.dumps({"is_error": True, "subtype": "success", "result": "Failed to authenticate: OAuth session expired"})))
    with pytest.raises(ClaudeAuthError):
        classifier(runner).classify_batch("d", songs2())
    assert len(runner.calls) == 1


def test_other_error_envelope_raises_claude_error():
    runner = FakeRunner(proc(json.dumps({"is_error": True, "result": "something odd"})))
    with pytest.raises(ClaudeError) as exc:
        classifier(runner).classify_batch("d", songs2())
    assert not isinstance(exc.value, (ClaudeAuthError, ClaudeUsageLimitError))


def test_error_envelope_on_retry_is_raised():
    runner = FakeRunner(
        proc(envelope([{"n": 1, "verdict": "include", "reason": "y"}])),
        proc(json.dumps({"is_error": True, "result": "rate limit exceeded"})),
    )
    with pytest.raises(ClaudeUsageLimitError):
        classifier(runner).classify_batch("d", songs2())


def test_nonzero_exit_with_unparseable_stdout_raises_with_stderr_tail():
    runner = FakeRunner(proc("", returncode=1, stderr="boom\nsomething exploded"))
    with pytest.raises(ClaudeError, match="something exploded"):
        classifier(runner).classify_batch("d", songs2())
    assert len(runner.calls) == 1


def test_timeout_raises_claude_error():
    def runner(args, stdin_text):
        raise subprocess.TimeoutExpired(args, 300)

    with pytest.raises(ClaudeTemporaryError, match="took too long"):
        classifier(runner).classify_batch("d", songs2())


def test_unicode_roundtrip():
    title = 'Kick Back "チェンソーマン" 🎸\nline2'
    song = Song("v1", title, ["米津玄師", "Земфира"], "Альбом", "3:00")
    reason = "Подходит: опенинг аниме"
    runner = FakeRunner(proc(envelope([{"n": 1, "verdict": "include", "reason": reason}])))
    result = classifier(runner).classify_batch("аниме опенинги", [song])

    stdin = runner.calls[0][1]
    assert "аниме опенинги" in stdin
    sent = prompt_songs(stdin)[0]
    assert sent == {"n": 1, "title": title, "artists": ["米津玄師", "Земфира"], "album": "Альбом", "duration": "3:00"}
    assert result == [Decision("v1", "include", reason)]


def test_status_not_installed(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    status = ClaudeClassifier(runner=FakeRunner(), executable=None).status()
    assert status.installed is False and status.logged_in is False
    assert status.message


def test_status_logged_in():
    runner = FakeRunner(proc(json.dumps({"loggedIn": True, "authMethod": "claude.ai"})))
    status = classifier(runner).status()
    assert status.installed and status.logged_in
    assert runner.calls[0][0] == ["claude-exe", "auth", "status", "--json"]


def test_status_logged_out():
    runner = FakeRunner(proc(json.dumps({"loggedIn": False, "authMethod": "none"})))
    status = classifier(runner).status()
    assert status.installed is True and status.logged_in is False
    assert "Open a terminal, run `claude`, and log in" in status.message


def test_status_check_uses_a_short_timeout():
    runner = FakeRunner(proc(json.dumps({"loggedIn": True})))
    classifier(runner).status()
    assert runner.timeouts == [claude_cli.STATUS_TIMEOUT]
    assert 10 <= claude_cli.STATUS_TIMEOUT <= 30


def test_status_timeout_is_not_logged_in_with_plain_message():
    def runner(args, stdin_text, timeout=None):
        raise subprocess.TimeoutExpired(args, timeout)

    status = classifier(runner).status()
    assert status.installed is True and status.logged_in is False
    assert "too long" in status.message and "Open a terminal, run `claude`, and log in" in status.message


def test_status_uses_which_when_no_executable(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "/found/claude")
    runner = FakeRunner(proc(json.dumps({"loggedIn": True})))
    ClaudeClassifier(runner=runner).status()
    assert runner.calls[0][0][0] == "/found/claude"


def test_status_unparseable_output_is_not_logged_in():
    status = classifier(FakeRunner(proc("nope", returncode=1))).status()
    assert status.installed is True and status.logged_in is False


def test_classify_without_executable_raises(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    with pytest.raises(ClaudeError, match="not installed"):
        ClaudeClassifier(runner=FakeRunner()).classify_batch("d", songs2())


def test_default_runner_runs_in_an_empty_work_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("YTSORTER_DATA_DIR", str(tmp_path))
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return proc("{}")

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    claude_cli.default_runner(["x", "", "y"], "hello")
    assert seen["args"] == ["x", "", "y"]
    assert seen["kwargs"]["input"] == "hello"
    assert seen["kwargs"]["encoding"] == "utf-8"
    assert seen["kwargs"]["timeout"] == 300
    assert seen["kwargs"]["capture_output"] is True
    assert seen["kwargs"]["cwd"] == tmp_path / "claude-work"  # not next to youtube_auth.json
    assert (tmp_path / "claude-work").is_dir()
    claude_cli.default_runner(["x"], "", timeout=20)
    assert seen["kwargs"]["timeout"] == 20


class TestFakeClassifier:
    def test_default_rule_matches_description_words(self):
        fake = FakeClassifier()
        result = fake.classify_batch("lisa rock", songs2())
        assert result == [Decision("vid1", "include", "matched 'lisa'"), Decision("vid2", "exclude", "no match")]
        assert fake.status() == ClaudeStatus(True, True, "ok")

    def test_custom_rules(self):
        fake = FakeClassifier(rules=lambda desc, song: ("unsure", song.title))
        assert fake.classify_batch("x", songs2()) == [Decision("vid1", "unsure", "Gurenge"), Decision("vid2", "unsure", "Smells Like Teen Spirit")]

    def test_fail_after_raises_usage_limit_after_n_batches(self):
        fake = FakeClassifier(fail_after=2)
        fake.classify_batch("x", songs2())
        fake.classify_batch("x", songs2())
        with pytest.raises(ClaudeUsageLimitError):
            fake.classify_batch("x", songs2())


@pytest.mark.parametrize("output", ["error: unknown command 'auth'", "error: unknown option '--json'"])
def test_status_old_claude_code_says_update(output):
    status = classifier(FakeRunner(proc("", returncode=1, stderr=output))).status()
    assert status.installed is True and status.logged_in is False
    assert "claude update" in status.message


# --- the user's model and effort choice ---------------------------------------------------------

from ytsorter.settings import ClaudeSettings  # noqa: E402


def _flag(args, name):
    return args[args.index(name) + 1] if name in args else None


def test_default_choice_is_sonnet_without_effort_flag():
    runner = FakeRunner(proc(envelope([{"n": 1, "verdict": "include", "reason": "a"},
                                       {"n": 2, "verdict": "include", "reason": "b"}])))
    classifier(runner).classify_batch("d", songs2())
    args = runner.calls[0][0]
    assert _flag(args, "--model") == "sonnet" and "--effort" not in args


def test_chosen_model_and_effort_are_passed_and_reread_each_call():
    choice = [ClaudeSettings("opus", "max")]
    reply = envelope([{"n": 1, "verdict": "include", "reason": "a"}, {"n": 2, "verdict": "include", "reason": "b"}])
    runner = FakeRunner(proc(reply), proc(reply))
    clf = ClaudeClassifier(runner=runner, executable="claude-exe", settings=lambda: choice[0])
    clf.classify_batch("d", songs2())
    choice[0] = ClaudeSettings("haiku", "default")
    clf.classify_batch("d", songs2())
    first, second = runner.calls[0][0], runner.calls[1][0]
    assert (_flag(first, "--model"), _flag(first, "--effort")) == ("opus", "max")
    assert _flag(second, "--model") == "haiku" and "--effort" not in second

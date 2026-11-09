"""Test doubles used by the test suite and by the app's offline/demo wiring."""
import re
from collections.abc import Callable

from .claude_cli import ClaudeStatus, ClaudeUsageLimitError
from .models import Decision, Song, Verdict

Rules = Callable[[str, Song], tuple[Verdict, str]]


def _default_rule(description: str, song: Song) -> tuple[Verdict, str]:
    haystack = f"{song.title} {' '.join(song.artists)}".lower()
    for word in re.findall(r"\w+", description.lower()):
        if word in haystack:
            return "include", f"matched '{word}'"
    return "exclude", "no match"


class FakeClassifier:
    """Stands in for ClaudeClassifier. `fail_after=N` raises a usage-limit error on batch N+1."""

    def __init__(self, rules: Rules | None = None, fail_after: int | None = None):
        self.rules = rules or _default_rule
        self.fail_after = fail_after
        self.batches = 0

    def status(self) -> ClaudeStatus:
        return ClaudeStatus(True, True, "ok")

    def classify_batch(self, description: str, songs: list[Song]) -> list[Decision]:
        if self.fail_after is not None and self.batches >= self.fail_after:
            raise ClaudeUsageLimitError("Claude usage limit reached (fake)")
        self.batches += 1
        decisions = []
        for song in songs:
            verdict, reason = self.rules(description, song)
            decisions.append(Decision(song.video_id, verdict, reason))
        return decisions

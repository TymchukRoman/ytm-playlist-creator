"""The user's Claude choices (model and effort), saved in the data folder."""
from dataclasses import asdict, dataclass
from pathlib import Path

from .store import read_json, write_json_atomic

# (CLI alias, label, plain-language hint) — every pair below was checked with `claude -p`.
MODELS = [
    ("haiku", "Haiku", "Fastest, and uses the least of your Claude limit. Fine for clear-cut playlists."),
    ("sonnet", "Sonnet", "Recommended. Good judgment, and quick."),
    ("opus", "Opus", "More careful with subtle playlists. Uses more of your Claude limit."),
    ("fable", "Fable", "The most capable model. Uses the most of your Claude limit."),
]
EFFORTS = [
    ("default", "Default", "Claude Code decides."),
    ("low", "Low", "Quickest answers."),
    ("medium", "Medium", ""),
    ("high", "High", ""),
    ("xhigh", "Extra high", ""),
    ("max", "Max", "Thinks the longest. Slowest, and uses the most of your limit."),
]
MODEL_IDS = {m[0] for m in MODELS}
EFFORT_IDS = {e[0] for e in EFFORTS}
BAD_SETTINGS_MSG = "Pick one of the listed models and effort levels."


@dataclass(frozen=True)
class ClaudeSettings:
    model: str = "sonnet"
    effort: str = "default"  # "default" means: don't pass --effort

    def __post_init__(self):
        if self.model not in MODEL_IDS or self.effort not in EFFORT_IDS:
            raise ValueError(BAD_SETTINGS_MSG)


class SettingsStore:
    """settings.json in the data folder; `path=None` keeps the choice in memory only (tests)."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None
        self._memory = ClaudeSettings()

    def load(self) -> ClaudeSettings:
        if self.path is None:
            return self._memory
        try:
            data = read_json(self.path) or {}
            return ClaudeSettings(data.get("model", "sonnet"), data.get("effort", "default"))
        except (OSError, ValueError, AttributeError):  # unreadable or hand-edited: fall back
            return ClaudeSettings()

    def save(self, settings: ClaudeSettings) -> None:
        if self.path is None:
            self._memory = settings
        else:
            write_json_atomic(self.path, asdict(settings))


def options_json() -> dict:
    return {
        "models": [{"id": i, "label": label, "hint": hint} for i, label, hint in MODELS],
        "efforts": [{"id": i, "label": label, "hint": hint} for i, label, hint in EFFORTS],
    }

import json

import pytest

from ytsorter.settings import EFFORT_IDS, MODEL_IDS, ClaudeSettings, SettingsStore


def test_defaults_are_sonnet_and_default_effort():
    assert ClaudeSettings() == ClaudeSettings("sonnet", "default")


def test_offered_choices():
    assert MODEL_IDS == {"haiku", "sonnet", "opus", "fable"}
    assert EFFORT_IDS == {"default", "low", "medium", "high", "xhigh", "max"}


@pytest.mark.parametrize("model,effort", [("gpt", "low"), ("sonnet", "turbo"), ("", "")])
def test_unknown_choices_rejected(model, effort):
    with pytest.raises(ValueError):
        ClaudeSettings(model, effort)


def test_store_saves_and_loads(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    assert store.load() == ClaudeSettings()
    store.save(ClaudeSettings("opus", "high"))
    assert SettingsStore(tmp_path / "settings.json").load() == ClaudeSettings("opus", "high")
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8")) == {"model": "opus", "effort": "high"}


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"model": "gpt-9", "effort": "low"}'])
def test_store_falls_back_to_defaults_for_a_bad_file(tmp_path, content):
    (tmp_path / "settings.json").write_text(content, encoding="utf-8")
    assert SettingsStore(tmp_path / "settings.json").load() == ClaudeSettings()


def test_memory_store():
    store = SettingsStore(None)
    store.save(ClaudeSettings("haiku", "low"))
    assert store.load() == ClaudeSettings("haiku", "low")

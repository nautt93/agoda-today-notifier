"""Quiet-hour settings migration and non-destructive configuration validation."""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from booking_notifier import config as settings
from booking_notifier.config import DEFAULT_CONFIG, ConfigStore, atomic_json_write


def _quiet_pair(config):
    return config["quiet_start_time"], config["quiet_end_time"]


def test_fresh_configuration_uses_legacy_quiet_period_without_creating_file(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = store.load()
    assert _quiet_pair(DEFAULT_CONFIG) == _quiet_pair(config) == ("00:00", "08:00")
    assert config["quiet_hours_enabled"] is True
    assert not store.path.exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_legacy_configuration_missing_time_keys_retains_toggle_and_all_other_settings(tmp_path, enabled):
    store = ConfigStore(tmp_path / "config.json")
    original = {
        "email_address": "hotel@example.invalid", "password_encrypted": "synthetic-encrypted-secret",
        "quiet_hours_enabled": enabled, "agoda_sound_file": "custom-agoda.mp3",
        "booking_com_sound_file": "custom-booking.wav", "booking_com_browser": "chrome",
        "booking_com_enrichment": False, "poll_seconds": 90, "f92_port": "COM8",
        "update_manifest_source": "https://example.invalid/update.json", "retired_unknown_key": "drop",
    }
    atomic_json_write(store.path, original)
    previous_bytes = store.path.read_bytes()
    loaded = store.load()
    assert _quiet_pair(loaded) == ("00:00", "08:00")
    for key, value in original.items():
        if key in DEFAULT_CONFIG:
            assert loaded[key] == value
    assert "retired_unknown_key" not in loaded
    assert store.path.read_bytes() == previous_bytes
    store.save(loaded)
    assert store.load() == loaded
    assert "retired_unknown_key" not in json.loads(store.path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("start", "end", "expected"), [
    (" 7:05 ", " 9:30 ", ("07:05", "09:30")),
    ("22:15", "6:45", ("22:15", "06:45")),
    ("08:00", "17:00", ("08:00", "17:00")),
    ("00:00", "23:59", ("00:00", "23:59")),
])
def test_load_and_save_canonicalize_time_pair_without_mutating_callers(tmp_path, start, end, expected):
    store = ConfigStore(tmp_path / "config.json")
    draft = {"quiet_start_time": start, "quiet_end_time": end, "quiet_hours_enabled": False}
    original_draft = dict(draft)
    atomic_json_write(store.path, draft)
    assert _quiet_pair(store.load()) == expected
    store.save(draft)
    assert draft == original_draft
    assert _quiet_pair(store.load()) == expected
    persisted = json.loads(store.path.read_text(encoding="utf-8"))
    assert _quiet_pair(persisted) == expected
    assert persisted["quiet_hours_enabled"] is False


@pytest.mark.parametrize(("start", "end"), [
    ("not-a-time", "06:45"), ("22:15", "not-a-time"),
    ("24:00", "06:45"), ("22:15", "06:60"),
    ("", "06:45"), ("22:15", ""),
    (None, "06:45"), ("22:15", None),
    (0, "06:45"), ("22:15", 8),
    (False, "06:45"), ("22:15", ["06:45"]),
    ("06:45", "6:45"), ("00:00", "00:00"),
])
def test_invalid_stored_pair_loads_legacy_fallback_together_without_overwriting_file(tmp_path, start, end):
    store = ConfigStore(tmp_path / "config.json")
    original = {
        "quiet_start_time": start, "quiet_end_time": end, "quiet_hours_enabled": False,
        "password_encrypted": "synthetic-unchanged-secret", "booking_com_browser": "msedge",
        "trip_sound_file": "custom-trip.wav",
    }
    atomic_json_write(store.path, original)
    previous_bytes = store.path.read_bytes()
    loaded = store.load()
    assert _quiet_pair(loaded) == ("00:00", "08:00")
    assert loaded["quiet_hours_enabled"] is False
    assert loaded["password_encrypted"] == original["password_encrypted"]
    assert loaded["booking_com_browser"] == original["booking_com_browser"]
    assert loaded["trip_sound_file"] == original["trip_sound_file"]
    assert store.path.read_bytes() == previous_bytes


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(("start", "end"), [
    ("24:00", "06:45"), ("22:15", "06:60"), ("", "06:45"),
    (None, "06:45"), ("22:15", 8), ("06:45", "6:45"),
])
def test_invalid_save_raises_before_atomic_write_and_keeps_previous_configuration(tmp_path, monkeypatch, enabled, start, end):
    store = ConfigStore(tmp_path / "config.json")
    store.save({
        "quiet_start_time": "22:15", "quiet_end_time": "06:45", "quiet_hours_enabled": enabled,
        "email_address": "hotel@example.invalid", "password_encrypted": "synthetic-unchanged-secret",
        "agoda_sound_file": "custom-agoda.mp3", "booking_com_browser": "chrome",
    })
    previous_bytes = store.path.read_bytes()
    previous = store.load()
    write = Mock(wraps=atomic_json_write)
    monkeypatch.setattr(settings, "atomic_json_write", write)
    with pytest.raises(ValueError):
        store.save({**previous, "quiet_start_time": start, "quiet_end_time": end,
                    "password_encrypted": "synthetic-different-draft-secret"})
    write.assert_not_called()
    assert store.path.read_bytes() == previous_bytes
    assert store.load() == previous


def test_invalid_first_save_does_not_create_configuration_directory(tmp_path):
    store = ConfigStore(tmp_path / "not-created" / "config.json")
    with pytest.raises(ValueError):
        store.save({"quiet_start_time": "08:00", "quiet_end_time": "8:00"})
    assert not store.path.parent.exists()


def test_disabled_valid_custom_times_persist_and_unknown_keys_are_dropped(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.save({
        "quiet_hours_enabled": False, "quiet_start_time": "23:30", "quiet_end_time": "07:15",
        "unknown_time_override": "ignore", "password_encrypted": "synthetic-encrypted-secret",
        "booking_com_browser": "msedge", "booking_com_enrichment": True,
        "agoda_sound_file": "custom-agoda.mp3", "expedia_sound_file": "custom-expedia.wav",
        "traveloka_sound_file": "custom-traveloka.mp3", "trip_sound_file": "custom-trip.wav",
        "booking_com_sound_file": "custom-booking.mp3",
    })
    config = store.load()
    assert _quiet_pair(config) == ("23:30", "07:15") and config["quiet_hours_enabled"] is False
    assert "unknown_time_override" not in config
    persisted = json.loads(store.path.read_text(encoding="utf-8"))
    assert "unknown_time_override" not in persisted
    assert persisted == config
    store.save(config)
    assert store.load() == config

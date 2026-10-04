from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest

import app as desktop
from app import BookingNotifierApp
from booking_notifier import audio
from booking_notifier.audio import SOURCE_SOUND_FILES, SOURCE_SOUND_KEYS, SOURCE_SOUND_PACK, SOURCE_SOUND_SHA256
from booking_notifier.config import DEFAULT_CONFIG, ConfigStore


def test_new_sound_pack_replaces_old_choices_once_without_changing_credentials(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.save({"email_address": "hotel@example.invalid", "password_encrypted": "unchanged-encrypted-secret",
                "agoda_sound_file": "old-agoda.wav", "expedia_sound_file": "old-expedia.wav",
                "sound_file": "old-common.mp3", "f92_port": "COM6", "poll_seconds": 90})
    previous = store.load()
    installed = store.install_source_sound_pack(tmp_path / "sounds")
    assert installed["source_sound_pack"] == SOURCE_SOUND_PACK
    for source, filename in SOURCE_SOUND_FILES.items():
        path = Path(installed[SOURCE_SOUND_KEYS[source]])
        assert path == tmp_path / "sounds" / filename
        assert hashlib.sha256(path.read_bytes()).hexdigest() == SOURCE_SOUND_SHA256[source]
        pcm = tmp_path / "sounds" / audio.SOURCE_PCM_FILES[source]
        assert hashlib.sha256(pcm.read_bytes()).hexdigest() == audio.SOURCE_PCM_SHA256[source]
    for key in ("email_address", "password_encrypted", "sound_file", "f92_port", "poll_seconds", "update_manifest_source"):
        assert installed[key] == previous[key]
    # Once applied, an intentional later custom source choice survives restarts.
    installed["agoda_sound_file"] = str(tmp_path / "my-custom.mp3")
    store.save(installed)
    assert store.install_source_sound_pack(tmp_path / "sounds") == installed


def test_partial_pack_failure_does_not_change_saved_sound_choices(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.json")
    store.save({"agoda_sound_file": "keep-agoda.wav", "expedia_sound_file": "keep-expedia.wav"})
    original = store.path.read_bytes()
    monkeypatch.setattr(audio, "bundled_source_sound", Mock(side_effect=[tmp_path / "1-agoda.mp3", OSError("missing asset")]))
    with pytest.raises(OSError, match="missing asset"):
        store.install_source_sound_pack(tmp_path / "sounds")
    assert store.path.read_bytes() == original
    assert store.load()["source_sound_pack"] == ""


def test_pcm_pack_failure_does_not_commit_partial_mapping(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.json")
    store.save({"agoda_sound_file": "keep-agoda.wav", "traveloka_sound_file": "keep-traveloka.mp3"})
    original = store.path.read_bytes()
    monkeypatch.setattr(audio, "bundled_source_pcm", Mock(side_effect=OSError("missing PCM asset")))
    with pytest.raises(OSError, match="missing PCM asset"):
        store.install_source_sound_pack(tmp_path / "sounds")
    assert store.path.read_bytes() == original
    assert store.load()["source_sound_pack"] == ""


def test_saving_settings_preserves_pack_marker_and_all_three_sound_choices(tmp_path, monkeypatch):
    config = {**DEFAULT_CONFIG, "source_sound_pack": SOURCE_SOUND_PACK, "email_address": "test@example.invalid",
              "agoda_sound_file": "custom-a.mp3", "expedia_sound_file": "custom-e.wav", "traveloka_sound_file": "custom-t.mp3"}
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.config = config
    variables = {"provider": "provider", "host": "imap_host", "port": "imap_port", "email": "email_address",
                 "password": "password_encrypted", "poll": "poll_seconds", "scan_days": "scan_days", "sound": "sound_file",
                 "quiet": "quiet_hours_enabled", "start_windows": "start_with_windows", "start_minimized": "start_minimized",
                 "f92_enabled": "f92_enabled", "f92_port": "f92_port", "f92_sound": "f92_sound_index",
                 "f92_builtin_sound": "f92_builtin_sound_enabled", "update_source": "update_manifest_source"}
    for variable, key in variables.items():
        setattr(app, f"{variable}_var", Mock(get=Mock(return_value=str(config[key]) if key not in {
            "quiet_hours_enabled", "start_with_windows", "start_minimized", "f92_enabled", "f92_builtin_sound_enabled"} else config[key])))
    app.source_sound_vars = {source.title(): Mock(get=Mock(return_value=config[key])) for source, key in SOURCE_SOUND_KEYS.items()}
    monkeypatch.setattr(desktop, "protect_secret", lambda value: "unchanged-encrypted-secret")
    collected = app._collect_config()
    store = ConfigStore(tmp_path / "config.json")
    store.save(collected)
    assert collected["source_sound_pack"] == SOURCE_SOUND_PACK
    assert store.install_source_sound_pack(tmp_path / "sounds") == store.load()
    assert all(store.load()[key] == config[key] for key in SOURCE_SOUND_KEYS.values())

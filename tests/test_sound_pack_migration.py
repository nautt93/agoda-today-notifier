from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest

import app as desktop
from app import BookingNotifierApp
from booking_notifier import audio
from booking_notifier.audio import (
    SOURCE_SOUND_FILES,
    SOURCE_SOUND_KEYS,
    SOURCE_SOUND_PACK,
    SOURCE_SOUND_SHA256,
    TRIP_SOUND_PACK,
)
from booking_notifier.config import DEFAULT_CONFIG, ConfigStore


def test_fresh_install_without_configuration_installs_both_packs_and_all_four_sounds(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    assert not store.path.exists()
    installed = store.install_source_sound_pack(tmp_path / "sounds")
    assert installed == store.load()
    assert installed["source_sound_pack"] == SOURCE_SOUND_PACK
    assert installed["trip_sound_pack"] == TRIP_SOUND_PACK
    for source, filename in SOURCE_SOUND_FILES.items():
        path = tmp_path / "sounds" / filename
        assert installed[SOURCE_SOUND_KEYS[source]] == str(path)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == SOURCE_SOUND_SHA256[source]
        pcm = tmp_path / "sounds" / audio.SOURCE_PCM_FILES[source]
        assert hashlib.sha256(pcm.read_bytes()).hexdigest() == audio.SOURCE_PCM_SHA256[source]


def test_new_sound_pack_replaces_old_choices_once_without_changing_credentials(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.save({"email_address": "hotel@example.invalid", "password_encrypted": "unchanged-encrypted-secret",
                "agoda_sound_file": "old-agoda.wav", "expedia_sound_file": "old-expedia.wav",
                "sound_file": "old-common.mp3", "f92_port": "COM6", "poll_seconds": 90})
    previous = store.load()
    installed = store.install_source_sound_pack(tmp_path / "sounds")
    assert installed["source_sound_pack"] == SOURCE_SOUND_PACK
    assert installed["trip_sound_pack"] == TRIP_SOUND_PACK
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
    installed["trip_sound_file"] = str(tmp_path / "my-trip-custom.wav")
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
    assert store.load()["trip_sound_pack"] == ""


def test_pcm_pack_failure_does_not_commit_partial_mapping(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.json")
    store.save({"agoda_sound_file": "keep-agoda.wav", "traveloka_sound_file": "keep-traveloka.mp3"})
    original = store.path.read_bytes()
    monkeypatch.setattr(audio, "bundled_source_pcm", Mock(side_effect=OSError("missing PCM asset")))
    with pytest.raises(OSError, match="missing PCM asset"):
        store.install_source_sound_pack(tmp_path / "sounds")
    assert store.path.read_bytes() == original
    assert store.load()["source_sound_pack"] == ""
    assert store.load()["trip_sound_pack"] == ""


@pytest.mark.parametrize("explicit_trip", ["", "my-trip-custom.mp3"])
def test_upgrade_from_three_source_pack_defaults_only_trip_and_preserves_custom_choices(tmp_path, monkeypatch, explicit_trip):
    store = ConfigStore(tmp_path / "config.json")
    store.save({"source_sound_pack": SOURCE_SOUND_PACK,
                "agoda_sound_file": "custom-agoda.wav", "expedia_sound_file": "custom-expedia.mp3", "traveloka_sound_file": "custom-traveloka.wav",
                "trip_sound_file": explicit_trip, "sound_file": "custom-common.wav",
                "email_address": "hotel@example.invalid", "password_encrypted": "unchanged-encrypted-secret",
                "update_manifest_source": "https://example.invalid/custom-update.json", "f92_port": "COM8"})
    previous = store.load()
    mp3_install = Mock(wraps=audio.bundled_source_sound)
    pcm_install = Mock(wraps=audio.bundled_source_pcm)
    monkeypatch.setattr(audio, "bundled_source_sound", mp3_install)
    monkeypatch.setattr(audio, "bundled_source_pcm", pcm_install)

    installed = store.install_source_sound_pack(tmp_path / "sounds")
    assert SOURCE_SOUND_PACK == "hotel-mp3-v1"  # Never bump the old marker to add a fourth source.
    assert installed["source_sound_pack"] == previous["source_sound_pack"]
    assert installed["trip_sound_pack"] == TRIP_SOUND_PACK == "trip-mp3-v1"
    expected_trip = explicit_trip or str(tmp_path / "sounds" / "4-trip.mp3")
    assert installed["trip_sound_file"] == expected_trip
    for key in DEFAULT_CONFIG:
        if key not in {"trip_sound_file", "trip_sound_pack"}:
            assert installed[key] == previous[key]
    mp3_install.assert_called_once_with("Trip", tmp_path / "sounds")
    pcm_install.assert_called_once_with("Trip", tmp_path / "sounds")
    assert {path.name for path in (tmp_path / "sounds").iterdir()} == {"4-trip.mp3", "4-trip-pcm.wav"}
    assert hashlib.sha256((tmp_path / "sounds" / "4-trip.mp3").read_bytes()).hexdigest() == SOURCE_SOUND_SHA256["trip"]
    assert hashlib.sha256((tmp_path / "sounds" / "4-trip-pcm.wav").read_bytes()).hexdigest() == audio.SOURCE_PCM_SHA256["trip"]

    installed["trip_sound_file"] = "later-changed-trip.wav"
    store.save(installed)
    original = store.path.read_bytes()
    assert store.install_source_sound_pack(tmp_path / "sounds") == installed
    assert store.path.read_bytes() == original
    assert mp3_install.call_count == pcm_install.call_count == 1


def test_fresh_pack_installation_preserves_an_explicit_trip_override(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.save({"trip_sound_file": "my-own-trip-recording.wav"})
    installed = store.install_source_sound_pack(tmp_path / "sounds")
    assert installed["source_sound_pack"] == SOURCE_SOUND_PACK
    assert installed["trip_sound_pack"] == TRIP_SOUND_PACK
    assert installed["trip_sound_file"] == "my-own-trip-recording.wav"
    assert (tmp_path / "sounds" / SOURCE_SOUND_FILES["trip"]).is_file()
    assert (tmp_path / "sounds" / audio.SOURCE_PCM_FILES["trip"]).is_file()


@pytest.mark.parametrize("legacy_installed", [False, True])
@pytest.mark.parametrize("failed_helper", ["bundled_source_sound", "bundled_source_pcm"])
def test_trip_preparation_failure_commits_neither_marker_nor_partial_choices(tmp_path, monkeypatch, legacy_installed, failed_helper):
    store = ConfigStore(tmp_path / "config.json")
    store.save({"source_sound_pack": SOURCE_SOUND_PACK if legacy_installed else "",
                "agoda_sound_file": "keep-agoda.wav", "expedia_sound_file": "keep-expedia.mp3", "traveloka_sound_file": "keep-traveloka.wav",
                "trip_sound_file": "keep-trip.wav", "sound_file": "keep-common.wav",
                "email_address": "hotel@example.invalid", "password_encrypted": "unchanged-encrypted-secret"})
    original = store.path.read_bytes()
    original_installer = getattr(audio, failed_helper)

    def fail_trip(source, directory):
        if source.lower() == "trip":
            raise OSError("Trip asset unavailable")
        return original_installer(source, directory)

    monkeypatch.setattr(audio, failed_helper, fail_trip)
    with pytest.raises(OSError, match="Trip asset unavailable"):
        store.install_source_sound_pack(tmp_path / "sounds")
    assert store.path.read_bytes() == original
    assert store.load()["source_sound_pack"] == (SOURCE_SOUND_PACK if legacy_installed else "")
    assert store.load()["trip_sound_pack"] == ""


def test_saving_settings_preserves_both_pack_markers_and_all_four_sound_choices(tmp_path, monkeypatch):
    config = {**DEFAULT_CONFIG, "source_sound_pack": SOURCE_SOUND_PACK, "trip_sound_pack": TRIP_SOUND_PACK, "email_address": "test@example.invalid",
              "agoda_sound_file": "custom-a.mp3", "expedia_sound_file": "custom-e.wav", "traveloka_sound_file": "custom-t.mp3", "trip_sound_file": "custom-trip.wav"}
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.config = config
    variables = {"provider": "provider", "host": "imap_host", "port": "imap_port", "email": "email_address",
                 "password": "password_encrypted", "poll": "poll_seconds", "scan_days": "scan_days", "sound": "sound_file",
                 "quiet": "quiet_hours_enabled", "start_windows": "start_with_windows", "start_minimized": "start_minimized",
                 "f92_enabled": "f92_enabled", "f92_port": "f92_port", "f92_sound": "f92_sound_index",
                 "f92_builtin_sound": "f92_builtin_sound_enabled", "update_source": "update_manifest_source"}
    app.booking_com_enabled_var = Mock(get=Mock(return_value=True))
    app.booking_com_browser_var = Mock(get=Mock(return_value="auto"))
    for variable, key in variables.items():
        setattr(app, f"{variable}_var", Mock(get=Mock(return_value=str(config[key]) if key not in {
            "quiet_hours_enabled", "start_with_windows", "start_minimized", "f92_enabled", "f92_builtin_sound_enabled"} else config[key])))
    app.source_sound_vars = {source.title(): Mock(get=Mock(return_value=config[key])) for source, key in SOURCE_SOUND_KEYS.items()}
    monkeypatch.setattr(desktop, "protect_secret", lambda value: "unchanged-encrypted-secret")
    collected = app._collect_config()
    store = ConfigStore(tmp_path / "config.json")
    store.save(collected)
    assert collected["source_sound_pack"] == SOURCE_SOUND_PACK
    assert collected["trip_sound_pack"] == TRIP_SOUND_PACK
    assert store.install_source_sound_pack(tmp_path / "sounds") == store.load()
    assert all(store.load()[key] == config[key] for key in SOURCE_SOUND_KEYS.values())

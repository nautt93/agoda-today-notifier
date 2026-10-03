from __future__ import annotations

import sys
import wave
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app as desktop
from app import BookingNotifierApp
from booking_notifier.audio import SAMPLE_RATE, WindowsMciAudioPlayer, configured_sound_paths, default_source_sound
from booking_notifier.config import ConfigStore
from booking_notifier.models import BookingEvent


@pytest.mark.parametrize(("source", "expected"), [
    ("Agoda", ["agoda.mp3", "common.wav"]),
    ("Expedia", ["expedia.wav", "common.wav"]),
    (" agoda ", ["agoda.mp3", "common.wav"]),
    ("Unknown", ["common.wav"]),
])
def test_sound_paths_only_use_matching_source_then_legacy(source, expected):
    config = {"agoda_sound_file": "agoda.mp3", "expedia_sound_file": "expedia.wav", "sound_file": "common.wav"}
    assert configured_sound_paths(config, source) == [Path(value) for value in expected]
    assert configured_sound_paths({"agoda_sound_file": "same.wav", "sound_file": "same.wav"}, "Agoda") == [Path("same.wav")]


def test_migration_and_config_round_trip_preserve_old_and_new_files(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.save({"sound_file": "old.wav", "email_address": "hotel@example.com", "password_encrypted": "unchanged"})
    config = store.load()
    assert config["agoda_sound_file"] == config["expedia_sound_file"] == ""
    assert config["sound_file"] == "old.wav"
    config.update(agoda_sound_file="agoda.mp3", expedia_sound_file="expedia.wav")
    store.save(config)
    restored = store.load()
    assert restored == config
    assert restored["password_encrypted"] == "unchanged"


def test_built_in_chimes_are_distinct_valid_wav_and_reused(tmp_path):
    agoda = default_source_sound("Agoda", tmp_path)
    expedia = default_source_sound("Expedia", tmp_path)
    assert agoda != expedia and agoda.read_bytes() != expedia.read_bytes()
    for path in (agoda, expedia):
        with wave.open(str(path), "rb") as sound:
            assert sound.getnchannels() == 1 and sound.getsampwidth() == 2
            assert sound.getframerate() == SAMPLE_RATE and sound.getnframes() == SAMPLE_RATE * 3
            assert any(sound.readframes(sound.getnframes()))
    old_timestamp = agoda.stat().st_mtime_ns
    assert default_source_sound("Agoda", tmp_path) == agoda and agoda.stat().st_mtime_ns == old_timestamp
    agoda.write_bytes(b"broken file")
    assert default_source_sound("Agoda", tmp_path).read_bytes().startswith(b"RIFF")
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError):
        default_source_sound("untrusted/../name", tmp_path)


def audio_app(tmp_path, monkeypatch):
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.root = Mock()
    app.root.after.return_value = "timer-1"
    app.config = {}
    app.active_alert = None
    app.sound_active = False
    app.sound_uses_file = False
    app.sound_repeat_job = None
    app.sound_preview_job = None
    app.mp3_player = Mock()
    app.log = Mock()
    app.settings_window = Mock()
    monkeypatch.setattr(desktop, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    winsound = SimpleNamespace(PlaySound=Mock(), MessageBeep=Mock(), SND_FILENAME=1, SND_ASYNC=2, SND_LOOP=4, SND_NODEFAULT=8, MB_ICONEXCLAMATION=16)
    monkeypatch.setitem(sys.modules, "winsound", winsound)
    return app, winsound


@pytest.mark.parametrize(("source", "extension"), [("Agoda", ".mp3"), ("Expedia", ".wav")])
def test_booking_sound_routes_by_active_alert_and_stops_previous_timer(tmp_path, monkeypatch, source, extension):
    app, winsound = audio_app(tmp_path, monkeypatch)
    selected = tmp_path / (source + extension)
    selected.write_bytes(b"audio")
    app.config = {f"{source.lower()}_sound_file": str(selected), "sound_file": "old.wav"}
    app.active_alert = BookingEvent(source=source, booking_id="12345", checkin_date=date.today())
    app.sound_repeat_job = "old-beep"
    app.sound_preview_job = "old-preview"
    app.play_sound()
    assert app.sound_active and app.sound_uses_file
    assert app.sound_repeat_job is app.sound_preview_job is None
    assert app.root.after_cancel.call_args_list == [(('old-beep',),), (('old-preview',),)]
    if extension == ".mp3":
        app.mp3_player.play_loop.assert_called_once_with(selected)
    else:
        assert winsound.PlaySound.call_args.args == (str(selected), 15)
    winsound.MessageBeep.assert_not_called()
    app.root.after.assert_not_called()  # File loops do not schedule extra beep chains.
    app.stop_sound()
    assert not app.sound_active and winsound.PlaySound.call_args.args == (None, 0)


def test_missing_source_file_falls_back_to_legacy_not_other_provider(tmp_path, monkeypatch):
    app, winsound = audio_app(tmp_path, monkeypatch)
    legacy = default_source_sound("Expedia", tmp_path)
    app._start_source_sound("Agoda", {"agoda_sound_file": "missing.mp3", "expedia_sound_file": "other.wav", "sound_file": str(legacy)})
    assert app.sound_uses_file and winsound.PlaySound.call_args.args[0] == str(legacy)
    app.log.assert_called_once()


def test_corrupt_source_audio_falls_back_to_builtin(tmp_path, monkeypatch):
    app, winsound = audio_app(tmp_path, monkeypatch)
    selected = tmp_path / "corrupt.mp3"
    selected.write_bytes(b"not mp3")
    app.mp3_player.play_loop.side_effect = RuntimeError("bad codec")
    app._start_source_sound("Agoda", {"agoda_sound_file": str(selected)})
    assert app.sound_active and app.sound_uses_file
    assert Path(winsound.PlaySound.call_args.args[0]).name == "agoda-chime-v1.wav"
    winsound.MessageBeep.assert_not_called()


def test_unavailable_audio_device_keeps_fallback_timer_single_and_cancellable(tmp_path, monkeypatch):
    app, winsound = audio_app(tmp_path, monkeypatch)
    winsound.PlaySound.side_effect = RuntimeError("device unavailable")
    app._start_source_sound("Expedia", {})
    assert app.sound_active and not app.sound_uses_file and app.sound_repeat_job == "timer-1"
    winsound.MessageBeep.assert_called_once()
    app.stop_sound()
    app.root.after_cancel.assert_called_once_with("timer-1")
    assert not app.sound_active and app.sound_repeat_job is None


def test_preview_draft_settings_timeout_and_real_booking_takeover(tmp_path, monkeypatch):
    app, _ = audio_app(tmp_path, monkeypatch)
    app.sound_var = Mock(get=Mock(return_value=""))
    app.source_sound_vars = {source: Mock(get=Mock(return_value="")) for source in ("Agoda", "Expedia")}
    app.preview_source_sound("Expedia")
    assert app.config == {}  # Preview does not save draft settings.
    assert app.sound_preview_job == "timer-1"
    assert app.root.after.call_args.args == (4000, app.stop_sound_preview)
    app.active_alert = BookingEvent(source="Agoda", booking_id="12345", checkin_date=date.today())
    app.play_sound()
    app.root.after_cancel.assert_called_once_with("timer-1")
    app.stop_sound_preview()  # The preview Stop button cannot silence a booking.
    assert app.sound_active
    app._start_source_sound = Mock()
    monkeypatch.setattr(desktop.messagebox, "showinfo", Mock())
    app.preview_source_sound("Expedia")
    app._start_source_sound.assert_not_called()
    desktop.messagebox.showinfo.assert_called_once()


def test_mp3_failure_closes_mci_alias(tmp_path, monkeypatch):
    player = WindowsMciAudioPlayer()
    send = Mock(side_effect=[None, None, RuntimeError("play failed"), None])
    monkeypatch.setattr(player, "_send", send)
    with pytest.raises(RuntimeError, match="play failed"):
        player.play_loop(tmp_path / "with space.mp3")
    assert send.call_args.args == (f"close {player.ALIAS}",)

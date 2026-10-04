from __future__ import annotations

import hashlib
import json
import runpy
import shutil
import sys
import wave
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app as desktop
from app import BookingNotifierApp
from booking_notifier.audio import (
    SAMPLE_RATE,
    SOURCE_SOUND_FILES,
    SOURCE_SOUND_KEYS,
    SOURCE_SOUND_SHA256,
    WindowsMciAudioPlayer,
    bundled_source_sound,
    configured_sound_paths,
    default_source_sound,
)
from booking_notifier.config import ConfigStore
from booking_notifier.models import BookingEvent


@pytest.mark.parametrize(("source", "expected"), [
    ("Agoda", ["agoda.mp3", "common.wav"]),
    ("Expedia", ["expedia.wav", "common.wav"]),
    ("Traveloka", ["traveloka.mp3", "common.wav"]),
    (" agoda ", ["agoda.mp3", "common.wav"]),
    ("Unknown", ["common.wav"]),
])
def test_sound_paths_only_use_matching_source_then_legacy(source, expected):
    config = {"agoda_sound_file": "agoda.mp3", "expedia_sound_file": "expedia.wav", "traveloka_sound_file": "traveloka.mp3", "sound_file": "common.wav"}
    assert configured_sound_paths(config, source) == [Path(value) for value in expected]
    assert configured_sound_paths({"agoda_sound_file": "same.wav", "sound_file": "same.wav"}, "Agoda") == [Path("same.wav")]


def test_migration_and_config_round_trip_preserve_old_and_new_files(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.save({"sound_file": "old.wav", "email_address": "hotel@example.com", "password_encrypted": "unchanged"})
    config = store.load()
    assert config["agoda_sound_file"] == config["expedia_sound_file"] == config["traveloka_sound_file"] == ""
    assert config["sound_file"] == "old.wav"
    config.update(agoda_sound_file="agoda.mp3", expedia_sound_file="expedia.wav", traveloka_sound_file="traveloka.mp3")
    store.save(config)
    restored = store.load()
    assert restored == config
    assert restored["password_encrypted"] == "unchanged"


def test_built_in_chimes_are_distinct_valid_wav_and_reused(tmp_path):
    agoda = default_source_sound("Agoda", tmp_path)
    expedia = default_source_sound("Expedia", tmp_path)
    traveloka = default_source_sound("Traveloka", tmp_path)
    assert len({agoda, expedia, traveloka}) == len({path.read_bytes() for path in (agoda, expedia, traveloka)}) == 3
    for path in (agoda, expedia, traveloka):
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


def test_original_bundled_sound_assets_match_recorded_metadata():
    asset_dir = Path(__file__).resolve().parent.parent / "assets" / "sounds"
    metadata = json.loads((asset_dir / "manifest.json").read_text(encoding="utf-8"))
    assert set(SOURCE_SOUND_FILES) == set(SOURCE_SOUND_KEYS) == set(SOURCE_SOUND_SHA256) == {"agoda", "expedia", "traveloka"}
    assert metadata["audio_format"] == "MPEG Layer III"
    assert metadata["channels"] == 1 and metadata["sample_rate_hz"] == 44100
    for source, entry in metadata["sounds"].items():
        key = source.lower()
        data = (asset_dir / SOURCE_SOUND_FILES[key]).read_bytes()
        assert entry["filename"] == SOURCE_SOUND_FILES[key]
        assert len(data) == entry["bytes"]
        assert hashlib.sha256(data).hexdigest() == entry["sha256"] == SOURCE_SOUND_SHA256[key]
        assert 2.5 <= entry["duration_seconds"] <= 2.8


def test_pyinstaller_spec_bundles_all_exact_source_mp3_files():
    project_dir = Path(__file__).resolve().parent.parent
    analysis = Mock(return_value=SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[]))
    runpy.run_path(str(project_dir / "BookingNotifier.spec"), init_globals={
        "SPECPATH": str(project_dir), "Analysis": analysis, "PYZ": Mock(), "EXE": Mock(),
    })
    datas = analysis.call_args.kwargs["datas"]
    assert {Path(source).name for source, destination in datas} == {*SOURCE_SOUND_FILES.values(), "manifest.json"}
    for source, destination in datas:
        assert Path(source).parent == project_dir / "assets" / "sounds"
        assert Path(source).is_file() and destination == "assets/sounds"


@pytest.mark.parametrize("source", ["Agoda", " Expedia ", "Traveloka"])
def test_bundled_source_sound_preserves_bytes_repairs_damage_and_reuses_stable_file(tmp_path, source):
    key = source.strip().lower()
    directory = tmp_path / "writable app data" / "sounds"
    path = bundled_source_sound(source, directory)
    assert path == directory / SOURCE_SOUND_FILES[key]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SOURCE_SOUND_SHA256[key]
    timestamp = path.stat().st_mtime_ns
    assert bundled_source_sound(source, directory) == path
    assert path.stat().st_mtime_ns == timestamp
    path.write_bytes(b"corrupt old audio")
    assert bundled_source_sound(source, directory) == path
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SOURCE_SOUND_SHA256[key]
    assert not list(directory.glob("*.tmp"))


def test_frozen_resource_resolution_uses_meipass_and_never_cwd(tmp_path, monkeypatch):
    asset_dir = Path(__file__).resolve().parent.parent / "assets" / "sounds"
    frozen_dir = tmp_path / "onefile extraction" / "assets" / "sounds"
    frozen_dir.mkdir(parents=True)
    for filename in SOURCE_SOUND_FILES.values():
        shutil.copyfile(asset_dir / filename, frozen_dir / filename)
    monkeypatch.setattr(sys, "_MEIPASS", str(frozen_dir.parent.parent), raising=False)
    work_dir = tmp_path / "unrelated working directory"
    work_dir.mkdir()
    monkeypatch.chdir(work_dir)
    for source, filename in SOURCE_SOUND_FILES.items():
        path = bundled_source_sound(source, tmp_path / "persistent sounds")
        assert path.name == filename
        assert path.read_bytes() == (frozen_dir / filename).read_bytes()
        assert path.parent != frozen_dir


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_invalid_packaged_asset_is_rejected_without_overwriting_existing_file(tmp_path, monkeypatch, damage):
    frozen_root = tmp_path / "frozen"
    frozen_dir = frozen_root / "assets" / "sounds"
    frozen_dir.mkdir(parents=True)
    if damage == "corrupt":
        (frozen_dir / "3-traveloka.mp3").write_bytes(b"not the supplied MP3")
    monkeypatch.setattr(sys, "_MEIPASS", str(frozen_root), raising=False)
    destination = tmp_path / "sounds"
    destination.mkdir()
    preserved = destination / "3-traveloka.mp3"
    preserved.write_bytes(b"previous destination remains untouched")
    with pytest.raises((FileNotFoundError, ValueError)):
        bundled_source_sound("Traveloka", destination)
    assert preserved.read_bytes() == b"previous destination remains untouched"
    assert not list(destination.glob("*.tmp"))


def test_bundled_copy_failure_is_atomic_and_cleans_temporary_file(tmp_path, monkeypatch):
    directory = tmp_path / "sounds"
    directory.mkdir()
    preserved = directory / "1-agoda.mp3"
    preserved.write_bytes(b"old file")
    monkeypatch.setattr("booking_notifier.audio.os.replace", Mock(side_effect=PermissionError("file in use")))
    with pytest.raises(PermissionError, match="file in use"):
        bundled_source_sound("Agoda", directory)
    assert preserved.read_bytes() == b"old file"
    assert not list(directory.glob("*.tmp"))
    with pytest.raises(ValueError):
        bundled_source_sound("../../untrusted", directory)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows MCI decoder required")
@pytest.mark.parametrize("source", ["Agoda", "Expedia", "Traveloka"])
def test_supplied_mp3_decodes_with_real_windows_mci_without_playback(tmp_path, source):
    import ctypes
    from ctypes import wintypes

    path = bundled_source_sound(source, tmp_path / "sounds")
    player = WindowsMciAudioPlayer()
    alias = "bookingdesk_codec_test"
    opened = False
    try:
        player._send(f'open "{path}" type mpegvideo alias {alias}')
        opened = True
        player._send(f"set {alias} time format milliseconds")
        output = ctypes.create_unicode_buffer(128)
        winmm = ctypes.WinDLL("winmm")
        winmm.mciSendStringW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HWND)
        winmm.mciSendStringW.restype = wintypes.DWORD
        assert winmm.mciSendStringW(f"status {alias} length", output, len(output), None) == 0
        assert 2500 <= int(output.value) <= 2850
    finally:
        if opened:
            player._send(f"close {alias}")


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


@pytest.mark.parametrize(("source", "extension"), [("Agoda", ".mp3"), ("Expedia", ".wav"), ("Traveloka", ".mp3")])
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


def test_missing_source_file_uses_own_bundled_mp3_before_legacy_or_other_provider(tmp_path, monkeypatch):
    app, winsound = audio_app(tmp_path, monkeypatch)
    legacy = default_source_sound("Expedia", tmp_path)
    app._start_source_sound("Agoda", {"agoda_sound_file": "missing.mp3", "expedia_sound_file": "other.wav", "sound_file": str(legacy)})
    assert app.sound_uses_file
    app.mp3_player.play_loop.assert_called_once_with(tmp_path / "sounds" / "1-agoda.mp3")
    assert winsound.PlaySound.call_args.args == (None, 0)
    app.log.assert_called_once()


@pytest.mark.parametrize("source", ["Agoda", "Expedia", "Traveloka"])
def test_without_source_configuration_uses_exact_supplied_mp3_before_common_audio(tmp_path, monkeypatch, source):
    app, winsound = audio_app(tmp_path, monkeypatch)
    legacy = default_source_sound("Agoda", tmp_path / "legacy")
    app._start_source_sound(source, {"sound_file": str(legacy)})
    assert app.sound_active and app.sound_uses_file
    path = tmp_path / "sounds" / SOURCE_SOUND_FILES[source.lower()]
    app.mp3_player.play_loop.assert_called_once_with(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SOURCE_SOUND_SHA256[source.lower()]
    assert winsound.PlaySound.call_args.args == (None, 0)
    winsound.MessageBeep.assert_not_called()
    app.root.after.assert_not_called()


def test_missing_bundled_asset_uses_legacy_not_other_provider(tmp_path, monkeypatch):
    app, winsound = audio_app(tmp_path, monkeypatch)
    legacy = default_source_sound("Expedia", tmp_path)
    monkeypatch.setattr(desktop, "bundled_source_sound", Mock(side_effect=FileNotFoundError("no bundled audio")))
    app._start_source_sound("Traveloka", {"traveloka_sound_file": "missing.mp3", "agoda_sound_file": "other.wav", "sound_file": str(legacy)})
    assert app.sound_uses_file and winsound.PlaySound.call_args.args[0] == str(legacy)
    app.mp3_player.play_loop.assert_not_called()


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
    app.mp3_player.play_loop.side_effect = RuntimeError("device unavailable")
    app._start_source_sound("Expedia", {})
    assert app.sound_active and not app.sound_uses_file and app.sound_repeat_job == "timer-1"
    winsound.MessageBeep.assert_called_once()
    app.stop_sound()
    app.root.after_cancel.assert_called_once_with("timer-1")
    assert not app.sound_active and app.sound_repeat_job is None


def test_preview_draft_settings_timeout_and_real_booking_takeover(tmp_path, monkeypatch):
    app, _ = audio_app(tmp_path, monkeypatch)
    app.sound_var = Mock(get=Mock(return_value=""))
    app.source_sound_vars = {source: Mock(get=Mock(return_value="")) for source in ("Agoda", "Expedia", "Traveloka")}
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

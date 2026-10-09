"""Exercise frozen handoff -> replacement -> real app UI startup on Windows."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from datetime import date, timedelta
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_synthetic_wav(path: Path, frequency: int) -> None:
    """Create a short test-only sound; never copy or alter a real user's files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 22050
    frames = b"".join(struct.pack("<h", int(600 * math.sin(2 * math.pi * frequency * index / rate)))
                      for index in range(rate // 4))
    with wave.open(str(path), "wb") as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(rate)
        sound.writeframes(frames)


def prepare_profile(config_dir: Path, marked: bool, current: bool = False) -> tuple[dict, dict, dict[Path, str], str]:
    config_dir.mkdir(parents=True)
    config = {
        "f92_enabled": False, "start_with_windows": False, "quiet_hours_enabled": False,
        "email_address": "", "update_manifest_source": "",
        "booking_com_enrichment": False,
    }
    pending = {
        "checkin_date": date.today().isoformat(),
        "checkout_date": (date.today() + timedelta(days=1)).isoformat(),
        "guest_name": "Test Legacy Guest", "room_type": "Deluxe x1",
        "subject": "Agoda test booking confirmation",
    }  # The legacy scenario intentionally omits source and booking_id.
    popup_message = "POPUP Agoda (không có mã)"
    custom_hashes: dict[Path, str] = {}
    if marked:
        config.update({
            "source_sound_pack": "hotel-mp3-v1", "trip_sound_pack": "", "trip_sound_file": "",
            "email_address": "ota-smoke@example.invalid", "password_encrypted": "synthetic-not-a-real-credential",
            "imap_host": "", "poll_seconds": 90, "scan_days": 90, "f92_port": "COM8",
        })  # No host means the synthetic credentials never trigger a network connection.
        sources = ("agoda", "expedia", "traveloka", "common", "trip") if current else ("agoda", "expedia", "traveloka", "common")
        for source, frequency in zip(sources, (220, 330, 440, 550, 660)[:len(sources)], strict=True):
            sound = config_dir / "custom sounds" / f"existing-{source}.wav"
            create_synthetic_wav(sound, frequency)
            config["sound_file" if source == "common" else f"{source}_sound_file"] = str(sound)
            custom_hashes[sound] = digest(sound)
        assert len(set(custom_hashes.values())) == len(sources)
        pending.update({"source": "Agoda", "booking_id": "TEST-OTA-1718", "guest_name": "Test Upgraded Guest"})
        popup_message = "POPUP Agoda TEST-OTA-1718"
        if current:
            config.update({"trip_sound_pack": "trip-mp3-v1", "booking_com_browser": "chrome", "booking_com_sound_file": ""})
            profile = config_dir / "booking-com-browser" / "synthetic-profile-marker"
            profile.parent.mkdir()
            profile.write_bytes(b"synthetic-only; no real cookie or credential")
            custom_hashes[profile] = digest(profile)
            pending.update({"source": "Booking.com", "booking_id": "TEST-OTA-1720"})
            popup_message = "POPUP Booking.com TEST-OTA-1720"
    state = {
        "schema": 5, "pending_alerts": [pending],
        "history": [{"guest_name": "Test Old Guest", "checkin_date": "30/09/2026", "custom": "preserve"}],
        "mailbox_reads": {"incremental:test:123": {
            "cursor": 110, "pending_uids": list(range(101, 111)), "parser_version": "p12" if marked else "p10",
        }},
    }
    (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (config_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return config, state, custom_hashes, popup_message


def verify_source_recordings(project: Path, config_dir: Path, installed_config: dict, sources: tuple[str, ...]) -> None:
    manifest = json.loads((project / "assets" / "sounds" / "manifest.json").read_text(encoding="utf-8"))
    hashes = set()
    for source in sources:
        metadata = manifest["sounds"]["Booking.com" if source == "booking.com" else source.title()]
        copied = config_dir / "sounds" / metadata["filename"]
        key = "booking_com_sound_file" if source == "booking.com" else f"{source}_sound_file"
        assert installed_config[key] == str(copied), f"Incorrect {source} default mapping"
        assert digest(copied) == digest(project / "assets" / "sounds" / metadata["filename"]) == metadata["sha256"], f"Incorrect {source} MP3 bytes"
        hashes.add(digest(copied))
        pcm = config_dir / "sounds" / metadata["pcm_filename"]
        assert digest(pcm) == digest(project / "assets" / "sounds" / metadata["pcm_filename"]) == metadata["pcm_sha256"], f"Incorrect {source} PCM bytes"
        with wave.open(str(pcm), "rb") as sound:
            assert sound.getnchannels() == 1 and sound.getsampwidth() == 2
            assert sound.getframerate() == 44100
            assert sound.getnframes() == metadata["pcm_sample_frames"], f"Incorrect {source} PCM frame count"
    assert len(hashes) == len(sources), "Supplied source files must be distinct"


def exercise_upgrade(project: Path, asset: Path, expected: str, launcher: Path, scratch: Path, marked: bool, current: bool = False) -> None:
    label = "marked-1.7.20" if current else "marked-1.7.18" if marked else "legacy-1.5.x"
    scenario = scratch / label
    target = scenario / "dist" / f"OTA-Smoke-{scratch.name}-{label}.exe"
    target.parent.mkdir(parents=True)
    shutil.copyfile(launcher, target)
    appdata = scenario / "profile"
    config_dir = appdata / "AgodaTodayNotifier"
    original_config, original_state, custom_hashes, popup_message = prepare_profile(config_dir, marked, current)
    process = None
    print(f"RUN: frozen OTA scenario {label}", flush=True)
    try:
        deadline = time.monotonic() + 90
        process = subprocess.Popen([str(target), str(asset), expected], env={
            **os.environ, "APPDATA": str(appdata), "LOCALAPPDATA": str(appdata),
            "PYINSTALLER_RESET_ENVIRONMENT": "1",
        })
        assert process.wait(timeout=max(0.1, deadline - time.monotonic())) == 0, f"{label}: old app did not hand off successfully"
        while time.monotonic() < deadline:
            try:
                installed = digest(target) == expected
            except OSError:
                installed = False
            if (installed and not target.with_suffix(".exe.previous").exists()
                    and not list(target.parent.glob("*.update.tmp"))
                    and not list(target.parent.glob("*.update.started"))):
                assert not (config_dir / "update-error.log").exists(), f"{label}: updater error log exists"
                logs = list(config_dir.glob("*.log"))
                log_text = "\n".join(log.read_text(encoding="utf-8") for log in logs)
                if "Windows system tray ready" not in log_text:
                    time.sleep(0.25)
                    continue
                assert "missing 1 required positional argument" not in log_text, f"{label}: booking compatibility error"
                assert popup_message in log_text, f"{label}: pending popup did not restore"
                saved = json.loads((config_dir / "state.json").read_text(encoding="utf-8"))
                for key in ("history", "pending_alerts", "mailbox_reads"):
                    assert saved[key] == original_state[key], f"{label}: changed {key}"
                sounds = [config_dir / "sounds" / f"{source}-chime-v1.wav" for source in ("agoda", "expedia")]
                assert sounds[0].read_bytes() != sounds[1].read_bytes(), f"{label}: fallback sounds must differ"
                for sound_file in sounds:
                    with wave.open(str(sound_file), "rb") as sound:
                        assert sound.getnframes() == sound.getframerate() * 3
                installed_config = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
                assert installed_config["source_sound_pack"] == "hotel-mp3-v1", f"{label}: legacy pack marker changed"
                assert installed_config["trip_sound_pack"] == "trip-mp3-v1", f"{label}: Trip pack marker missing"
                assert installed_config["booking_com_sound_pack"] == "booking-com-mp3-v1", f"{label}: Booking.com pack marker missing"
                if marked:
                    retired = {"booking_com_enrichment", "booking_com_enrichment_mode", "booking_com_browser"}
                    assert retired.isdisjoint(installed_config), f"{label}: retired browser settings returned"
                    changed = {"booking_com_sound_file", "booking_com_sound_pack", *retired}
                    if not current:
                        changed.update({"trip_sound_pack", "trip_sound_file"})
                    for key, value in original_config.items():
                        if key not in changed:
                            assert installed_config[key] == value, f"{label}: existing {key} was overwritten"
                    for path, original_hash in custom_hashes.items():
                        assert digest(path) == original_hash, f"{label}: existing custom sound bytes changed"
                    verify_source_recordings(project, config_dir, installed_config, ("booking.com",) if current else ("trip", "booking.com"))
                    print(f"PASS: {label} frozen OTA preserves existing source choices, common sound bytes, browser profile and synthetic credentials; only new defaults added.", flush=True)
                else:
                    verify_source_recordings(project, config_dir, installed_config, ("agoda", "expedia", "traveloka", "trip", "booking.com"))
                    print("PASS: legacy frozen OTA installs all five exact MP3/PCM recordings, mappings and manifest frame counts.", flush=True)
                print(f"PASS: {label} frozen handoff, EXE replacement, real Tk UI and tray startup; all pack markers persisted and updater markers cleaned.", flush=True)
                print(f"PASS: {label} pending popup, history and ten unfinished email UIDs preserved.", flush=True)
                return
            time.sleep(0.25)
        error_log = config_dir / "update-error.log"
        details = error_log.read_text(encoding="utf-8") if error_log.exists() else "No updater error log"
        raise AssertionError(f"{label}: packaged OTA did not complete within 90 seconds: {details}")
    finally:
        if process and process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False, timeout=15)
        # Only our uniquely named scratch EXE, never a user's running app. Stop
        # each scenario before the next acquires the shared single-instance lock.
        subprocess.run(["taskkill", "/IM", target.name, "/T", "/F"], check=False, timeout=15)
        time.sleep(1)


def main() -> None:
    if os.name != "nt":
        raise RuntimeError("This integration check requires Windows.")
    project = Path(__file__).resolve().parents[1]
    asset = Path(sys.argv[1]).resolve()
    from PyInstaller.archive.readers import CArchiveReader

    package = CArchiveReader(str(asset))
    names = list(package.toc)
    for name, entry in package.toc.items():
        if entry[-1] == "z":
            names.extend(package.open_embedded_archive(name).toc)
    assert not any("playwright" in name.lower() or name.lower().endswith("node.exe")
                   or name in {"booking_notifier.booking_com_browser", "booking_notifier.browser_windows",
                               "booking_notifier.popup_state"} for name in names), "Retired browser machinery in EXE"
    print("PASS: packaged EXE contains no Booking.com browser, Playwright or Node driver.", flush=True)
    expected = digest(asset)
    with tempfile.TemporaryDirectory(prefix="booking-ota-smoke-") as directory:
        scratch = Path(directory)
        name = "OTA-Smoke-Launcher-" + scratch.name
        launcher = scratch / "launcher-dist" / f"{name}.exe"
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--noconsole",
            "--name", name, "--paths", str(project), "--distpath", str(launcher.parent),
            "--workpath", str(scratch / "build"), "--specpath", str(scratch),
            str(project / "tests" / "fixtures" / "ota_launcher.py"),
        ], check=True, timeout=240)
        for marked, current in ((False, False), (True, False), (True, True)):
            exercise_upgrade(project, asset, expected, launcher, scratch, marked, current)
        print("PASS: all three frozen OTA profile scenarios completed successfully.", flush=True)


if __name__ == "__main__":
    main()

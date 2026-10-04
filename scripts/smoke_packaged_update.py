"""Exercise frozen handoff -> replacement -> real app UI startup on Windows."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from datetime import date, timedelta
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    if os.name != "nt":
        raise RuntimeError("This integration check requires Windows.")
    project = Path(__file__).resolve().parents[1]
    asset = Path(sys.argv[1]).resolve()
    expected = digest(asset)
    with tempfile.TemporaryDirectory(prefix="booking-ota-smoke-") as directory:
        scratch = Path(directory)
        name = "OTA-Smoke-" + scratch.name
        target = scratch / "dist" / f"{name}.exe"
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--noconsole",
            "--name", name, "--paths", str(project), "--distpath", str(target.parent),
            "--workpath", str(scratch / "build"), "--specpath", str(scratch),
            str(project / "tests" / "fixtures" / "ota_launcher.py"),
        ], check=True, timeout=240)
        appdata = scratch / "profile"
        config_dir = appdata / "AgodaTodayNotifier"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text(json.dumps({
            "f92_enabled": False, "start_with_windows": False, "quiet_hours_enabled": False,
            "email_address": "", "update_manifest_source": "",
        }), encoding="utf-8")
        # Exercise an existing 1.5.x-style profile through the real frozen upgrade.
        legacy_pending = {
            "checkin_date": date.today().isoformat(),
            "checkout_date": (date.today() + timedelta(days=1)).isoformat(),
            "guest_name": "Test Legacy Guest", "room_type": "Deluxe x1",
            "subject": "Agoda test booking confirmation",
        }  # Intentionally no source or booking_id.
        legacy_history = {"guest_name": "Test Old Guest", "checkin_date": "30/09/2026", "custom": "preserve"}
        mailbox_record = {"cursor": 110, "pending_uids": list(range(101, 111)), "parser_version": "p10"}
        (config_dir / "state.json").write_text(json.dumps({
            "schema": 5, "pending_alerts": [legacy_pending], "history": [legacy_history],
            "mailbox_reads": {"incremental:test:123": mailbox_record},
        }), encoding="utf-8")
        process = None
        try:
            process = subprocess.Popen([str(target), str(asset), expected], env={
                **os.environ, "APPDATA": str(appdata), "LOCALAPPDATA": str(appdata),
                "PYINSTALLER_RESET_ENVIRONMENT": "1",
            })
            assert process.wait(timeout=90) == 0, "Old app did not hand off successfully"
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    installed = digest(target) == expected
                except OSError:
                    installed = False
                if (installed and not target.with_suffix(".exe.previous").exists()
                        and not list(target.parent.glob("*.update.tmp"))
                        and not list(target.parent.glob("*.update.started"))):
                    assert not (config_dir / "update-error.log").exists()
                    logs = list(config_dir.glob("*.log"))
                    if not any("Windows system tray ready" in log.read_text(encoding="utf-8") for log in logs):
                        time.sleep(0.25)
                        continue
                    log_text = "\n".join(log.read_text(encoding="utf-8") for log in logs)
                    assert "missing 1 required positional argument" not in log_text
                    assert "POPUP Agoda (không có mã)" in log_text, "Legacy ID-less pending popup did not restore"
                    saved = json.loads((config_dir / "state.json").read_text(encoding="utf-8"))
                    assert saved["history"] == [legacy_history] and saved["pending_alerts"] == [legacy_pending]
                    assert saved["mailbox_reads"]["incremental:test:123"] == mailbox_record
                    sounds = [config_dir / "sounds" / f"{source}-chime-v1.wav" for source in ("agoda", "expedia")]
                    assert sounds[0].read_bytes() != sounds[1].read_bytes(), "Provider sounds must differ"
                    for sound_file in sounds:
                        with wave.open(str(sound_file), "rb") as sound:
                            assert sound.getnframes() == sound.getframerate() * 3
                    print("PASS: frozen updater handoff, EXE replacement, real Tk UI startup confirmed.", flush=True)
                    print("PASS: packaged Windows tray backend starts successfully.", flush=True)
                    print("PASS: packaged app creates two distinct provider WAV files in user data.", flush=True)
                    print("PASS: legacy missing-ID popup restores; history and ten unfinished email UIDs preserved.", flush=True)
                    return
                time.sleep(0.25)
            error_log = config_dir / "update-error.log"
            details = error_log.read_text(encoding="utf-8") if error_log.exists() else "No updater error log"
            raise AssertionError(f"Packaged OTA did not complete: {details}")
        finally:
            if process and process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False)
            # Only our unique smoke-test executable, never a user's running app.
            subprocess.run(["taskkill", "/IM", target.name, "/T", "/F"], check=False)
            time.sleep(1)


if __name__ == "__main__":
    main()

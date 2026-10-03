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
            "f92_enabled": False, "start_with_windows": False,
            "email_address": "", "update_manifest_source": "",
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
                    sounds = [config_dir / "sounds" / f"{source}-chime-v1.wav" for source in ("agoda", "expedia")]
                    assert sounds[0].read_bytes() != sounds[1].read_bytes(), "Provider sounds must differ"
                    for sound_file in sounds:
                        with wave.open(str(sound_file), "rb") as sound:
                            assert sound.getnframes() == sound.getframerate() * 3
                    print("PASS: frozen updater handoff, EXE replacement, real Tk UI startup confirmed.", flush=True)
                    print("PASS: packaged Windows tray backend starts successfully.", flush=True)
                    print("PASS: packaged app creates two distinct provider WAV files in user data.", flush=True)
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

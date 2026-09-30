from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from booking_notifier import ota_update


def signed_manifest(monkeypatch):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(ota_update, "UPDATE_PUBLIC_KEY_B64", base64.b64encode(public).decode())
    value = {
        "schema": 1,
        "version": "1.6.1",
        "url": "https://github.com/nautt93/agoda-today-notifier/releases/download/v1.6.1/app.exe",
        "sha256": "a" * 64,
        "size": 1234,
        "notes": "Safe release",
    }
    value["signature"] = base64.b64encode(private.sign(ota_update._canonical_manifest(value))).decode()
    return value


def test_signed_web_manifest_is_accepted(monkeypatch):
    value = signed_manifest(monkeypatch)
    monkeypatch.setattr(ota_update, "_read_source", lambda *_: json.dumps(value).encode())
    info = ota_update.load_update_manifest("https://example.com/update.json")
    assert info.version == "1.6.1"


def test_unsigned_web_manifest_is_rejected(monkeypatch):
    value = signed_manifest(monkeypatch)
    value.pop("signature")
    monkeypatch.setattr(ota_update, "_read_source", lambda *_: json.dumps(value).encode())
    with pytest.raises(ValueError, match="chưa được ký"):
        ota_update.load_update_manifest("https://example.com/update.json")


def test_tampered_manifest_is_rejected(monkeypatch):
    value = signed_manifest(monkeypatch)
    value["url"] = "https://evil.example/app.exe"
    monkeypatch.setattr(ota_update, "_read_source", lambda *_: json.dumps(value).encode())
    with pytest.raises(ValueError, match="không hợp lệ"):
        ota_update.load_update_manifest("https://example.com/update.json")


def test_failed_launch_rolls_back_old_executable(tmp_path, monkeypatch):
    source = tmp_path / "new.exe"
    target = tmp_path / "app.exe"
    source.write_bytes(b"MZnew-version")
    target.write_bytes(b"MZold-version")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(ota_update.subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(OSError("blocked")))
    with pytest.raises(OSError, match="blocked"):
        ota_update.apply_update_files(source, target, digest)
    assert target.read_bytes() == b"MZold-version"


def update_files(tmp_path):
    source, target = tmp_path / "new.exe", tmp_path / "app.exe"
    source.write_bytes(b"MZnew-version")
    target.write_bytes(b"MZold-version")
    return source, target, hashlib.sha256(source.read_bytes()).hexdigest()


def test_helper_ready_only_after_staging_and_waits_before_replacing(tmp_path, monkeypatch):
    source, target, digest = update_files(tmp_path)
    ready = tmp_path / "ready"

    def wait(pid):
        assert pid == 123
        assert ready.read_text() == "ready"
        assert target.read_bytes() == b"MZold-version"

    def launch(argv, **kwargs):
        assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        ota_update.report_update_startup(argv)
        return Mock()

    monkeypatch.setattr(ota_update, "_wait_for_process", wait)
    monkeypatch.setattr(ota_update.subprocess, "Popen", launch)
    ota_update.apply_update_files(source, target, digest, 123, ready)
    assert target.read_bytes() == b"MZnew-version"
    assert not target.with_suffix(".exe.previous").exists()


def test_parent_timeout_preserves_old_executable(tmp_path, monkeypatch):
    source, target, digest = update_files(tmp_path)
    monkeypatch.setattr(ota_update, "_wait_for_process", Mock(side_effect=TimeoutError("still running")))
    with pytest.raises(TimeoutError):
        ota_update.apply_update_files(source, target, digest, 123)
    assert target.read_bytes() == b"MZold-version"
    assert not list(tmp_path.glob("*.update.tmp"))


def test_retry_when_windows_bootloader_still_locks_exe(tmp_path, monkeypatch):
    source, target, _ = update_files(tmp_path)
    replace = os.replace
    attempts = []

    def flaky_replace(a, b):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("locked")
        replace(a, b)

    monkeypatch.setattr(ota_update.os, "replace", flaky_replace)
    monkeypatch.setattr(ota_update.time, "sleep", lambda _: None)
    ota_update._replace_with_retry(source, target)
    assert len(attempts) == 3


def test_new_app_crash_restores_and_restarts_old_app(tmp_path, monkeypatch):
    source, target, digest = update_files(tmp_path)
    launch = Mock(return_value=Mock(poll=Mock(return_value=1), returncode=1))
    monkeypatch.setattr(ota_update, "_launch_independent", launch)
    with pytest.raises(RuntimeError, match="đã thoát"):
        ota_update.apply_update_files(source, target, digest)
    assert target.read_bytes() == b"MZold-version"
    assert launch.call_args.args[0] == [str(target)]


def test_launcher_waits_for_helper_readiness(tmp_path, monkeypatch):
    source, target, digest = update_files(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def launch(argv):
        Path(argv[argv.index("--helper-ready") + 1]).write_text("ready")
        return Mock()

    monkeypatch.setattr(ota_update, "_launch_independent", launch)
    ota_update.launch_self_update(source, target, digest)


def test_unconfirmed_running_app_keeps_recovery_copy(tmp_path, monkeypatch):
    source, target, digest = update_files(tmp_path)
    monkeypatch.setattr(ota_update, "_launch_independent", Mock(return_value=Mock(poll=Mock(return_value=None))))
    monkeypatch.setattr(ota_update, "_wait_for_marker", Mock(side_effect=TimeoutError("no UI confirmation")))
    with pytest.raises(TimeoutError):
        ota_update.apply_update_files(source, target, digest)
    assert target.read_bytes() == b"MZnew-version"
    assert target.with_suffix(".exe.previous").read_bytes() == b"MZold-version"


def test_ui_stays_open_when_update_preparation_fails(monkeypatch, tmp_path):
    import queue
    from types import SimpleNamespace

    import app as desktop

    app = SimpleNamespace(set_status=Mock(), exit_app=Mock(), events=queue.Queue())
    monkeypatch.setattr(desktop, "launch_self_update", Mock(side_effect=OSError("cannot write destination")))
    desktop.BookingNotifierApp._install_update(app, SimpleNamespace(sha256="a" * 64), tmp_path / "new.exe")
    event, message = app.events.get(timeout=5)
    assert event == "update_failed"
    assert "cannot write" in message
    app.exit_app.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows process handles")
def test_wait_for_process_observes_exit_even_with_open_process_handle():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
    try:
        ota_update._wait_for_process(process.pid, 5)
        assert process.wait(timeout=2) == 0
    finally:
        if process.poll() is None:
            process.kill()

from __future__ import annotations

import base64
import hashlib
import json

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


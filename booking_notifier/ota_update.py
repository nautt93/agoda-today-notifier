from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_MANIFEST_BYTES = 1_000_000
MAX_UPDATE_BYTES = 300_000_000
USER_AGENT = "BookingDesk-Updater/1.6.1"
UPDATE_PUBLIC_KEY_B64 = "MfTQyUTsvyFEk2ybaktEjB26OsNYQLVVM/4dWRo6RO8="
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+]([0-9A-Za-z.-]+))?$")


@dataclass(slots=True)
class UpdateInfo:
    version: str
    download_source: str
    sha256: str
    notes: str = ""
    size: int | None = None
    signature: str = ""


def version_key(value: str) -> tuple[int, int, int, int, str]:
    match = VERSION_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Phiên bản không hợp lệ: {value}")
    major, minor, patch = map(int, match.group(1, 2, 3))
    suffix = match.group(4) or ""
    return major, minor, patch, 0 if suffix else 1, suffix


def is_newer_version(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


def _is_web(source: str) -> bool:
    return urllib.parse.urlparse(source).scheme.lower() in {"http", "https"}


def _read_source(source: str, max_bytes: int) -> bytes:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme.lower() in {"http", "https"}:
        if parsed.scheme.lower() != "https":
            raise ValueError("Nguồn OTA trên Internet bắt buộc phải dùng HTTPS.")
        request = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme.lower() != "https":
                raise ValueError("Nguồn cập nhật chuyển hướng khỏi HTTPS.")
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise ValueError("Tệp cập nhật lớn hơn giới hạn cho phép.")
            value = response.read(max_bytes + 1)
            if len(value) > max_bytes:
                raise ValueError("Tệp cập nhật lớn hơn giới hạn cho phép.")
            return value
    path = Path(urllib.request.url2pathname(parsed.path)) if parsed.scheme == "file" else Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"Không tìm thấy nguồn cập nhật: {path}")
    if path.stat().st_size > max_bytes:
        raise ValueError("Tệp cập nhật lớn hơn giới hạn cho phép.")
    return path.read_bytes()


def _canonical_manifest(value: dict[str, object]) -> bytes:
    signed = {
        "schema": value.get("schema"),
        "version": value.get("version"),
        "url": value.get("url"),
        "sha256": value.get("sha256"),
        "size": value.get("size"),
        "notes": value.get("notes", ""),
    }
    return json.dumps(signed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_manifest_signature(value: dict[str, object], require_signature: bool) -> None:
    signature_text = str(value.get("signature", "")).strip()
    if not signature_text:
        if require_signature:
            raise ValueError("Manifest OTA trên Internet chưa được ký; đã chặn cập nhật.")
        return
    try:
        signature = base64.b64decode(signature_text, validate=True)
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(UPDATE_PUBLIC_KEY_B64))
        public_key.verify(signature, _canonical_manifest(value))
    except (ValueError, InvalidSignature) as exc:
        raise ValueError("Chữ ký manifest OTA không hợp lệ; đã chặn cập nhật.") from exc


def _resolve_download_source(manifest_source: str, value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme:
        if parsed.scheme.lower() in {"http", "https"} and parsed.scheme.lower() != "https":
            raise ValueError("File EXE OTA trên Internet bắt buộc phải dùng HTTPS.")
        if parsed.scheme.lower() not in {"https", "file"}:
            raise ValueError("Nguồn file OTA phải là HTTPS, file hoặc đường dẫn mạng.")
        return value
    if _is_web(manifest_source):
        resolved = urllib.parse.urljoin(manifest_source, value)
        if urllib.parse.urlparse(resolved).scheme.lower() != "https":
            raise ValueError("File EXE OTA trên Internet bắt buộc phải dùng HTTPS.")
        return resolved
    base = Path(manifest_source).resolve().parent
    target = (base / value).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError("Đường dẫn file OTA không được ra ngoài thư mục manifest.") from exc
    return str(target)


def load_update_manifest(source: str) -> UpdateInfo:
    if not source.strip():
        raise ValueError("Chưa cấu hình nguồn cập nhật OTA.")
    try:
        value = json.loads(_read_source(source, MAX_MANIFEST_BYTES).decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise ValueError("update.json phải dùng mã hóa UTF-8.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("update.json không phải JSON hợp lệ.") from exc
    if not isinstance(value, dict):
        raise ValueError("update.json phải là một JSON object.")
    if value.get("schema") not in {1, 2}:
        raise ValueError("Phiên bản schema update.json không được hỗ trợ.")
    verify_manifest_signature(value, require_signature=_is_web(source))
    version = str(value.get("version", "")).removeprefix("v")
    version_key(version)
    url = str(value.get("url") or value.get("download_url") or "").strip()
    if not url:
        raise ValueError("update.json thiếu trường url.")
    sha256 = str(value.get("sha256", "")).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("update.json có SHA-256 không hợp lệ.")
    size_raw = value.get("size")
    size = int(size_raw) if size_raw is not None else None
    if size is not None and not 1 <= size <= MAX_UPDATE_BYTES:
        raise ValueError("update.json có size không hợp lệ.")
    return UpdateInfo(
        version=version,
        download_source=_resolve_download_source(source, url),
        sha256=sha256,
        notes=str(value.get("notes", "")),
        size=size,
        signature=str(value.get("signature", "")),
    )


def check_for_update(current_version: str, manifest_source: str) -> UpdateInfo | None:
    info = load_update_manifest(manifest_source)
    return info if is_newer_version(info.version, current_version) else None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_update_executable(path: Path, expected_sha256: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Không tìm thấy bản cập nhật: {path}")
    with path.open("rb") as handle:
        if handle.read(2) != b"MZ":
            raise ValueError("Bản cập nhật không phải file EXE Windows hợp lệ.")
    if file_sha256(path) != expected_sha256.lower():
        raise ValueError("SHA-256 của bản cập nhật không khớp.")


def download_update(
    info: UpdateInfo,
    destination_dir: Path | None = None,
    progress: Callable[[int, int | None], None] | None = None,
) -> Path:
    destination = destination_dir or Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "AgodaTodayNotifier" / "updates"
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"Booking-Checkin-Hom-Nay-{info.version}.exe"
    partial = target.with_suffix(".exe.part")
    parsed = urllib.parse.urlparse(info.download_source)
    downloaded = 0
    digest = hashlib.sha256()
    try:
        if parsed.scheme.lower() == "https":
            request = urllib.request.Request(info.download_source, headers={"User-Agent": USER_AGENT})
            source_handle = urllib.request.urlopen(request, timeout=60)
        else:
            source_handle = open(Path(urllib.request.url2pathname(parsed.path)) if parsed.scheme == "file" else Path(info.download_source), "rb")
        with source_handle, partial.open("wb") as output:
            while True:
                chunk = source_handle.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > MAX_UPDATE_BYTES:
                    raise ValueError("File cập nhật lớn hơn giới hạn cho phép.")
                digest.update(chunk)
                output.write(chunk)
                if progress:
                    progress(downloaded, info.size)
        if info.size is not None and downloaded != info.size:
            raise ValueError("Kích thước file cập nhật không khớp update.json.")
        if digest.hexdigest() != info.sha256:
            raise ValueError("SHA-256 không khớp; đã hủy bản cập nhật không an toàn.")
        with partial.open("rb") as handle:
            if handle.read(2) != b"MZ":
                raise ValueError("File cập nhật không phải ứng dụng Windows hợp lệ.")
        os.replace(partial, target)
        return target
    finally:
        partial.unlink(missing_ok=True)


def _wait_for_process(parent_pid: int, timeout_seconds: float = 60.0) -> None:
    if os.name != "nt" or parent_pid <= 0:
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, parent_pid)  # type: ignore[name-defined]
        except Exception:
            return
        if not handle:
            return
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[name-defined]
        time.sleep(0.3)
    raise TimeoutError("Ứng dụng cũ chưa đóng sau 60 giây.")


def apply_update_files(source_exe: Path, target_exe: Path, expected_sha256: str, parent_pid: int = 0) -> None:
    validate_update_executable(source_exe, expected_sha256)
    _wait_for_process(parent_pid)
    temporary = target_exe.with_suffix(target_exe.suffix + ".update.tmp")
    backup = target_exe.with_suffix(target_exe.suffix + ".previous")
    temporary.unlink(missing_ok=True)
    shutil.copy2(source_exe, temporary)
    validate_update_executable(temporary, expected_sha256)
    try:
        backup.unlink(missing_ok=True)
        if target_exe.exists():
            os.replace(target_exe, backup)
        os.replace(temporary, target_exe)
        subprocess.Popen([str(target_exe)], close_fds=True)
        backup.unlink(missing_ok=True)
    except Exception:
        temporary.unlink(missing_ok=True)
        if backup.exists():
            os.replace(backup, target_exe)
        if target_exe.exists():
            try:
                subprocess.Popen([str(target_exe)], close_fds=True)
            except Exception:
                pass
        raise


def launch_self_update(downloaded_exe: Path, current_exe: Path, expected_sha256: str) -> subprocess.Popen:
    validate_update_executable(downloaded_exe, expected_sha256)
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Chỉ có thể tự cập nhật khi chạy bản EXE đã đóng gói.")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    return subprocess.Popen(
        [
            str(downloaded_exe), "--apply-update", "--parent-pid", str(os.getpid()),
            "--target", str(current_exe), "--sha256", expected_sha256,
        ],
        creationflags=flags,
        close_fds=True,
    )


def run_update_helper_from_argv(argv: list[str]) -> int | None:
    if "--apply-update" not in argv:
        return None
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--apply-update", action="store_true")
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--sha256", required=True)
    args, _ = parser.parse_known_args(argv[1:])
    try:
        apply_update_files(Path(argv[0]), Path(args.target), args.sha256, args.parent_pid)
        return 0
    except Exception as exc:
        log_path = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "AgodaTodayNotifier" / "update-error.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {exc}\n", encoding="utf-8")
        return 2


# Imported lazily on non-Windows platforms so test discovery remains portable.
if os.name == "nt":
    import ctypes

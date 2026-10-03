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
import traceback
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_MANIFEST_BYTES = 1_000_000
MAX_UPDATE_BYTES = 300_000_000
USER_AGENT = "BookingDesk-Updater/1.7.14"
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
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x100000, False, parent_pid)  # SYNCHRONIZE
    if not handle:
        if ctypes.get_last_error() == 87:  # PID no longer exists.
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = kernel.WaitForSingleObject(handle, int(timeout_seconds * 1000))
        if result == 258:
            raise TimeoutError("Ứng dụng cũ chưa đóng; chưa thay thế tệp chương trình.")
        if result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


def _replace_with_retry(source: Path, target: Path, timeout_seconds: float = 15.0) -> None:
    # The onefile bootloader / antivirus can hold the EXE briefly after Python exits.
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def _launch_independent(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        argv, close_fds=True,
        env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _wait_for_marker(process: subprocess.Popen, marker: Path, timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if marker.is_file():
            return
        if process.poll() is not None:
            raise RuntimeError(f"Tiến trình cập nhật/ứng dụng đã thoát (mã {process.returncode}).")
        time.sleep(0.1)
    raise TimeoutError("Chưa nhận được xác nhận khởi động sau 60 giây.")


def report_update_startup(argv: list[str]) -> None:
    """Called only after the new app has built its UI and owns the single-instance lock."""
    if "--update-started" in argv:
        marker = Path(argv[argv.index("--update-started") + 1])
        marker.write_text(str(os.getpid()), encoding="utf-8")


def apply_update_files(
    source_exe: Path, target_exe: Path, expected_sha256: str, parent_pid: int = 0,
    ready_file: Path | None = None,
) -> None:
    validate_update_executable(source_exe, expected_sha256)
    if source_exe.resolve() == target_exe.resolve():
        raise ValueError("Tệp tải về phải khác tệp ứng dụng đang chạy.")
    descriptor, staging = tempfile.mkstemp(prefix=target_exe.name + ".", suffix=".update.tmp", dir=target_exe.parent)
    os.close(descriptor)
    temporary = Path(staging)
    backup = target_exe.with_suffix(target_exe.suffix + ".previous")
    startup_marker = temporary.with_suffix(".started")
    replaced = False
    process = None
    try:
        # Validate the staged copy AND destination write access before asking the old app to exit.
        shutil.copy2(source_exe, temporary)
        validate_update_executable(temporary, expected_sha256)
        if ready_file is not None:
            ready_file.write_text("ready", encoding="utf-8")
        _wait_for_process(parent_pid)
        backup.unlink(missing_ok=True)
        if target_exe.exists():
            _replace_with_retry(target_exe, backup)
        replaced = True
        _replace_with_retry(temporary, target_exe)
        process = _launch_independent([str(target_exe), "--update-started", str(startup_marker)])
        _wait_for_marker(process, startup_marker)
    except Exception:
        # Do not overwrite a new instance that may still be running but has not signalled yet.
        if replaced and (process is None or process.poll() is not None):
            if backup.exists():
                _replace_with_retry(backup, target_exe)
                _launch_independent([str(target_exe)])
        raise
    else:
        try:
            backup.unlink(missing_ok=True)
        except OSError:
            pass  # A leftover recovery copy must not roll back a working application.
    finally:
        temporary.unlink(missing_ok=True)
        startup_marker.unlink(missing_ok=True)


def launch_self_update(downloaded_exe: Path, current_exe: Path, expected_sha256: str) -> subprocess.Popen:
    validate_update_executable(downloaded_exe, expected_sha256)
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Chỉ có thể tự cập nhật khi chạy bản EXE đã đóng gói.")
    with tempfile.TemporaryDirectory(prefix="booking-update-ready-") as directory:
        ready = Path(directory) / "ready"
        process = _launch_independent([
            str(downloaded_exe), "--apply-update", "--parent-pid", str(os.getpid()),
            "--target", str(current_exe), "--sha256", expected_sha256,
            "--helper-ready", str(ready),
        ])
        _wait_for_marker(process, ready)
        return process


def run_update_helper_from_argv(argv: list[str]) -> int | None:
    if "--apply-update" not in argv:
        return None
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--apply-update", action="store_true")
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--helper-ready")
    args, _ = parser.parse_known_args(argv[1:])
    try:
        source = Path(sys.executable) if getattr(sys, "frozen", False) else Path(argv[0])
        apply_update_files(source, Path(args.target), args.sha256, args.parent_pid,
                           Path(args.helper_ready) if args.helper_ready else None)
        return 0
    except Exception as exc:
        log_path = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "AgodaTodayNotifier" / "update-error.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n{traceback.format_exc()}\n", encoding="utf-8")
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, f"Không hoàn tất cập nhật: {exc}\n\nChi tiết: {log_path}\n"
                "Hãy mở lại ứng dụng hoặc tải bản mới trực tiếp từ GitHub.", "Lỗi cập nhật Booking Desk", 0x10,
            )
        return 2

"""Source-specific bundled MP3s, fallback chimes, and Windows playback."""

import hashlib
import math
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any

SOURCE_SOUND_KEYS = {
    "agoda": "agoda_sound_file",
    "expedia": "expedia_sound_file",
    "traveloka": "traveloka_sound_file",
    "trip": "trip_sound_file",
}
SOURCE_SOUND_FILES = {"agoda": "1-agoda.mp3", "expedia": "2-expedia.mp3", "traveloka": "3-traveloka.mp3", "trip": "4-trip.mp3"}
SOURCE_SOUND_PACK = "hotel-mp3-v1"
TRIP_SOUND_PACK = "trip-mp3-v1"
SOURCE_SOUND_SHA256 = {
    "agoda": "3fa61f373996074a05dc1851d96510dea3081df922e3efd17a9910582ed920c9",
    "expedia": "5badf83c9b0dbccf032f49d16d8d510693f2d55084cf7a7086ff55b277bffb69",
    "traveloka": "dbd9535a22f4f6f48952f3598048b213923818dc633b45d28813e4577015762e",
    "trip": "e998ab377bfb81d68bed847045a5c8135cdca94da15a5f46dc997d3ab6cd07e4",
}
SOURCE_PCM_FILES = {source: Path(filename).stem + "-pcm.wav" for source, filename in SOURCE_SOUND_FILES.items()}
SOURCE_PCM_SHA256 = {
    "agoda": "65f52af3772aac91753e9c8646963ce9dc25e02e8b533e7a19b52865871050ec",
    "expedia": "732e0a4a9ab44ff5f10dbb6c9e9f863b6837a2736a19cb8668c6688f599c0b38",
    "traveloka": "b7f80a489713fe6e553d392fb84330c2a2323c6f31835ec1b055423c9ce3123a",
    "trip": "745006680189974e73add2f9bba9c5f585636c998fde37a161c1b25a0936f7c4",
}
CHIME_NOTES = {
    "agoda": (659.25, 880.0),
    "expedia": (523.25, 659.25, 783.99),
    "traveloka": (783.99, 659.25, 523.25),
    "trip": (880.0, 523.25, 783.99, 659.25),
}
SAMPLE_RATE = 22050


def configured_sound_paths(config: dict[str, Any], source: str) -> list[Path]:
    """Use this source's file first, then the legacy common sound (never the other source)."""
    source_key = SOURCE_SOUND_KEYS.get(source.strip().lower(), "")
    values = [config.get(source_key, "") if source_key else "", config.get("sound_file", "")]
    paths: list[Path] = []
    for value in values:
        if value and str(value).strip():
            path = Path(str(value).strip())
            if path not in paths:
                paths.append(path)
    return paths


def bundled_source_sound(source: str, directory: Path) -> Path:
    """Install the exact supplied MP3 atomically in writable app data.

    PyInstaller's temporary extraction folder is read-only input: playback uses
    the stable app-data copy, which remains available when the EXE is updated.
    Existing intact copies are reused; damaged copies are repaired from the
    verified package, never by silently using another provider's file.
    """
    return _install_bundled_audio(source, directory, SOURCE_SOUND_FILES, SOURCE_SOUND_SHA256)


def bundled_source_pcm(source: str, directory: Path) -> Path:
    """Install the same supplied recording as PCM WAV when MP3/MCI is unavailable.

    These WAVs were decoded once at build time without resampling or changing
    the recording. No decoder library or media component is needed at runtime.
    The original, byte-identical MP3 remains the first playback choice.
    """
    return _install_bundled_audio(source, directory, SOURCE_PCM_FILES, SOURCE_PCM_SHA256)


def _install_bundled_audio(source: str, directory: Path, filenames: dict[str, str], hashes: dict[str, str]) -> Path:
    key = source.strip().lower()
    filename = filenames.get(key)
    if filename is None:
        raise ValueError("Nguồn booking chưa có tệp âm thanh đi kèm.")
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    data = (bundle_root / "assets" / "sounds" / filename).read_bytes()
    expected_hash = hashes[key]
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError(f"Tệp âm thanh đi kèm {source.strip()} không hợp lệ.")
    directory = Path(directory)
    path = directory / filename
    try:
        if hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash:
            return path
    except OSError:
        pass
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def default_source_sound(source: str, directory: Path) -> Path:
    """Create distinct fallback PCM WAVs in writable app data, not beside the EXE."""
    key = source.strip().lower()
    notes = CHIME_NOTES.get(key)
    if notes is None:
        raise ValueError("Nguồn booking chưa có âm thanh mặc định.")
    path = directory / f"{key}-chime-v1.wav"
    try:
        with wave.open(str(path), "rb") as sound:
            if ((sound.getnchannels(), sound.getsampwidth(), sound.getframerate(), sound.getnframes()) == (1, 2, SAMPLE_RATE, SAMPLE_RATE * 3)
                    and path.stat().st_size >= 44 + SAMPLE_RATE * 3 * 2):
                return path
    except (OSError, EOFError, wave.Error):
        pass
    directory.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    # Soft attack/release prevents clicks; a quiet tail spaces out the repeated alert.
    for frequency in notes:
        for index in range(int(SAMPLE_RATE * 0.38)):
            seconds = index / SAMPLE_RATE
            envelope = min(1.0, seconds / 0.018, max(0.0, (0.38 - seconds) / 0.08)) * math.exp(-2 * seconds)
            sample = int(11000 * envelope * (math.sin(2 * math.pi * frequency * seconds)
                                            + 0.16 * math.sin(4 * math.pi * frequency * seconds)))
            frames.extend(struct.pack("<h", sample))
        frames.extend(b"\0\0" * int(SAMPLE_RATE * 0.08))
    frames.extend(b"\0\0" * (SAMPLE_RATE * 3 - len(frames) // 2))
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output, wave.open(output, "wb") as sound:
            sound.setnchannels(1)
            sound.setsampwidth(2)
            sound.setframerate(SAMPLE_RATE)
            sound.writeframes(frames)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


class WindowsMciError(RuntimeError):
    """An MCI failure with its original code, independent of Windows language."""

    def __init__(self, code: int, message: str):
        self.code = int(code)
        super().__init__(message)


class WindowsMciAudioPlayer:
    ALIAS = "bookingdesk_alert_mp3"

    @staticmethod
    def _send(command: str) -> None:
        import ctypes
        from ctypes import wintypes

        winmm = ctypes.WinDLL("winmm")
        winmm.mciSendStringW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HWND)
        winmm.mciSendStringW.restype = wintypes.DWORD
        result = winmm.mciSendStringW(command, None, 0, None)
        if result:
            error = ctypes.create_unicode_buffer(256)
            winmm.mciGetErrorStringW.argtypes = (wintypes.DWORD, wintypes.LPWSTR, wintypes.UINT)
            winmm.mciGetErrorStringW(result, error, len(error))
            raise WindowsMciError(result, error.value or f"Không phát được âm thanh ({result}).")

    def play_loop(self, path: Path) -> None:
        self.stop()
        filename = str(path.resolve())
        if '"' in filename:
            raise ValueError("Đường dẫn âm thanh không hợp lệ.")
        self._send(f'open "{filename}" type mpegvideo alias {self.ALIAS}')
        try:
            self._send(f"play {self.ALIAS} repeat")
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        try:
            self._send(f"close {self.ALIAS}")
        except (OSError, RuntimeError):
            pass

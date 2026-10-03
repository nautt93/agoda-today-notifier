"""Source-specific sound selection, built-in chimes, and Windows MP3 playback."""

import math
import os
import struct
import tempfile
import wave
from pathlib import Path
from typing import Any

SOURCE_SOUND_KEYS = {"agoda": "agoda_sound_file", "expedia": "expedia_sound_file"}
CHIME_NOTES = {"agoda": (659.25, 880.0), "expedia": (523.25, 659.25, 783.99)}
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


def default_source_sound(source: str, directory: Path) -> Path:
    """Create our own two distinct PCM WAV files in writable app data, not beside the EXE."""
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
            raise RuntimeError(error.value or f"Không phát được âm thanh ({result}).")

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

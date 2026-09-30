"""Windows MP3 loop playback retained from the original desktop app."""

from pathlib import Path


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

"""Hide/show only top-level Chromium windows owned by an explicit browser PID.

No browser processes are started or stopped here. The caller gets a fresh PID
from its owned CDP connection; a temporary, random page-title marker selects a
single window without relying on guest names, URLs, or a cached native handle.
"""
from __future__ import annotations

import ctypes as ct
import os
from typing import Any

SW_HIDE = 0
SW_RESTORE = 9
_BROWSER_WINDOW_CLASS = "Chrome_WidgetWin_"


class _Win32Windows:
    def __init__(self) -> None:
        # Use fixed-width Win32 integer types and pointer-sized HWND/LPARAM.
        self.callback_type = ct.WINFUNCTYPE(ct.c_int32, ct.c_void_p, ct.c_ssize_t)
        self.user32 = ct.WinDLL("user32", use_last_error=True)
        self.user32.EnumWindows.argtypes = [self.callback_type, ct.c_ssize_t]
        self.user32.EnumWindows.restype = ct.c_int32
        self.user32.IsWindow.argtypes = [ct.c_void_p]
        self.user32.IsWindow.restype = ct.c_int32
        self.user32.GetWindowThreadProcessId.argtypes = [ct.c_void_p, ct.POINTER(ct.c_uint32)]
        self.user32.GetWindowThreadProcessId.restype = ct.c_uint32
        for name in ("GetClassNameW", "GetWindowTextW"):
            function = getattr(self.user32, name)
            function.argtypes = [ct.c_void_p, ct.c_wchar_p, ct.c_int32]
            function.restype = ct.c_int32
        self.user32.ShowWindow.argtypes = [ct.c_void_p, ct.c_int32]
        self.user32.ShowWindow.restype = ct.c_int32

    def enumerate_windows(self) -> list[int]:
        windows: list[int] = []

        @self.callback_type
        def collect(hwnd: int, _parameter: int) -> int:
            if hwnd:
                windows.append(hwnd)
            return 1

        ct.set_last_error(0)
        if not self.user32.EnumWindows(collect, 0):
            raise ct.WinError(ct.get_last_error())
        return windows

    def is_window(self, hwnd: int) -> bool:
        return bool(self.user32.IsWindow(hwnd))

    def window_pid(self, hwnd: int) -> int:
        pid = ct.c_uint32()
        if not self.user32.GetWindowThreadProcessId(hwnd, ct.byref(pid)):
            return 0
        return pid.value

    def window_class(self, hwnd: int) -> str:
        buffer = ct.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def window_title(self, hwnd: int) -> str:
        buffer = ct.create_unicode_buffer(32768)
        self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def show_window(self, hwnd: int, command: int) -> None:
        # The return value is the PREVIOUS visibility, not success/failure.
        self.user32.ShowWindow(hwnd, command)


def _native() -> Any:
    return _Win32Windows() if os.name == "nt" else None


def _valid_pid(browser_pid: int) -> bool:
    return isinstance(browser_pid, int) and not isinstance(browser_pid, bool) and 0 < browser_pid <= 0xFFFFFFFF


def _owned_window(api: Any, hwnd: int, browser_pid: int, marker: str = "") -> bool:
    if not api.is_window(hwnd) or api.window_pid(hwnd) != browser_pid:
        return False
    if not api.window_class(hwnd).startswith(_BROWSER_WINDOW_CLASS):
        return False
    return not marker or marker in api.window_title(hwnd)


def _apply(api: Any, hwnd: int, browser_pid: int, command: int, marker: str = "") -> bool:
    # Handles can be destroyed/reused between enumeration and this operation.
    # Recheck class/title, then existence/PID immediately before ShowWindow.
    if not _owned_window(api, hwnd, browser_pid, marker):
        return False
    if not api.is_window(hwnd) or api.window_pid(hwnd) != browser_pid:
        return False
    api.show_window(hwnd, command)
    return True


def hide_owned_browser_windows(browser_pid: int) -> bool:
    """Request SW_HIDE for owned Chromium windows; False if none were touched."""
    if not _valid_pid(browser_pid):
        return False
    try:
        api = _native()
        if api is None:
            return False
        candidates = [hwnd for hwnd in api.enumerate_windows() if _owned_window(api, hwnd, browser_pid)]
        changed = False
        for hwnd in candidates:
            changed = _apply(api, hwnd, browser_pid, SW_HIDE) or changed
        return changed
    except OSError:
        return False


def set_owned_browser_window_visible(browser_pid: int, unique_title_marker: str, visible: bool) -> bool:
    """Hide/restore exactly one owned window matching an in-memory title nonce.

    A missing or ambiguous marker fails closed. True means ShowWindow was issued
    to a freshly checked owned handle, not that the window was visible before.
    """
    if not _valid_pid(browser_pid) or not isinstance(unique_title_marker, str):
        return False
    if not unique_title_marker.strip() or "\x00" in unique_title_marker:
        return False
    try:
        api = _native()
        if api is None:
            return False
        candidates = [hwnd for hwnd in api.enumerate_windows()
                      if _owned_window(api, hwnd, browser_pid, unique_title_marker)]
        if len(candidates) != 1:
            return False
        return _apply(api, candidates[0], browser_pid, SW_RESTORE if visible else SW_HIDE, unique_title_marker)
    except OSError:
        return False

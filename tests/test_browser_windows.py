from __future__ import annotations

import ctypes as ct
import os
import uuid
from collections import Counter
from unittest.mock import Mock

import pytest

import booking_notifier.browser_windows as windows


class FakeWindows:
    def __init__(self, records: dict[int, dict]) -> None:
        self.records = records
        self.shown: list[tuple[int, int]] = []
        self.pid_reads: Counter = Counter()
        self.exists_reads: Counter = Counter()
        self.title_reads: Counter = Counter()
        self.class_reads: Counter = Counter()
        self.pid_replaced_after: dict[int, int] = {}
        self.destroyed_after: dict[int, int] = {}
        self.title_replaced_after: dict[int, int] = {}
        self.class_replaced_after: dict[int, int] = {}

    def enumerate_windows(self):
        return list(self.records)

    def is_window(self, hwnd):
        self.exists_reads[hwnd] += 1
        return self.exists_reads[hwnd] <= self.destroyed_after.get(hwnd, 100)

    def window_pid(self, hwnd):
        self.pid_reads[hwnd] += 1
        if self.pid_reads[hwnd] > self.pid_replaced_after.get(hwnd, 100):
            return 9999
        return self.records[hwnd]["pid"]

    def window_class(self, hwnd):
        self.class_reads[hwnd] += 1
        if self.class_reads[hwnd] > self.class_replaced_after.get(hwnd, 100):
            return "unrelated replacement class"
        return self.records[hwnd]["class"]

    def window_title(self, hwnd):
        self.title_reads[hwnd] += 1
        if self.title_reads[hwnd] > self.title_replaced_after.get(hwnd, 100):
            return "unrelated replacement window"
        return self.records[hwnd].get("title", "")

    def show_window(self, hwnd, command):
        self.shown.append((hwnd, command))


def record(pid=1234, class_name="Chrome_WidgetWin_1", title="BookingDesk-nonce - Microsoft Edge"):
    return {"pid": pid, "class": class_name, "title": title}


def test_hide_only_exact_owned_pid_chromium_top_level_class(monkeypatch):
    # A pointer-sized handle must survive Python/native boundaries unchanged.
    owned = 0x123456789ABC
    api = FakeWindows({owned: record(), 2: record(class_name="Chrome_WidgetWin_0"),
                       3: record(pid=9999), 4: record(class_name="Notepad"),
                       5: record(class_name="Chrome_RenderWidgetHostHWND"), 6: record(class_name="Chrome_WidgetWin")})
    monkeypatch.setattr(windows, "_native", lambda: api)
    assert windows.hide_owned_browser_windows(1234)
    assert api.shown == [(owned, windows.SW_HIDE), (2, windows.SW_HIDE)]
    assert api.pid_reads[owned] == 3 and api.exists_reads[owned] == 3


@pytest.mark.parametrize("visible,command", [(False, windows.SW_HIDE), (True, windows.SW_RESTORE)])
def test_marker_selects_only_one_owned_window(monkeypatch, visible, command):
    api = FakeWindows({1: record(), 2: record(title="BookingDesk-keeper - Microsoft Edge"),
                       3: record(pid=9999), 4: record(class_name="Other")})
    monkeypatch.setattr(windows, "_native", lambda: api)
    assert windows.set_owned_browser_window_visible(1234, "BookingDesk-nonce", visible)
    assert api.shown == [(1, command)]
    assert api.pid_reads[1] == 3


def test_ambiguous_or_missing_marker_does_not_touch_any_window(monkeypatch):
    api = FakeWindows({1: record(), 2: record()})
    monkeypatch.setattr(windows, "_native", lambda: api)
    assert not windows.set_owned_browser_window_visible(1234, "BookingDesk-nonce", True)
    assert not windows.set_owned_browser_window_visible(1234, "missing", True)
    assert not api.shown


@pytest.mark.parametrize("method", ["hide", "marker"])
@pytest.mark.parametrize("mutation", ["destroyed", "reused_after_enumeration", "reused_immediately_before_show"])
def test_stale_or_reused_handles_never_touch_another_process(monkeypatch, method, mutation):
    api = FakeWindows({1: record()})
    if mutation == "destroyed":
        api.destroyed_after[1] = 1
    else:
        api.pid_replaced_after[1] = 1 if mutation == "reused_after_enumeration" else 2
    monkeypatch.setattr(windows, "_native", lambda: api)
    if method == "hide":
        assert not windows.hide_owned_browser_windows(1234)
    else:
        assert not windows.set_owned_browser_window_visible(1234, "BookingDesk-nonce", True)
    assert not api.shown


def test_changed_marker_after_enumeration_fails_closed(monkeypatch):
    api = FakeWindows({1: record()})
    api.title_replaced_after[1] = 1
    monkeypatch.setattr(windows, "_native", lambda: api)
    assert not windows.set_owned_browser_window_visible(1234, "BookingDesk-nonce", False)
    assert not api.shown


@pytest.mark.parametrize("method", ["hide", "marker"])
def test_reused_handle_with_foreign_class_is_not_touched(monkeypatch, method):
    api = FakeWindows({1: record()})
    api.class_replaced_after[1] = 1
    monkeypatch.setattr(windows, "_native", lambda: api)
    assert not (windows.hide_owned_browser_windows(1234) if method == "hide" else
                windows.set_owned_browser_window_visible(1234, "BookingDesk-nonce", True))
    assert not api.shown


def test_no_owned_windows_is_false(monkeypatch):
    api = FakeWindows({1: record(pid=9999), 2: record(class_name="Other")})
    monkeypatch.setattr(windows, "_native", lambda: api)
    assert not windows.hide_owned_browser_windows(1234)
    assert not api.shown


@pytest.mark.parametrize("pid", [0, -1, 0x100000000, True, None, "1234", 1234.0])
def test_invalid_pid_never_loads_win32(monkeypatch, pid):
    native = Mock()
    monkeypatch.setattr(windows, "_native", native)
    assert not windows.hide_owned_browser_windows(pid)
    assert not windows.set_owned_browser_window_visible(pid, "BookingDesk-nonce", True)
    native.assert_not_called()


@pytest.mark.parametrize("marker", ["", " ", None, "BookingDesk\x00nonce"])
def test_invalid_marker_never_loads_win32(monkeypatch, marker):
    native = Mock()
    monkeypatch.setattr(windows, "_native", native)
    assert not windows.set_owned_browser_window_visible(1234, marker, True)
    native.assert_not_called()


@pytest.mark.parametrize("operation", ["hide", "marker"])
def test_native_failure_is_nonfatal(monkeypatch, operation):
    monkeypatch.setattr(windows, "_native", Mock(side_effect=OSError("synthetic unavailable API")))
    assert not (windows.hide_owned_browser_windows(1234) if operation == "hide" else
                windows.set_owned_browser_window_visible(1234, "BookingDesk-nonce", True))


@pytest.mark.skipif(os.name == "nt", reason="Non-Windows no-op contract")
def test_non_windows_noop():
    assert windows._native() is None
    assert not windows.hide_owned_browser_windows(os.getpid())
    assert not windows.set_owned_browser_window_visible(os.getpid(), "BookingDesk-nonce", True)


@pytest.mark.skipif(os.name != "nt", reason="Native Win32 synthetic browser-window handles")
def test_native_synthetic_windows_hide_restore_and_class_filter():
    # These windows belong to THIS test process, not a real browser or account.
    api = windows._native()
    user32 = api.user32
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes, kernel.GetModuleHandleW.restype = [ct.c_wchar_p], ct.c_void_p
    callback_type = ct.WINFUNCTYPE(ct.c_ssize_t, ct.c_void_p, ct.c_uint32, ct.c_size_t, ct.c_ssize_t)
    user32.DefWindowProcW.argtypes = [ct.c_void_p, ct.c_uint32, ct.c_size_t, ct.c_ssize_t]
    user32.DefWindowProcW.restype = ct.c_ssize_t

    class WindowClass(ct.Structure):
        _fields_ = [("style", ct.c_uint32), ("lpfnWndProc", callback_type),
                    ("cbClsExtra", ct.c_int32), ("cbWndExtra", ct.c_int32),
                    ("hInstance", ct.c_void_p), ("hIcon", ct.c_void_p),
                    ("hCursor", ct.c_void_p), ("hbrBackground", ct.c_void_p),
                    ("lpszMenuName", ct.c_wchar_p), ("lpszClassName", ct.c_wchar_p)]

    user32.RegisterClassW.argtypes, user32.RegisterClassW.restype = [ct.POINTER(WindowClass)], ct.c_uint16
    user32.UnregisterClassW.argtypes, user32.UnregisterClassW.restype = [ct.c_wchar_p, ct.c_void_p], ct.c_int32
    user32.CreateWindowExW.argtypes = [ct.c_uint32, ct.c_wchar_p, ct.c_wchar_p, ct.c_uint32] + [ct.c_int32] * 4 + [ct.c_void_p] * 4
    user32.CreateWindowExW.restype = ct.c_void_p
    user32.DestroyWindow.argtypes, user32.DestroyWindow.restype = [ct.c_void_p], ct.c_int32
    user32.IsWindowVisible.argtypes, user32.IsWindowVisible.restype = [ct.c_void_p], ct.c_int32

    @callback_type
    def window_proc(hwnd, message, wparam, lparam):
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    module = kernel.GetModuleHandleW(None)
    nonce = uuid.uuid4().hex
    class_names = [f"Chrome_WidgetWin_BookingDeskTest_{nonce}", f"NotBrowser_BookingDeskTest_{nonce}"]
    handles, registered = [], []
    try:
        for index, name in enumerate(class_names):
            specification = WindowClass(0, window_proc, 0, 0, module, None, None, None, None, name)
            assert user32.RegisterClassW(ct.byref(specification)), ct.get_last_error()
            registered.append(name)
            hwnd = user32.CreateWindowExW(0, name, f"BookingDesk-native-{nonce}-{index}", 0x00CF0000,
                                         10, 10, 200, 120, None, None, module, None)
            assert hwnd, ct.get_last_error()
            handles.append(hwnd)
            user32.ShowWindow(hwnd, 1)
            assert user32.IsWindowVisible(hwnd)
        owned, unrelated = handles
        marker = f"BookingDesk-native-{nonce}-0"
        assert windows.set_owned_browser_window_visible(os.getpid(), marker, False)
        assert not user32.IsWindowVisible(owned) and user32.IsWindowVisible(unrelated)
        assert windows.set_owned_browser_window_visible(os.getpid(), marker, True)
        assert user32.IsWindowVisible(owned) and user32.IsWindowVisible(unrelated)
        assert windows.hide_owned_browser_windows(os.getpid())
        assert not user32.IsWindowVisible(owned) and user32.IsWindowVisible(unrelated)
    finally:
        for hwnd in handles:
            user32.DestroyWindow(hwnd)
        for name in registered:
            user32.UnregisterClassW(name, module)

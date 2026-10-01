from __future__ import annotations

import queue
from unittest.mock import Mock

from booking_notifier.system_tray import SystemTray


def test_ready_and_callbacks_only_post_events():
    events = queue.Queue()
    tray = SystemTray(events)
    icon = Mock()
    tray.icon = icon
    tray._on_ready(icon)
    assert icon.visible is True and tray.available
    assert events.get_nowait() == ("tray_ready", None)
    for callback, action in [(tray._open, "tray_open"), (tray._settings, "tray_settings"), (tray._exit, "tray_exit")]:
        callback(icon, None)
        assert events.get_nowait() == (action, None)
    tray.stop()
    icon.stop.assert_called_once()
    assert not tray.available


def test_close_before_icon_ready_cannot_leave_an_orphan_icon():
    events = queue.Queue()
    tray = SystemTray(events)
    icon = Mock()
    tray.icon = icon
    tray.stop()
    tray._on_ready(icon)
    icon.stop.assert_called_once()
    assert not tray.available and events.empty()


def test_visibility_failure_keeps_window_recoverable():
    from unittest.mock import PropertyMock

    events = queue.Queue()
    tray = SystemTray(events)
    icon = Mock()
    type(icon).visible = PropertyMock(side_effect=OSError("tray unavailable"))
    tray._on_ready(icon)
    assert not tray.available
    assert events.get_nowait() == ("tray_failed", "tray unavailable")
    icon.stop.assert_called_once()

"""Saved quiet schedules gate popup/audio/F92; isolated native time-picker UI."""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import ttk
from unittest.mock import Mock

import pytest

import app as desktop
from app import BookingNotifierApp
from booking_notifier.config import DEFAULT_CONFIG, ConfigStore
from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore


class Variable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _event(booking_id, checkin=None):
    return BookingEvent(source="Agoda", booking_id=booking_id, checkin_date=checkin or date.today(),
                        guest_name="SYNTHETIC QUIET GUEST", room_type="Superior Room x1",
                        checkout_date=(checkin or date.today()) + timedelta(days=1), total_revenue="VND 200.000")


def _app(tmp_path):
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.root = Mock()
    app.root.after.return_value = "synthetic-timer"
    app.config = {**DEFAULT_CONFIG, "quiet_hours_enabled": True,
                  "quiet_start_time": "22:30", "quiet_end_time": "06:15"}
    app.config_store = ConfigStore(tmp_path / "config.json")
    app.config_store.save(app.config)
    app.state = StateStore(tmp_path / "state.json")
    app.alert_queue, app.queued_ids = [], set()
    app.active_alert = app.active_popup = None
    app.active_sound_muted, app.sound_active = False, False
    app.sound_repeat_job = app.sound_preview_job = None
    app.closing = False
    app.monitor = None
    app.f92_worker = Mock()
    app.history_day = date.today()
    app.log = Mock()
    app.set_status = Mock()
    app.refresh_history = Mock(side_effect=lambda: setattr(app, "history_day", desktop.date.today()))
    app.play_sound = Mock(side_effect=lambda: setattr(app, "sound_active", True))
    app.stop_sound = Mock(side_effect=lambda: setattr(app, "sound_active", False))
    for name in ("active_guest_var", "active_room_var", "active_revenue_var", "active_checkout_var",
                 "active_nights_var", "active_copy_button",
                 "active_guest_label", "active_hero", "active_menu"):
        setattr(app, name, None)
    fields = {
        "provider_var": "Gmail", "host_var": "imap.gmail.com", "port_var": "993",
        "email_var": "", "password_var": "", "poll_var": "60", "scan_days_var": "90",
        "sound_var": "",
        "quiet_var": True, "quiet_start_hour_var": "22", "quiet_start_minute_var": "30",
        "quiet_end_hour_var": "06", "quiet_end_minute_var": "15", "quiet_feedback_var": "",
        "start_windows_var": False, "start_minimized_var": False, "f92_enabled_var": False,
        "f92_port_var": "AUTO", "f92_sound_var": "4", "f92_builtin_sound_var": False,
        "update_source_var": "",
    }
    for name, value in fields.items():
        setattr(app, name, Variable(value))
    app.source_sound_vars = {source: Variable("") for source in desktop.BOOKING_SOURCES}
    return app


def _quiet(monkeypatch, active):
    monkeypatch.setattr(desktop, "quiet_hours_active", lambda _config: active, raising=False)


def _consume_without_tk(app):
    def show():
        if app.active_alert is None and app.alert_queue:
            app.active_alert = app.alert_queue.pop(0)
            app.active_popup = Mock()
            app.play_sound()
            app.f92_worker.notify(app.active_alert)
    app._show_next_alert = Mock(side_effect=show)


@pytest.mark.parametrize("saved_enabled", [False, True])
def test_runtime_uses_saved_quiet_config_never_unsaved_picker_drafts(tmp_path, monkeypatch, saved_enabled):
    app = _app(tmp_path)
    app.config["quiet_hours_enabled"] = saved_enabled
    for name in ("quiet_var", "quiet_start_hour_var", "quiet_start_minute_var", "quiet_end_hour_var", "quiet_end_minute_var"):
        setattr(app, name, Mock(get=Mock(side_effect=AssertionError("Runtime read an unsaved quiet-settings draft"))))
    seen = []

    def from_saved(config):
        seen.append(config)
        assert config == app.config
        assert config["quiet_start_time"] == "22:30" and config["quiet_end_time"] == "06:15"
        return config["quiet_hours_enabled"]

    monkeypatch.setattr(desktop, "quiet_hours_active", from_saved, raising=False)
    assert app._quiet_hours_active() is saved_enabled
    assert seen == [app.config]


def test_quiet_incoming_and_direct_queue_advance_cannot_open_popup_audio_or_booking_f92(tmp_path, monkeypatch):
    app = _app(tmp_path)
    _quiet(monkeypatch, True)
    forbidden_popup = Mock(side_effect=AssertionError("Popup opened during quiet hours"))
    monkeypatch.setattr(desktop.tk, "Toplevel", forbidden_popup)
    event = app.state.register_today_confirmation(_event("5551000001"), ("synthetic-quiet-mail",), date.today())
    app.enqueue_alert(event)
    assert app.active_alert is None and app.active_popup is None
    assert not app.sound_active and not app.alert_queue and not app.queued_ids
    assert len(app.state.pending_for_date(date.today())) == 1 and not app.state.is_acknowledged(event)
    # Even a previously queued booking must recheck quiet time before advancing.
    app.alert_queue, app.queued_ids = [event], {event.storage_id}
    app._show_next_alert()
    assert app.active_alert is None and app.active_popup is None
    forbidden_popup.assert_not_called()
    app.play_sound.assert_not_called()
    app.f92_worker.notify.assert_not_called()
    app.f92_worker.set_notifications_suspended.assert_called_with(True)
    assert not app.state.is_acknowledged(event) and not app.state.history()


def test_entering_quiet_suspends_active_popup_without_acknowledging_and_resumes_once(tmp_path, monkeypatch):
    app = _app(tmp_path)
    first = app.state.register_today_confirmation(_event("5551000001"), ("synthetic-first",), date.today())
    second = app.state.register_today_confirmation(_event("5551000002"), ("synthetic-second",), date.today())
    popup = Mock()
    app.active_alert, app.active_popup, app.sound_active = first, popup, True
    app.alert_queue, app.queued_ids = [second], {first.storage_id, second.storage_id}
    _quiet(monkeypatch, True)
    assert app._enforce_quiet_hours()
    popup.destroy.assert_called_once()
    app.stop_sound.assert_called_once()
    app.f92_worker.idle.assert_called_once()
    app.f92_worker.notify.assert_not_called()
    app.f92_worker.set_notifications_suspended.assert_called_with(True)
    assert app.active_alert is None and app.active_popup is None and not app.sound_active
    assert not app.alert_queue and not app.queued_ids
    assert {event.storage_id for event in app.state.pending_for_date(date.today())} == {first.storage_id, second.storage_id}
    assert not app.state.history() and not app.state.is_acknowledged(first) and not app.state.is_acknowledged(second)
    assert app._enforce_quiet_hours()
    popup.destroy.assert_called_once()
    app.stop_sound.assert_called_once()
    app.f92_worker.idle.assert_called_once()
    _quiet(monkeypatch, False)
    _consume_without_tk(app)
    app._minute_tick()
    app._minute_tick()
    assert app.active_alert.storage_id == first.storage_id and app.sound_active
    assert [event.storage_id for event in app.alert_queue] == [second.storage_id]
    assert app.queued_ids == {first.storage_id, second.storage_id}
    app.play_sound.assert_called_once()
    app.f92_worker.notify.assert_called_once_with(app.active_alert)
    app.f92_worker.set_notifications_suspended.assert_called_with(False)
    assert not app.state.history() and not app.state.is_acknowledged(first)


def test_midnight_release_only_current_checkin_day_not_yesterday_pending(tmp_path, monkeypatch):
    app = _app(tmp_path)
    today, yesterday = date.today(), date.today() - timedelta(days=1)
    old = app.state.register_today_confirmation(_event("5551000001", yesterday), ("synthetic-yesterday",), yesterday)
    current = app.state.register_today_confirmation(_event("5551000002", today), ("synthetic-today",), today)
    app.history_day = yesterday

    class Today(date):
        @classmethod
        def today(cls):
            return today

    monkeypatch.setattr(desktop, "date", Today)
    _quiet(monkeypatch, True)
    app._minute_tick()
    assert app.active_alert is None and not app.alert_queue
    app.play_sound.assert_not_called()
    app.f92_worker.notify.assert_not_called()
    _quiet(monkeypatch, False)
    _consume_without_tk(app)
    app._minute_tick()
    app._minute_tick()
    assert app.active_alert.storage_id == current.storage_id and not app.alert_queue
    assert app.queued_ids == {current.storage_id}
    app.play_sound.assert_called_once()
    assert not app.state.is_acknowledged(old) and not app.state.is_acknowledged(current)
    assert len(app.state.pending_for_date(yesterday)) == 1


def test_delayed_monitor_restart_uses_latest_saved_quiet_preferences_not_restart_snapshot(tmp_path, monkeypatch):
    app = _app(tmp_path)
    app.monitor_generation = 9
    app.events = Mock()
    snapshot = {**app.config, "quiet_hours_enabled": False, "quiet_start_time": "00:00", "quiet_end_time": "08:00",
                "poll_seconds": 120}
    original_snapshot = dict(snapshot)
    latest = dict(app.config)
    monitor = Mock()
    constructor = Mock(return_value=monitor)
    monkeypatch.setattr(desktop, "ImapMonitor", constructor)
    app._start_monitor_generation(9, snapshot, "synthetic-password-only")
    constructor.assert_called_once()
    effective, password, state, events = constructor.call_args.args
    assert effective["quiet_hours_enabled"]
    assert effective["quiet_start_time"] == "22:30" and effective["quiet_end_time"] == "06:15"
    assert effective["poll_seconds"] == 120  # Unrelated connection settings retain the restart snapshot.
    assert password == "synthetic-password-only" and state is app.state and events is app.events
    assert app.monitor is monitor
    monitor.start.assert_called_once()
    assert snapshot == original_snapshot and app.config == latest
    app._start_monitor_generation(8, snapshot, "synthetic-password-only")
    constructor.assert_called_once()  # A superseded restart cannot create another worker.
    monitor.start.assert_called_once()


@pytest.mark.parametrize("second,microsecond,expected_delay", [(59, 900_000, 100), (0, 100_000, 30_000)])
def test_minute_tick_aligns_quiet_boundary_and_caps_periodic_check_delay(tmp_path, monkeypatch, second, microsecond, expected_delay):
    app = _app(tmp_path)
    today = date.today()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(today.year, today.month, today.day, 22, 29, second, microsecond, tzinfo=tz)

    monkeypatch.setattr(desktop, "datetime", Clock)
    _quiet(monkeypatch, True)
    app._minute_tick()
    app.root.after.assert_called_once_with(expected_delay, app._minute_tick)
    app.f92_worker.set_notifications_suspended.assert_called_with(True)
    app.play_sound.assert_not_called()
    app.f92_worker.notify.assert_not_called()
    assert not app.active_alert and not app.active_popup and not app.sound_active


@pytest.mark.parametrize("start_hour,start_minute,end_hour,end_minute", [
    ("24", "00", "06", "15"), ("22", "60", "06", "15"), ("22", "30", "-1", "15"),
    ("22", "30", "06", "-1"), ("hour", "30", "06", "15"), ("22", "30", "06", "61"),
    ("06", "15", "06", "15"),
])
def test_collect_rejects_invalid_or_equal_ranges_without_mutating_saved_config(
    tmp_path, monkeypatch, start_hour, start_minute, end_hour, end_minute,
):
    app = _app(tmp_path)
    saved, runtime = app.config_store.load(), dict(app.config)
    monkeypatch.setattr(desktop, "protect_secret", lambda _value: "")
    app.quiet_start_hour_var.set(start_hour)
    app.quiet_start_minute_var.set(start_minute)
    app.quiet_end_hour_var.set(end_hour)
    app.quiet_end_minute_var.set(end_minute)
    with pytest.raises(ValueError):
        app._collect_config()
    assert app.config_store.load() == saved and app.config == runtime


def test_collect_and_independent_quiet_save_canonical_pair_preserve_other_settings_without_restart(tmp_path, monkeypatch):
    app = _app(tmp_path)
    app.config.update(quiet_hours_enabled=False, poll_seconds=120, sound_file="preserve-user-sound.wav")
    app.config_store.save(app.config)
    saved = app.config_store.load()
    app.quiet_var.set(True)
    monkeypatch.setattr(desktop, "protect_secret", lambda _value: "")
    _quiet(monkeypatch, False)
    monitor = Mock()
    app.monitor = monitor
    collected = app._collect_config()
    assert collected["quiet_start_time"] == "22:30" and collected["quiet_end_time"] == "06:15"
    assert collected["quiet_hours_enabled"]
    app.save_quiet_hours()
    result = app.config_store.load()
    assert result["quiet_hours_enabled"] and result["quiet_start_time"] == "22:30" and result["quiet_end_time"] == "06:15"
    assert {key: value for key, value in result.items() if not key.startswith("quiet_")} == {
        key: value for key, value in saved.items() if not key.startswith("quiet_")
    }
    monitor.configure_quiet_hours.assert_called_once()
    assert monitor.configure_quiet_hours.call_args.args[0]["quiet_start_time"] == "22:30"
    monitor.stop.assert_not_called()
    monitor.start.assert_not_called()
    app.root.after.assert_not_called()  # Independent quiet save must not duplicate minute timers.


@pytest.mark.parametrize("failure", ["equal-range", "disk-write"])
def test_independent_quiet_save_failure_preserves_saved_runtime_and_active_notification(tmp_path, monkeypatch, failure):
    app = _app(tmp_path)
    saved, runtime = app.config_store.load(), dict(app.config)
    app.monitor = Mock()
    event, popup = _event("5551000001"), Mock()
    app.active_alert, app.active_popup, app.sound_active = event, popup, True
    error = Mock()
    monkeypatch.setattr(desktop.messagebox, "showerror", error)
    if failure == "equal-range":
        app.quiet_start_hour_var.set("06")
        app.quiet_start_minute_var.set("15")
    else:
        monkeypatch.setattr(app.config_store, "save", Mock(side_effect=OSError("Synthetic write failure")))
    app.save_quiet_hours()
    error.assert_called_once()
    assert app.config_store.load() == saved and app.config == runtime
    assert app.active_alert is event and app.active_popup is popup and app.sound_active
    popup.destroy.assert_not_called()
    app.stop_sound.assert_not_called()
    app.monitor.configure_quiet_hours.assert_not_called()
    app.monitor.stop.assert_not_called()
    app.monitor.start.assert_not_called()
    app.root.after.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows quiet schedule settings controls")
def test_native_four_quiet_time_pickers_toggle_independent_save_reload_and_equal_validation(tmp_path, monkeypatch):
    if os.environ.get("QUIET_HOURS_NATIVE_CHILD") != "1":
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q",
             f"{Path(__file__).resolve()}::test_native_four_quiet_time_pickers_toggle_independent_save_reload_and_equal_validation"],
            env={**os.environ, "QUIET_HOURS_NATIVE_CHILD": "1"}, check=True, timeout=90,
        )
        return
    from test_ui_smoke import wait_for_tray

    store = ConfigStore(tmp_path / "config.json")
    store.save({"email_address": "", "password_encrypted": "", "quiet_hours_enabled": False,
                "quiet_start_time": "00:00", "quiet_end_time": "08:00", "f92_enabled": False,
                "start_with_windows": False, "start_minimized": False, "update_manifest_source": "",
                "sound_file": "preserve-user-sound.wav", "poll_seconds": 120})
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(desktop, "ConfigStore", lambda: store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    start_monitoring = Mock()
    monkeypatch.setattr(desktop.BookingNotifierApp, "start_monitoring", start_monitoring)
    monkeypatch.setattr(desktop.BookingNotifierApp, "check_for_updates", Mock())
    play = Mock()
    monkeypatch.setattr(desktop.BookingNotifierApp, "play_sound", play)
    _quiet(monkeypatch, False)
    error = Mock()
    monkeypatch.setattr(desktop.messagebox, "showerror", error)
    root = tk.Tk()
    app = BookingNotifierApp(root)
    monitor = Mock()
    app.monitor = monitor
    try:
        wait_for_tray(app)
        app.open_settings()
        root.update()

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        widgets = {child.winfo_name(): child for child in descendants(app.settings_window)}
        names = ("quiet_start_hour", "quiet_start_minute", "quiet_end_hour", "quiet_end_minute")
        pickers = [widgets[name] for name in names]
        assert all(isinstance(picker, ttk.Combobox) for picker in pickers)
        checkbox, save = widgets["quiet_hours_enabled"], widgets["save_quiet_hours"]

        def show_quiet_controls():
            canvas = app.settings_canvas
            bounds = canvas.bbox("all")
            content_y = canvas.canvasy(checkbox.winfo_rooty() - canvas.winfo_rooty())
            if bounds and bounds[3] > bounds[1]:
                canvas.yview_moveto(max(0, (content_y - 40) / (bounds[3] - bounds[1])))
            root.update()
            for control in [checkbox, *pickers, save]:
                assert control.winfo_viewable()
                assert control.winfo_width() >= control.winfo_reqwidth()
                assert control.winfo_height() >= control.winfo_reqheight()
                assert canvas.winfo_rootx() <= control.winfo_rootx()
                assert control.winfo_rootx() + control.winfo_width() <= canvas.winfo_rootx() + canvas.winfo_width()
                assert canvas.winfo_rooty() <= control.winfo_rooty()
                assert control.winfo_rooty() + control.winfo_height() <= canvas.winfo_rooty() + canvas.winfo_height()

        show_quiet_controls()
        for index, picker in enumerate(pickers):
            maximum = 24 if index % 2 == 0 else 60
            assert tuple(picker.cget("values")) == tuple(f"{number:02d}" for number in range(maximum))
        assert checkbox.cget("text") == "Bật thời gian yên lặng" and save.cget("text") == "Lưu thời gian"
        assert all(str(picker.cget("state")) == "disabled" for picker in pickers)
        checkbox.invoke()
        root.update()
        assert app.quiet_var.get() and all(str(picker.cget("state")) == "readonly" for picker in pickers)
        assert not store.load()["quiet_hours_enabled"]  # Draft does not change saved/runtime policy.
        checkbox.invoke()
        root.update()
        assert all(str(picker.cget("state")) == "disabled" for picker in pickers)
        checkbox.invoke()
        for variable, value in zip((app.quiet_start_hour_var, app.quiet_start_minute_var,
                                    app.quiet_end_hour_var, app.quiet_end_minute_var), ("22", "30", "06", "15"), strict=True):
            variable.set(value)
        before = store.load()
        save.invoke()
        root.update()
        saved = store.load()
        assert saved["quiet_hours_enabled"] and saved["quiet_start_time"] == "22:30" and saved["quiet_end_time"] == "06:15"
        assert {key: value for key, value in saved.items() if not key.startswith("quiet_")} == {
            key: value for key, value in before.items() if not key.startswith("quiet_")
        }
        monitor.configure_quiet_hours.assert_called_once()
        assert monitor.configure_quiet_hours.call_args.args[0]["quiet_end_time"] == "06:15"
        start_monitoring.assert_not_called()
        monitor.stop.assert_not_called()
        play.assert_not_called()
        error.assert_not_called()
        assert not app.email_var.get() and not app.password_var.get()

        # Reopening/reloading must show the canonical persisted values, not a
        # stale draft, while the controls remain readonly when enabled.
        app.quiet_start_hour_var.set("01")
        app._load_config()
        app.settings_window.withdraw()
        app.open_settings()
        root.update()
        assert [variable.get() for variable in (app.quiet_start_hour_var, app.quiet_start_minute_var,
                                               app.quiet_end_hour_var, app.quiet_end_minute_var)] == ["22", "30", "06", "15"]
        assert all(str(picker.cget("state")) == "readonly" for picker in pickers)
        show_quiet_controls()
        directory = os.environ.get("BOOKING_UI_SCREENSHOT_DIR")
        if directory:
            from PIL import ImageGrab

            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            window = app.settings_window
            x, y = window.winfo_rootx(), window.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + window.winfo_width(), y + window.winfo_height())).save(target / "quiet-hours-settings.png")

        app.quiet_start_hour_var.set("06")
        app.quiet_start_minute_var.set("15")
        save.invoke()
        root.update()
        error.assert_called_once()
        assert store.load() == saved and app.config["quiet_start_time"] == "22:30"
        assert monitor.configure_quiet_hours.call_count == 1
        assert not app.active_popup and not app.sound_active and not state.history()
        play.assert_not_called()
        start_monitoring.assert_not_called()
    finally:
        app.exit_app()

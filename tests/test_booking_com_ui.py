"""Native Booking.com email details use the shared desktop notification."""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
import webbrowser
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_booking_com import exely_message
from test_booking_popup_windows import _assert_popup_content_contained, _screenshot
from test_ui_smoke import popup_action_buttons

import app as desktop
from app import BookingNotifierApp
from booking_notifier.config import ConfigStore
from booking_notifier.excel_export import excel_tsv
from booking_notifier.models import BookingEvent
from booking_notifier.parsing import parse_booking_message
from booking_notifier.state import StateStore

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" and os.name != "nt", reason="Native desktop Tk regression on macOS and Windows",
)


def _run_isolated(node: str) -> bool:
    if os.environ.get("BOOKING_COM_NATIVE_CHILD") == node:
        return False
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", f"{Path(__file__).resolve()}::{node}"],
        env={**os.environ, "BOOKING_COM_NATIVE_CHILD": node}, check=True, timeout=90,
    )
    return True


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def _synthetic_app(tmp_path, monkeypatch, *, legacy=False):
    store = ConfigStore(tmp_path / "config.json")
    config = {
        "f92_enabled": False, "quiet_hours_enabled": False, "start_with_windows": False,
        "start_minimized": False, "update_manifest_source": "", "email_address": "", "password_encrypted": "",
    }
    if legacy:
        config.update({"booking_com_enrichment": True, "booking_com_enrichment_mode": "visible",
                       "booking_com_browser": "chrome"})
    store.save(config)
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(desktop, "ConfigStore", lambda: store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    monkeypatch.setattr(store, "install_source_sound_pack", lambda _directory: store.load())
    monkeypatch.setattr(desktop, "default_source_sound", lambda *_args: tmp_path / "synthetic.wav")
    monkeypatch.setattr(desktop, "F92Worker", lambda *_args: Mock())
    monkeypatch.setattr(desktop, "SystemTray", lambda *_args: Mock(available=True))
    monkeypatch.setattr(desktop, "WindowsMciAudioPlayer", Mock())
    monkeypatch.setattr(webbrowser, "open", Mock(side_effect=AssertionError("No browser I/O in this test")))
    monkeypatch.setattr(BookingNotifierApp, "start_monitoring", Mock())
    monkeypatch.setattr(BookingNotifierApp, "check_for_updates", Mock())
    if os.name == "nt":
        import winsound

        monkeypatch.setattr(winsound, "PlaySound", Mock())
        monkeypatch.setattr(winsound, "MessageBeep", Mock())
    plays = Mock()

    def play_sound(instance):
        instance.sound_active = True
        plays(instance.active_alert.source)

    monkeypatch.setattr(BookingNotifierApp, "play_sound", play_sound)
    root = tk.Tk()
    monkeypatch.setattr(root, "after", Mock(return_value="synthetic-root-timer"))
    monkeypatch.setattr(root, "after_cancel", Mock())
    instance = BookingNotifierApp(root)
    instance.hide_to_tray()
    root.update()
    assert root.state() == "withdrawn"
    return instance, state, plays


def _event(booking_id, *, checkin=None, **details):
    return BookingEvent(source="Booking.com", booking_id=booking_id,
                        checkin_date=checkin or date.today(), **details)


def test_native_exely_popup_hidden_main_copy_close_and_no_replay(tmp_path, monkeypatch):
    if _run_isolated("test_native_exely_popup_hidden_main_copy_close_and_no_replay"):
        return
    instance, state, plays = _synthetic_app(tmp_path, monkeypatch, legacy=True)
    root = instance.root
    try:
        original = parse_booking_message(exely_message(date.today()))
        pending = state.register_today_confirmation(original, ("synthetic-exely",), date.today())
        instance.enqueue_alert(pending)
        root.update()
        popup = instance.active_popup
        copy, close = popup_action_buttons(popup)
        assert popup.winfo_viewable() and root.state() == "withdrawn"
        assert copy.cget("text") == "Sao chép" and copy.instate(["!disabled"])
        labels = {str(child.cget("text")) for child in _descendants(popup) if isinstance(child, tk.Label)}
        assert {original.booking_id, date.today().strftime("%d/%m/%Y")} <= labels
        assert instance.active_guest_var.get() == original.guest_name
        assert instance.active_room_var.get() == original.room_type
        assert instance.active_checkout_var.get() == original.checkout_date.strftime("%d/%m/%Y")
        assert instance.active_revenue_var.get() == "VND 420,500.00" and instance.active_nights_var.get() == "1"
        assert {child.winfo_name() for child in _descendants(popup) if isinstance(child, desktop.ttk.Button)} == {
            "copy_booking", "close_notification",
        }
        assert instance.active_menu.index("end") == 0
        assert instance.active_menu.entrycget(0, "label") == "Sao chép booking sang Excel"
        assert not root.grab_current()
        _assert_popup_content_contained(popup)
        _screenshot("booking-com-popup-tray.png", popup)
        plays.assert_called_once_with("Booking.com")

        copy.invoke()
        assert root.clipboard_get() == excel_tsv(original)
        assert instance.sound_active and not state.is_acknowledged(original)
        instance.active_menu.invoke(0)
        assert root.clipboard_get() == excel_tsv(original)
        instance.restore_main_window()
        root.update()
        instance.hide_to_tray()
        root.update()
        assert popup.winfo_viewable() and instance.sound_active
        plays.assert_called_once_with("Booking.com")

        close.invoke()
        root.update()
        assert instance.active_popup is None and not instance.sound_active
        assert state.is_acknowledged(original) and len(state.history()) == 1
        assert state.register_today_confirmation(original, ("synthetic-repeat",), date.today()) is None
        instance.events.put(("alert", pending))
        instance._drain_events()
        instance._release_pending_alerts()
        assert not instance.alert_queue and not state.pending_for_date(date.today())
        assert instance.active_alert is None and instance.active_popup is None
        plays.assert_called_once_with("Booking.com")
    finally:
        root.destroy()


def test_native_saved_booking_details_show_in_history_and_copy_without_browser_settings(tmp_path, monkeypatch):
    if _run_isolated("test_native_saved_booking_details_show_in_history_and_copy_without_browser_settings"):
        return
    instance, state, plays = _synthetic_app(tmp_path, monkeypatch, legacy=True)
    root = instance.root
    try:
        original = _event(
            "5558000002", guest_name="SYNTHETIC SAVED GUEST",
            room_type="Legacy room x3", checkout_date=date.today() + timedelta(days=2),
            total_revenue="VND 1.000.000", details_loaded_at="legacy",
        )
        pending = state.register_today_confirmation(original, ("synthetic-legacy",), date.today())
        instance.enqueue_alert(pending)
        root.update()
        assert instance.active_guest_var.get() == original.guest_name
        assert instance.active_room_var.get() == original.room_type
        popup_action_buttons(instance.active_popup)[1].invoke()
        root.update()
        instance.restore_main_window()
        root.update()
        row = instance.history_tree.get_children()[0]
        assert instance.history_tree.item(row, "values") == (
            "Booking.com", original.booking_id, original.guest_name, original.room_type,
            date.today().strftime("%d/%m/%Y"), "VND 1,000,000.00",
        )
        instance.history_tree.selection_set(row)
        instance.history_tree.focus(row)
        instance.copy_selected_history()
        assert root.clipboard_get() == excel_tsv(original)
        monkeypatch.setattr(instance.history_menu, "tk_popup", Mock())
        monkeypatch.setattr(instance.history_menu, "grab_release", Mock())
        y = instance.history_tree.bbox(row)[1] + 8
        instance.show_history_context_menu(SimpleNamespace(y=y, x_root=50, y_root=50))
        assert instance.history_menu.index("end") == 0
        assert instance.history_menu.entrycget(0, "label") == "Sao chép dòng đã chọn sang Excel"
        instance.history_menu.invoke(0)
        assert root.clipboard_get() == excel_tsv(original)

        instance.open_settings()
        root.update()
        controls = list(_descendants(instance.settings_window))
        assert not any(child.winfo_name() in {"booking_com_manual", "booking_com_background", "booking_com_login"}
                       for child in controls)
        labels = " ".join(str(child.cget("text")) for child in controls
                          if isinstance(child, (tk.Label, desktop.ttk.Label, desktop.ttk.Button, desktop.ttk.Checkbutton)))
        assert not any(text in labels for text in ("Nhập/dán", "Đăng nhập Booking.com", "Ẩn trình duyệt", "Lấy lại chi tiết"))
        assert "Booking.com" in instance.source_sound_entries
        assert not hasattr(instance, "booking_com_worker") and not hasattr(desktop, "BookingComWorker")
        plays.assert_called_once_with("Booking.com")
        webbrowser.open.assert_not_called()
    finally:
        root.destroy()


def test_native_booking_com_quiet_hours_resume_and_today_filter(tmp_path, monkeypatch):
    if _run_isolated("test_native_booking_com_quiet_hours_resume_and_today_filter"):
        return
    instance, state, plays = _synthetic_app(tmp_path, monkeypatch)
    root = instance.root
    try:
        for delta in (-1, 1):
            instance.enqueue_alert(_event(f"555800001{delta + 1}", checkin=date.today() + timedelta(days=delta)))
        assert instance.active_popup is None and not instance.alert_queue
        plays.assert_not_called()
        today = _event("5558000003")
        pending = state.register_today_confirmation(today, ("synthetic-quiet",), date.today())
        monkeypatch.setattr(instance, "_quiet_hours_active", lambda: True)
        instance.enqueue_alert(pending)
        root.update()
        assert instance.active_popup is None and len(state.pending_for_date(date.today())) == 1
        assert not state.is_acknowledged(today)
        plays.assert_not_called()
        monkeypatch.setattr(instance, "_quiet_hours_active", lambda: False)
        instance._release_pending_alerts()
        root.update()
        assert instance.active_popup.winfo_viewable() and root.state() == "withdrawn"
        assert instance.active_alert.booking_id == today.booking_id
        plays.assert_called_once_with("Booking.com")
        popup_action_buttons(instance.active_popup)[1].invoke()
        instance._release_pending_alerts()
        assert state.is_acknowledged(today) and not state.pending_for_date(date.today())
        assert instance.active_popup is None and not instance.sound_active
        plays.assert_called_once_with("Booking.com")
    finally:
        root.destroy()

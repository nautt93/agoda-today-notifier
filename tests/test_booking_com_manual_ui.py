"""Exercise the real manual Booking.com controls without accounts or browser I/O."""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_booking_popup_windows import _assert_popup_content_contained, _screenshot

import app as desktop
from app import BookingNotifierApp
from booking_notifier.booking_com import DETAIL_PATH, canonical_details_url
from booking_notifier.config import ConfigStore
from booking_notifier.excel_export import excel_tsv
from booking_notifier.models import BookingEvent
from booking_notifier.popup_state import booking_com_details_ready
from booking_notifier.state import StateStore

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" and os.name != "nt", reason="Native desktop Tk regression on macOS and Windows",
)


def _run_isolated(node: str) -> bool:
    # One Tcl interpreter per process matches a real application launch and
    # avoids Windows retaining a previous test's destroyed root.
    if os.environ.get("BOOKING_MANUAL_NATIVE_CHILD") == node:
        return False
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", f"{Path(__file__).resolve()}::{node}"],
        env={**os.environ, "BOOKING_MANUAL_NATIVE_CHILD": node}, check=True, timeout=90,
    )
    return True


def _widget(parent, name):
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)

    matching = [widget for widget in descendants(parent) if widget.winfo_name() == name]
    assert len(matching) == 1, f"Expected one actual {name} control, found {len(matching)}"
    return matching[0]


def _synthetic_app(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.json")
    # Leave the enrichment mode unset so this tests the installed default.
    store.save({
        "f92_enabled": False, "quiet_hours_enabled": False, "start_with_windows": False,
        "start_minimized": False, "update_manifest_source": "", "email_address": "", "password_encrypted": "",
    })
    state = StateStore(tmp_path / "state.json")
    browser_worker, f92_worker, tray = Mock(), Mock(), Mock(available=True)
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(desktop, "ConfigStore", lambda: store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    monkeypatch.setattr(store, "install_source_sound_pack", lambda _directory: store.load())
    monkeypatch.setattr(desktop, "default_source_sound", lambda *_args: tmp_path / "synthetic.wav")
    monkeypatch.setattr(desktop, "BookingComWorker", lambda *_args: browser_worker)
    monkeypatch.setattr(desktop, "F92Worker", lambda *_args: f92_worker)
    monkeypatch.setattr(desktop, "SystemTray", lambda *_args: tray)
    monkeypatch.setattr(desktop, "WindowsMciAudioPlayer", Mock())
    monkeypatch.setattr(desktop.webbrowser, "open", Mock(side_effect=AssertionError("No browser I/O in this test")))
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
    # Tests drain events explicitly. Prevent startup IMAP/OTA/minute timers
    # from introducing unrelated work into the native control workflow.
    monkeypatch.setattr(root, "after", Mock(return_value="synthetic-root-timer"))
    monkeypatch.setattr(root, "after_cancel", Mock())
    instance = BookingNotifierApp(root)
    instance.hide_to_tray()
    root.update()
    assert root.state() == "withdrawn"
    assert instance.config["booking_com_enrichment_mode"] == "manual"
    return instance, state, plays


def _event(booking_id, *, has_link=True):
    return BookingEvent(
        source="Booking.com", booking_id=booking_id, checkin_date=date.today(),
        details_url=canonical_details_url(
            f"https://admin.booking.com{DETAIL_PATH}?res_id={booking_id}&hotel_id=12345&lang=vi",
        ) if has_link else "",
    )


def _fill_form(form, *, total="VND 1.234.000"):
    values = {
        "guest": "NGUYỄN SYNTHETIC FULL GUEST", "room": "Deluxe Double Room x2",
        "checkout": (date.today() + timedelta(days=2)).strftime("%d/%m/%Y"), "total": total,
    }
    for name, value in values.items():
        entry = _widget(form, name)
        entry.delete(0, "end")
        entry.insert(0, value)


def _save_and_drain(instance, form):
    _widget(form, "save_manual_details").invoke()
    instance._drain_events()
    instance.root.update()


def test_native_manual_booking_popup_hidden_main_save_copy_and_no_replay(tmp_path, monkeypatch):
    if _run_isolated("test_native_manual_booking_popup_hidden_main_save_copy_and_no_replay"):
        return
    instance, state, plays = _synthetic_app(tmp_path, monkeypatch)
    root = instance.root
    try:
        original = _event("5558000001")
        pending = state.register_today_confirmation(original, ("synthetic-manual",), date.today())
        instance.enqueue_alert(pending)
        root.update()
        popup, active = instance.active_popup, instance.active_alert
        assert popup.winfo_viewable() and root.state() == "withdrawn"
        assert instance.active_copy_button.instate(["disabled"])
        assert "Nhập chi tiết" in instance.active_booking_details_var.get()
        _assert_popup_content_contained(popup)
        _screenshot("booking-manual-popup-pending.png", popup)
        plays.assert_called_once_with("Booking.com")

        instance.active_mute_button.invoke()
        assert instance.active_sound_muted and not instance.sound_active
        instance.active_manual_button.invoke()
        root.update()
        form = root.nametowidget("booking_manual_details")
        assert form.winfo_viewable() and form.state() == "normal"
        assert root.grab_current() is form
        assert popup.winfo_viewable() and root.state() == "withdrawn"
        _assert_popup_content_contained(form)
        _fill_form(form, total="VND")
        _save_and_drain(instance, form)
        assert form.winfo_exists() and form.winfo_viewable()
        feedback = _widget(form, "manual_feedback")
        assert form.getvar(feedback.cget("textvariable"))
        assert not booking_com_details_ready(state.pending_for_date(date.today())[0])
        assert instance.active_copy_button.instate(["disabled"])
        _assert_popup_content_contained(form)

        _fill_form(form)
        _save_and_drain(instance, form)
        assert not form.winfo_exists() and root.grab_current() is None
        assert instance.active_popup is popup and instance.active_alert is active
        assert booking_com_details_ready(active)
        assert isinstance(active.checkin_date, date) and isinstance(active.checkout_date, date)
        assert active.nights == 2
        assert active.guest_name == "NGUYỄN SYNTHETIC FULL GUEST"
        assert instance.active_guest_var.get() == active.guest_name
        assert instance.active_room_var.get() == active.room_type
        assert instance.active_booking_title_var.get() == "Đã lấy đủ thông tin"
        assert instance.active_copy_button.instate(["!disabled"])
        assert instance.active_sound_muted and not instance.sound_active
        plays.assert_called_once_with("Booking.com")
        assert len(state.pending_for_date(date.today())) == 1 and not state.is_acknowledged(active)
        _assert_popup_content_contained(popup)
        _screenshot("booking-manual-popup-ready.png", popup)

        instance.active_copy_button.invoke()
        copied = root.clipboard_get()
        assert copied == excel_tsv(active) and len(copied.split("\t")) == 9
        assert copied.endswith("Booking.com Deluxe Double Room x2")
        _widget(popup, "close_notification").invoke()
        root.update()
        instance._release_pending_alerts()
        # A repeated short email and late UI delivery must not replay the
        # already acknowledged, manually completed notification.
        assert state.register_today_confirmation(original, ("synthetic-repeat",), date.today()) is None
        instance.events.put(("alert", original))
        instance._drain_events()
        assert instance.active_alert is None and instance.active_popup is None
        assert not instance.alert_queue and not state.pending_for_date(date.today())
        assert state.is_acknowledged(active) and len(state.history()) == 1
        assert state.history()[0]["guest_name"] == active.guest_name
        plays.assert_called_once_with("Booking.com")
    finally:
        root.destroy()


def test_native_manual_repair_closed_no_link_history_preserves_acknowledgement(tmp_path, monkeypatch):
    if _run_isolated("test_native_manual_repair_closed_no_link_history_preserves_acknowledgement"):
        return
    instance, state, plays = _synthetic_app(tmp_path, monkeypatch)
    root = instance.root
    try:
        original = _event("5558000002", has_link=False)
        pending = state.register_today_confirmation(original, ("synthetic-no-link",), date.today())
        instance.enqueue_alert(pending)
        root.update()
        _widget(instance.active_popup, "close_notification").invoke()
        root.update()
        assert state.is_acknowledged(original) and len(state.history()) == 1
        assert not state.pending_for_date(date.today())
        rows = instance.history_tree.get_children()
        assert len(rows) == 1
        instance.history_tree.selection_set(rows[0])
        instance.history_tree.focus(rows[0])
        instance.open_settings()
        root.update()
        _widget(instance.settings_window, "booking_com_manual").invoke()
        root.update()
        form = root.nametowidget("booking_manual_details")
        assert form.winfo_viewable() and root.grab_current() is form
        _fill_form(form)
        _save_and_drain(instance, form)
        assert not form.winfo_exists() and root.grab_current() is None
        saved = BookingEvent.from_dict(state.history()[0])
        assert booking_com_details_ready(saved) and saved.details_url == ""
        assert state.is_acknowledged(saved) and not state.pending_for_date(date.today())
        assert len(state.history()) == 1 and instance.active_popup is None
        instance._release_pending_alerts()
        assert not instance.alert_queue and instance.active_alert is None
        rows = instance.history_tree.get_children()
        instance.history_tree.selection_set(rows[0])
        instance.copy_selected_history()
        copied = root.clipboard_get()
        assert copied == excel_tsv(saved) and len(copied.split("\t")) == 9
        plays.assert_called_once_with("Booking.com")
    finally:
        root.destroy()


def test_native_manual_form_retains_input_when_settings_hide_to_tray(tmp_path, monkeypatch):
    if _run_isolated("test_native_manual_form_retains_input_when_settings_hide_to_tray"):
        return
    instance, state, plays = _synthetic_app(tmp_path, monkeypatch)
    root = instance.root
    try:
        original = _event("5558000003", has_link=False)
        pending = state.register_today_confirmation(original, ("synthetic-form-hide",), date.today())
        instance.enqueue_alert(pending)
        root.update()
        _widget(instance.active_popup, "close_notification").invoke()
        root.update()
        row = instance.history_tree.get_children()[0]
        instance.history_tree.selection_set(row)
        instance.history_tree.focus(row)
        instance.open_settings()
        root.update()
        _widget(instance.settings_window, "booking_com_manual").invoke()
        root.update()
        form = root.nametowidget("booking_manual_details")
        assert form.winfo_viewable() and root.grab_current() is form
        _fill_form(form)
        entered = {name: _widget(form, name).get() for name in ("guest", "room", "checkout", "total")}

        # With a transient settings parent this used to hide the form while
        # retaining its grab, leaving no visible place to finish the edit.
        instance.hide_to_tray()
        root.update()
        assert root.state() == "withdrawn" and not instance.settings_window.winfo_viewable()
        assert instance.booking_com_manual_window is form
        assert form.state() == "normal" and form.winfo_viewable()
        assert root.grab_current() is form
        assert {name: _widget(form, name).get() for name in entered} == entered
        _assert_popup_content_contained(form)

        # Reusing the pending edit must restore this same window, never create
        # another form or reset its entered data.
        form.withdraw()
        root.update()
        instance.open_booking_com_manual_dialog()
        root.update()
        assert instance.booking_com_manual_window is form and form.winfo_viewable()
        assert root.grab_current() is form
        assert {name: _widget(form, name).get() for name in entered} == entered
        # Closing settings directly must not hide a still-owned modal edit.
        instance.open_settings()
        form.transient(instance.settings_window)
        instance.close_settings()
        root.update()
        assert not instance.settings_window.winfo_viewable() and form.winfo_viewable()
        assert root.grab_current() is form
        assert {name: _widget(form, name).get() for name in entered} == entered
        _save_and_drain(instance, form)
        assert not form.winfo_exists() and root.grab_current() is None
        saved = BookingEvent.from_dict(state.history()[0])
        assert booking_com_details_ready(saved) and saved.details_url == ""
        assert saved.guest_name == entered["guest"] and saved.room_type == entered["room"]
        assert state.is_acknowledged(saved) and len(state.history()) == 1
        assert not state.pending_for_date(date.today())
        instance._release_pending_alerts()
        assert instance.active_popup is None and instance.active_alert is None and not instance.alert_queue
        plays.assert_called_once_with("Booking.com")
    finally:
        root.destroy()

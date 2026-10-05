from __future__ import annotations

import os
import subprocess
import sys
import time
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app as desktop
from app import BookingNotifierApp
from booking_notifier.excel_export import excel_tsv
from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore


def test_trip_print_action_is_unsupported_before_credentials_fetch_or_render(monkeypatch):
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.closing = app.print_loading = app.print_spooling = False
    credentials = Mock()
    monkeypatch.setattr(desktop, "unprotect_secret", credentials)
    thread = Mock()
    monkeypatch.setattr(desktop.threading, "Thread", thread)
    alert = BookingEvent(source="Trip", booking_id="1666000000000001", checkin_date=date.today())
    app.request_booking_print(alert)
    app._request_source_print(alert)
    credentials.assert_not_called()
    thread.assert_not_called()
    assert not app.print_loading and not app.print_spooling


def test_trip_late_worker_event_after_acknowledgement_does_not_reopen_popup_or_sound(tmp_path):
    state = StateStore(tmp_path / "state.json")
    event = BookingEvent(source="Trip", booking_id="1666000000000001", checkin_date=date.today(), guest_name="Synthetic Guest")
    late = state.register_today_confirmation(event, ("mail-original",), date.today())
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.state, app.quiet_var = state, SimpleNamespace(get=lambda: False)
    app.queued_ids, app.alert_queue, app.active_alert = set(), [], None
    app._show_next_alert, app.play_sound, app.f92_worker = Mock(), Mock(), Mock()
    app.enqueue_alert(state.pending_for_date(date.today())[0])
    app._show_next_alert.assert_called_once()
    state.acknowledge(late)
    app.alert_queue.clear()
    app.queued_ids.clear()
    app.state = StateStore(state.path)
    app.enqueue_alert(late)
    app.enqueue_alert(late)
    assert not app.alert_queue and not app.queued_ids and app._show_next_alert.call_count == 1
    app.play_sound.assert_not_called()
    assert len(app.state.history()) == 1 and not app.state.pending_for_date(date.today())


def test_trip_original_repair_updates_active_popup_excel_net_without_replaying_audio(tmp_path):
    state = StateStore(tmp_path / "state.json")
    reminder = BookingEvent(source="Trip", booking_id="1666000000000001", checkin_date=date.today(),
                            checkout_date=date.today() + timedelta(days=2), guest_name="SYNTHETIC/GUEST",
                            room_type="Deluxe Double Room x2", total_revenue="VND 1,000,000.00",
                            subject="[Reminder] Trip.com New Reservation: 1666000000000001")
    stale_popup = state.register_today_confirmation(reminder, ("reminder",), date.today())
    original = BookingEvent(source="Trip", booking_id=reminder.booking_id, checkin_date=date.today(),
                            checkout_date=reminder.checkout_date, guest_name=reminder.guest_name,
                            room_type="Deluxe Double Room - Room Only x2", total_revenue="VND 800,000.00",
                            subject="Urgent action required - new booking received (booking no. #1666000000000001#)")
    assert state.register_today_confirmation(original, ("original",), date.today()) is None
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.state, app.alert_queue, app.active_alert = state, [], stale_popup
    app.active_guest_var = app.active_room_var = app.active_revenue_var = Mock()
    app.active_checkout_var = app.active_nights_var = Mock()
    app.f92_worker, app.play_sound, app.stop_sound = Mock(), Mock(), Mock()
    app.sound_active = True
    app._refresh_pending_details()
    assert app.active_alert.total_revenue == original.total_revenue
    assert app.active_alert.room_type == original.room_type and app.active_alert.subject == original.subject
    assert excel_tsv(app.active_alert).split("\t")[5] == "800000"
    assert app.sound_active and not state.is_acknowledged(original)
    app.play_sound.assert_not_called()
    app.stop_sound.assert_not_called()


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


@pytest.mark.skipif(os.name != "nt", reason="Real Windows Trip popup, source MP3, tray, clipboard and no-print menus")
def test_trip_popup_hidden_main_has_only_two_large_buttons_copy_keeps_exact_mp3_and_close_acknowledges(tmp_path, monkeypatch):
    if os.environ.get("BOOKING_TRIP_UI_CHILD") != "1":
        subprocess.run([sys.executable, "-m", "pytest", "-q",
                        f"{Path(__file__).resolve()}::test_trip_popup_hidden_main_has_only_two_large_buttons_copy_keeps_exact_mp3_and_close_acknowledges"],
                       env={**os.environ, "BOOKING_TRIP_UI_CHILD": "1"}, check=True, timeout=60)
        return
    import winsound

    from booking_notifier.audio import TRIP_SOUND_PACK, WindowsMciAudioPlayer
    from booking_notifier.config import ConfigStore

    store = ConfigStore(tmp_path / "config.json")
    store.save({"f92_enabled": False, "quiet_hours_enabled": False, "start_with_windows": False, "update_manifest_source": ""})
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(desktop, "ConfigStore", lambda: store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    send = Mock()
    monkeypatch.setattr(WindowsMciAudioPlayer, "_send", send)
    monkeypatch.setattr(winsound, "PlaySound", Mock())
    monkeypatch.setattr(winsound, "MessageBeep", Mock())
    root = tk.Tk()
    app = BookingNotifierApp(root)
    try:
        deadline = time.monotonic() + 8
        while not app.tray.available and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert app.tray.available and app.tray.icon.visible
        assert set(app.source_sound_vars) == {"Agoda", "Expedia", "Traveloka", "Trip"}
        assert app.config["trip_sound_pack"] == store.load()["trip_sound_pack"] == TRIP_SOUND_PACK
        path = Path(app.config["trip_sound_file"])
        assert path.name == "4-trip.mp3" and path.is_file()
        app.on_close()
        root.update()
        assert root.state() == "withdrawn"
        event = BookingEvent(source="Trip", booking_id="1666000000000001", guest_name="TRẦN/LINH", checkin_date=date.today(),
                             checkout_date=date.today() + timedelta(days=2), room_type="Deluxe Double Room x2", total_revenue="VND 800,000.00")
        app.enqueue_alert(state.register_today_confirmation(event, ("mail-original",), date.today()))
        root.update()
        popup = app.active_popup
        assert popup.winfo_viewable() and root.state() == "withdrawn" and app.sound_active and app.sound_uses_file
        opens = [call.args[0] for call in send.call_args_list if call.args[0].startswith("open ")]
        assert len(opens) == 1 and "4-trip.mp3" in opens[0]
        assert send.call_args.args == (f"play {WindowsMciAudioPlayer.ALIAS} repeat",)
        buttons = [child for child in descendants(popup) if isinstance(child, ttk.Button)]
        assert [button.cget("text") for button in buttons] == ["Sao chép", "Đóng thông báo"]
        assert all(button.winfo_viewable() and button.winfo_height() >= 96 and button.winfo_width() >= 240 for button in buttons)
        assert not any(button.winfo_name().startswith("print_") for button in buttons)
        assert app.active_menu.index("end") == 0 and "Sao chép" in app.active_menu.entrycget(0, "label")
        copy, close = buttons
        copy.invoke()
        assert root.clipboard_get() == excel_tsv(event) and len(root.clipboard_get().split("\t")) == 9
        assert root.clipboard_get().endswith("Trip Deluxe Double Room x2")
        assert app.sound_active and not state.is_acknowledged(event)
        assert [call.args[0] for call in send.call_args_list if call.args[0].startswith("open ")] == opens
        screenshot_dir = os.environ.get("BOOKING_UI_SCREENSHOT_DIR")
        if screenshot_dir:
            from PIL import ImageGrab

            target = Path(screenshot_dir)
            target.mkdir(parents=True, exist_ok=True)
            x, y = popup.winfo_rootx(), popup.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + popup.winfo_width(), y + popup.winfo_height())).save(target / "trip-popup-no-print.png")
        close.invoke()
        root.update()
        assert state.is_acknowledged(event) and len(state.history()) == 1 and not state.pending_for_date(date.today())
        assert app.active_popup is None and not app.sound_active
        app.events.put(("alert", event))
        app._drain_events()
        root.update()
        assert app.active_popup is None and not app.sound_active
        assert [call.args[0] for call in send.call_args_list if call.args[0].startswith("open ")] == opens
        app.restore_main_window()
        root.update()
        row = app.history_tree.get_children()[0]
        monkeypatch.setattr(app.history_menu, "tk_popup", Mock())
        monkeypatch.setattr(app.history_menu, "grab_release", Mock())
        app.show_history_context_menu(SimpleNamespace(y=app.history_tree.bbox(row)[1] + 8, x_root=50, y_root=50))
        assert app.history_menu.index("end") == 0
        assert app.history_menu.entrycget(0, "label") == "Sao chép dòng đã chọn sang Excel"
        app.open_settings()
        root.update()
        assert "Trip" in app.source_sound_entries and app.source_sound_entries["Trip"].winfo_exists()
        assert app.sound_preview_buttons["Trip"].cget("text") == "Nghe thử Trip"
    finally:
        app.exit_app()

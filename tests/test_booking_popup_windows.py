"""Native Booking.com pending/mute/ready popup regression; no browser/account I/O."""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import ttk
from unittest.mock import Mock

import pytest

import app as desktop
from app import BookingNotifierApp
from booking_notifier.audio import WindowsMciAudioPlayer
from booking_notifier.booking_com import DETAIL_PATH, canonical_details_url
from booking_notifier.booking_com_browser import LOGIN_REQUIRED
from booking_notifier.config import ConfigStore
from booking_notifier.excel_export import excel_tsv
from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows popup/tray/clipboard and source audio routing")


def _basic_event():
    return BookingEvent(
        source="Booking.com", booking_id="5550000001", checkin_date=date.today(),
        details_url=canonical_details_url(
            f"https://admin.booking.com{DETAIL_PATH}?res_id=5550000001&hotel_id=12345",
        ),
    )


def _screenshot(name, window):
    directory = os.environ.get("UI_SMOKE_ARTIFACT_DIR")
    if not directory:
        return
    from PIL import ImageGrab

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    window.update_idletasks()
    x, y = window.winfo_rootx(), window.winfo_rooty()
    ImageGrab.grab(bbox=(x, y, x + window.winfo_width(), y + window.winfo_height())).save(target / name)


def _play_commands(send):
    return [call.args[0] for call in send.call_args_list if call.args[0].startswith("play ")]


def _assert_popup_content_contained(popup):
    """Every displayed text/control must fit itself, its parent and the popup."""
    popup.update_idletasks()
    popup_bounds = (popup.winfo_rootx(), popup.winfo_rooty(),
                    popup.winfo_rootx() + popup.winfo_width(), popup.winfo_rooty() + popup.winfo_height())

    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)

    controls = [child for child in descendants(popup)
                if isinstance(child, (tk.Label, ttk.Label, tk.Button, ttk.Button))]
    assert controls, "Synthetic popup must contain actual labels and controls"
    for widget in controls:
        identity = str(widget)
        assert widget.winfo_viewable(), f"Hidden/clipped popup control: {identity}"
        assert widget.winfo_height() >= widget.winfo_reqheight(), f"Vertically cropped content: {identity}"
        assert widget.winfo_width() >= widget.winfo_reqwidth(), f"Horizontally cropped content: {identity}"
        bounds = (widget.winfo_rootx(), widget.winfo_rooty(),
                  widget.winfo_rootx() + widget.winfo_width(), widget.winfo_rooty() + widget.winfo_height())
        parent = widget.master
        parent_bounds = (parent.winfo_rootx(), parent.winfo_rooty(),
                         parent.winfo_rootx() + parent.winfo_width(), parent.winfo_rooty() + parent.winfo_height())
        for container, label in ((popup_bounds, "popup"), (parent_bounds, "parent")):
            assert bounds[0] >= container[0] and bounds[1] >= container[1], f"Content starts outside {label}: {identity}"
            assert bounds[2] <= container[2] and bounds[3] <= container[3], f"Content extends outside {label}: {identity}"


def test_native_booking_pending_mute_in_place_ready_copy_and_next_source(tmp_path, monkeypatch):
    if os.environ.get("BOOKING_POPUP_NATIVE_CHILD") != "1":
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q",
             f"{Path(__file__).resolve()}::test_native_booking_pending_mute_in_place_ready_copy_and_next_source"],
            env={**os.environ, "BOOKING_POPUP_NATIVE_CHILD": "1"}, check=True, timeout=90,
        )
        return
    import winsound

    from test_ui_smoke import popup_action_buttons, wait_for_tray

    store = ConfigStore(tmp_path / "config.json")
    store.save({
        "f92_enabled": False, "quiet_hours_enabled": False, "start_with_windows": False,
        "start_minimized": False, "update_manifest_source": "", "email_address": "", "password_encrypted": "",
        "booking_com_enrichment": True,
    })
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(desktop, "ConfigStore", lambda: store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    # Enrichment is simulated through StateStore; never start a real browser or
    # IMAP connection, and don't let a delayed OTA timer issue network requests.
    monkeypatch.setattr(desktop.BookingComWorker, "start", lambda _worker: None)
    monkeypatch.setattr(desktop.BookingNotifierApp, "start_monitoring", Mock())
    monkeypatch.setattr(desktop.BookingNotifierApp, "check_for_updates", Mock())
    play, beep, send = Mock(), Mock(), Mock()
    monkeypatch.setattr(WindowsMciAudioPlayer, "_send", send)
    monkeypatch.setattr(winsound, "PlaySound", play)
    monkeypatch.setattr(winsound, "MessageBeep", beep)
    notice = Mock()
    monkeypatch.setattr(desktop.messagebox, "showinfo", notice)
    root = tk.Tk()
    app = BookingNotifierApp(root)
    app.f92_worker.notify = Mock()
    try:
        wait_for_tray(app)
        app.hide_to_tray()
        root.update()
        event = _basic_event()
        app.enqueue_alert(state.register_today_confirmation(event, ("synthetic-booking",), date.today()))
        root.update()
        popup, active = app.active_popup, app.active_alert
        copy, close = popup_action_buttons(popup)
        assert app.active_copy_button is copy and copy.instate(["disabled"])
        assert app.active_guest_var.get() == "Booking mới đã nhận"
        assert app.active_booking_title_var.get() == "Vui lòng đợi lấy thông tin"
        assert "app đang lấy họ tên đầy đủ" in app.active_booking_details_var.get().casefold()
        assert app.sound_active and not app.active_sound_muted and len(_play_commands(send)) == 1
        assert not state.is_acknowledged(active) and len(state.pending_for_date(date.today())) == 1
        mute = app.active_mute_button
        assert mute.cget("text") == "Tắt chuông" and mute.winfo_viewable()
        assert mute.winfo_height() < copy.winfo_height()
        ancestor = mute.master
        while ancestor is not popup:
            assert ancestor is not copy.master, "Inline mute must remain outside the two-big-actions frame"
            ancestor = ancestor.master
        assert [child.winfo_name() for child in copy.master.winfo_children()] == ["copy_booking", "close_notification"]
        _assert_popup_content_contained(popup)
        _screenshot("booking-popup-pending.png", popup)

        root.clipboard_clear()
        root.clipboard_append("synthetic-existing-clipboard")
        root.update_idletasks()
        copy.invoke()
        popup.focus_force()
        popup.event_generate("<Control-c>")
        root.update()
        assert root.clipboard_get() == "synthetic-existing-clipboard"
        notice.assert_not_called()
        assert app.active_popup is popup and app.sound_active and not state.is_acknowledged(active)
        _assert_popup_content_contained(popup)

        # A global ready status for another reservation must not fake readiness
        # or put the unrelated booking's ID/name into this popup.
        app.events.put(("booking_com_status", "Booking.com 5550000002: đã bổ sung họ tên/hạng phòng/Excel; không báo lặp."))
        app._drain_events()
        root.update()
        assert copy.instate(["disabled"]) and app.active_guest_var.get() == "Booking mới đã nhận"
        assert app.active_booking_title_var.get() == "Vui lòng đợi lấy thông tin"
        assert "5550000002" not in app.active_booking_details_var.get()
        _assert_popup_content_contained(popup)
        app.events.put(("booking_com_status", LOGIN_REQUIRED))
        app._drain_events()
        root.update()
        assert copy.instate(["disabled"]) and "đăng nhập" in app.active_booking_details_var.get().casefold()
        assert app.active_popup is popup and app.active_alert is active
        _assert_popup_content_contained(popup)
        _screenshot("booking-popup-login.png", popup)
        app.events.put(("booking_com_status", "Booking.com: chưa lấy được chi tiết; thử Đăng nhập Booking.com trong Cài đặt."))
        app._drain_events()
        root.update()
        assert copy.instate(["disabled"])
        assert any(term in app.active_booking_details_var.get().casefold() for term in ("thử lại", "đăng nhập"))
        _assert_popup_content_contained(popup)
        app.events.put(("booking_com_status", f"Booking.com: đang lấy chi tiết {event.booking_id}…"))
        app._drain_events()
        root.update()
        assert app.active_booking_title_var.get() == "Vui lòng đợi lấy thông tin"
        _assert_popup_content_contained(popup)

        next_event = BookingEvent(
            source="Agoda", booking_id="5550000003", checkin_date=date.today(),
            guest_name="NEXT SYNTHETIC GUEST", room_type="Superior Room x1",
            checkout_date=date.today() + timedelta(days=1), total_revenue="VND 200.000",
        )
        app.enqueue_alert(state.register_today_confirmation(next_event, ("synthetic-agoda",), date.today()))
        queued_ids = set(app.queued_ids)
        before_mute = send.call_count
        mute.invoke()
        root.update()
        assert app.active_popup is popup and popup.winfo_viewable() and app.active_alert is active
        assert app.active_sound_muted and not app.sound_active
        assert mute.cget("text") == "Đã tắt chuông" and mute.instate(["disabled"])
        assert send.call_count > before_mute and len(_play_commands(send)) == 1
        assert send.call_args.args == (f"close {WindowsMciAudioPlayer.ALIAS}",)
        assert play.call_args.args == (None, 0)
        assert app.sound_repeat_job is None and app.sound_preview_job is None
        assert not state.is_acknowledged(active) and not state.history()
        assert app.queued_ids == queued_ids and len(app.alert_queue) == 1
        assert len(state.pending_for_date(date.today())) == 2
        _assert_popup_content_contained(popup)
        _screenshot("booking-popup-muted.png", popup)
        muted_calls = send.call_count

        full = BookingEvent.from_dict({
            **event.to_dict(), "guest_name": "NGUYỄN SYNTHETIC FULL GUEST",
            "room_type": "Deluxe Double Room x2; Triple City View x1",
            "checkout_date": (date.today() + timedelta(days=2)).isoformat(),
            "total_revenue": "VND 1.200.000", "details_loaded_at": "2026-10-07T12:00:00",
        })
        assert state.enrich_booking_com(full, date.today())
        app.events.put(("history_changed", None))
        app._drain_events()
        root.update()
        assert app.active_popup is popup and app.active_alert is active
        assert app.active_guest_var.get() == full.guest_name and app.active_room_var.get() == full.room_type
        assert copy.instate(["!disabled"]) and app.active_copy_button is copy
        assert app.active_sound_muted and not app.sound_active and send.call_count == muted_calls
        assert not state.is_acknowledged(active) and len(app.alert_queue) == 1
        assert app.f92_worker.notify.call_args.kwargs == {"play_sound": False}
        assert app.active_booking_title_var.get() == "Đã lấy đủ thông tin"
        _assert_popup_content_contained(popup)
        _screenshot("booking-popup-ready.png", popup)

        app.events.put(("booking_com_status", LOGIN_REQUIRED))  # Stale global login state for another reservation.
        app._drain_events()
        root.update()
        assert copy.instate(["!disabled"]) and "đăng nhập" not in app.active_booking_details_var.get().casefold()
        assert app.active_sound_muted and send.call_count == muted_calls
        _assert_popup_content_contained(popup)
        app.hide_to_tray()
        root.update()
        assert root.state() == "withdrawn" and popup.winfo_viewable()
        _assert_popup_content_contained(popup)
        app.restore_main_window()
        root.update()
        assert popup.winfo_viewable() and app.active_sound_muted and not app.sound_active
        assert send.call_count == muted_calls
        _assert_popup_content_contained(popup)

        # A small desktop and unusually long multi-room text must scroll only
        # the detail card; the fixed status/actions stay visible and audio
        # state, queue and the original model are unaffected.
        canvas = popup.booking_detail_canvas
        with monkeypatch.context() as small_desktop:
            small_desktop.setattr(popup, "winfo_screenheight", lambda: 768)
            app.active_room_var.set("; ".join(
                f"Synthetic Long Room Category {index:02d} With River View And Extra Beds x1"
                for index in range(1, 26)
            ))
            app._fit_popup_guest_header()
            root.update()
            assert popup.winfo_height() <= 696
            overflow_copy, overflow_close = popup_action_buttons(popup)
            assert overflow_copy is copy and overflow_close is close
            content_bounds = canvas.bbox("all")
            assert content_bounds and content_bounds[3] - content_bounds[1] > canvas.winfo_height()
            canvas.yview_moveto(0)
            root.update()
            initial_view = canvas.yview()
            canvas.event_generate("<MouseWheel>", delta=-120)
            root.update()
            assert canvas.yview()[0] > initial_view[0], "Mouse wheel must scroll overflowing booking details"
            canvas.yview_moveto(1)
            root.update()
            assert canvas.yview()[0] > initial_view[0] and canvas.yview()[1] == pytest.approx(1)
            popup_action_buttons(popup)
            assert app.active_sound_muted and not app.sound_active and send.call_count == muted_calls
            assert not state.is_acknowledged(active) and len(app.alert_queue) == 1
            assert active.room_type == full.room_type  # Only the displayed stress text was changed.
            # Scrollable content intentionally extends outside its viewport;
            # baseline all-label containment does not apply during overflow.
            _screenshot("booking-popup-long-rooms.png", popup)
        app.active_room_var.set(full.room_type)
        root.update_idletasks()
        app._fit_popup_guest_header()
        root.update()
        canvas.yview_moveto(0)
        root.update()
        assert app.active_room_var.get() == full.room_type and active.room_type == full.room_type
        assert app.active_sound_muted and not app.sound_active and send.call_count == muted_calls
        _assert_popup_content_contained(popup)
        copy.invoke()
        assert root.clipboard_get() == excel_tsv(full) and len(root.clipboard_get().split("\t")) == 9
        assert app.active_popup is popup and app.active_sound_muted and not app.sound_active
        assert send.call_count == muted_calls and not state.is_acknowledged(active)
        notice.assert_not_called()
        _assert_popup_content_contained(popup)
        close.invoke()
        root.update()
        assert state.is_acknowledged(full) and len(state.history()) == 1
        assert app.active_alert.storage_id == next_event.storage_id and app.active_popup is not popup
        assert app.sound_active and not app.active_sound_muted and len(_play_commands(send)) == 2
        opened = [call.args[0] for call in send.call_args_list if call.args[0].startswith("open ")]
        assert len(opened) == 2 and "5-booking-com.mp3" in opened[0] and "1-agoda.mp3" in opened[1]
        assert getattr(app, "active_mute_button", None) is None
        next_copy, next_close = popup_action_buttons(app.active_popup)
        assert next_copy.instate(["!disabled"]) and app.active_menu.index("end") == 0
        _assert_popup_content_contained(app.active_popup)
        app.mute_active_alert()  # Booking.com-only method must not silence Agoda.
        assert app.sound_active and len(_play_commands(send)) == 2
        next_close.invoke()
        root.update()
        assert app.active_popup is None and not app.sound_active and not state.pending_for_date(date.today())
        assert len(state.history()) == 2
        beep.assert_not_called()
    finally:
        app.exit_app()

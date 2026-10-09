"""Native Booking.com code popup and the next provider retain their audio routes."""
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
from booking_notifier.config import ConfigStore
from booking_notifier.excel_export import excel_tsv
from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows popup/tray/clipboard and source audio routing")


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


def test_native_booking_code_copy_close_and_next_source_audio(tmp_path, monkeypatch):
    if os.environ.get("BOOKING_POPUP_NATIVE_CHILD") != "1":
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q",
             f"{Path(__file__).resolve()}::test_native_booking_code_copy_close_and_next_source_audio"],
            env={**os.environ, "BOOKING_POPUP_NATIVE_CHILD": "1"}, check=True, timeout=90,
        )
        return
    import winsound

    from test_ui_smoke import popup_action_buttons, wait_for_tray

    store = ConfigStore(tmp_path / "config.json")
    store.save({
        "f92_enabled": False, "quiet_hours_enabled": False, "start_with_windows": False,
        "start_minimized": False, "update_manifest_source": "", "email_address": "", "password_encrypted": "",
        "booking_com_enrichment": True, "booking_com_enrichment_mode": "visible",
    })
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(desktop, "ConfigStore", lambda: store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    monkeypatch.setattr(BookingNotifierApp, "start_monitoring", Mock())
    monkeypatch.setattr(BookingNotifierApp, "check_for_updates", Mock())
    play, beep, send = Mock(), Mock(), Mock()
    monkeypatch.setattr(WindowsMciAudioPlayer, "_send", send)
    monkeypatch.setattr(winsound, "PlaySound", play)
    monkeypatch.setattr(winsound, "MessageBeep", beep)
    root = tk.Tk()
    instance = BookingNotifierApp(root)
    try:
        wait_for_tray(instance)
        instance.hide_to_tray()
        root.update()
        event = BookingEvent(source="Booking.com", booking_id="5550000001", checkin_date=date.today())
        instance.enqueue_alert(state.register_today_confirmation(event, ("synthetic-booking",), date.today()))
        root.update()
        popup = instance.active_popup
        copy, close = popup_action_buttons(popup)
        assert popup.winfo_viewable() and root.state() == "withdrawn"
        assert copy.instate(["!disabled"]) and copy.cget("text") == "Sao chép mã"
        assert instance.sound_active and len(_play_commands(send)) == 1
        opens = [call.args[0] for call in send.call_args_list if call.args[0].startswith("open ")]
        assert len(opens) == 1 and "5-booking-com.mp3" in opens[0]
        assert instance.active_menu.index("end") == 0
        assert instance.active_menu.entrycget(0, "label") == "Sao chép mã đặt phòng"
        _assert_popup_content_contained(popup)
        _screenshot("booking-code-popup-windows.png", popup)
        copy.invoke()
        assert root.clipboard_get() == event.booking_id
        assert instance.sound_active and not state.is_acknowledged(event)

        following = BookingEvent(
            source="Agoda", booking_id="5550000002", checkin_date=date.today(),
            guest_name="NEXT SYNTHETIC GUEST", room_type="Superior Room x1",
            checkout_date=date.today() + timedelta(days=1), total_revenue="VND 200.000",
        )
        instance.enqueue_alert(state.register_today_confirmation(following, ("synthetic-agoda",), date.today()))
        assert len(instance.alert_queue) == 1 and len(_play_commands(send)) == 1
        close.invoke()
        root.update()
        assert state.is_acknowledged(event) and instance.active_alert.storage_id == following.storage_id
        assert instance.active_popup is not popup and instance.active_popup.winfo_viewable()
        assert root.state() == "withdrawn" and instance.sound_active and len(_play_commands(send)) == 2
        opens = [call.args[0] for call in send.call_args_list if call.args[0].startswith("open ")]
        assert len(opens) == 2 and "1-agoda.mp3" in opens[1]
        next_copy, next_close = popup_action_buttons(instance.active_popup)
        assert next_copy.cget("text") == "Sao chép"
        next_copy.invoke()
        assert root.clipboard_get() == excel_tsv(following) and len(root.clipboard_get().split("\t")) == 9
        _assert_popup_content_contained(instance.active_popup)
        _screenshot("agoda-popup-after-booking-code.png", instance.active_popup)
        next_close.invoke()
        root.update()
        assert instance.active_popup is None and not instance.sound_active
        assert not state.pending_for_date(date.today()) and len(state.history()) == 2
        assert send.call_args.args == (f"close {WindowsMciAudioPlayer.ALIAS}",)
        beep.assert_not_called()
    finally:
        instance.exit_app()

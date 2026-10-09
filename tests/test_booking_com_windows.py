"""The simplified Windows Booking.com alert keeps the supplied MP3 and code copy."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tkinter as tk
import webbrowser
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app as desktop
from app import BookingNotifierApp
from booking_notifier.audio import BOOKING_COM_SOUND_PACK, SOURCE_SOUND_SHA256, WindowsMciAudioPlayer
from booking_notifier.config import ConfigStore
from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows Booking.com source MP3/tray/clipboard")


def test_native_code_popup_uses_supplied_mp3_and_legacy_settings_do_not_start_browser(tmp_path, monkeypatch):
    if os.environ.get("BOOKING_COM_UI_CHILD") != "1":
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q",
             f"{Path(__file__).resolve()}::test_native_code_popup_uses_supplied_mp3_and_legacy_settings_do_not_start_browser"],
            env={**os.environ, "BOOKING_COM_UI_CHILD": "1"}, check=True, timeout=60,
        )
        return
    import winsound

    from test_booking_popup_windows import _screenshot
    from test_ui_smoke import popup_action_buttons, wait_for_tray

    store = ConfigStore(tmp_path / "config.json")
    store.save({
        "f92_enabled": False, "quiet_hours_enabled": False, "start_with_windows": False,
        "start_minimized": False, "update_manifest_source": "", "email_address": "", "password_encrypted": "",
        "booking_com_enrichment": True, "booking_com_enrichment_mode": "visible", "booking_com_browser": "chrome",
    })
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(desktop, "ConfigStore", lambda: store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    monkeypatch.setattr(BookingNotifierApp, "start_monitoring", Mock())
    monkeypatch.setattr(BookingNotifierApp, "check_for_updates", Mock())
    monkeypatch.setattr(webbrowser, "open", Mock(side_effect=AssertionError("No browser for code alerts")))
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
        event = BookingEvent(source="Booking.com", booking_id="5550000003", checkin_date=date.today())
        instance.enqueue_alert(state.register_today_confirmation(event, ("synthetic-sound",), date.today()))
        root.update()
        popup = instance.active_popup
        copy, close = popup_action_buttons(popup)
        assert popup.winfo_viewable() and root.state() == "withdrawn" and instance.sound_active
        assert instance.sound_uses_file
        sound = Path(instance.config["booking_com_sound_file"])
        assert sound.name == "5-booking-com.mp3"
        assert hashlib.sha256(sound.read_bytes()).hexdigest() == SOURCE_SOUND_SHA256["booking.com"]
        assert instance.config["booking_com_sound_pack"] == store.load()["booking_com_sound_pack"] == BOOKING_COM_SOUND_PACK
        opens = [call.args[0] for call in send.call_args_list if call.args[0].startswith("open ")]
        assert len(opens) == 1 and "5-booking-com.mp3" in opens[0]
        assert send.call_args.args == (f"play {WindowsMciAudioPlayer.ALIAS} repeat",)
        beep.assert_not_called()
        assert play.call_args.args == (None, 0)
        copy.invoke()
        assert root.clipboard_get() == event.booking_id and instance.sound_active
        close.invoke()
        root.update()
        assert state.is_acknowledged(event) and instance.active_popup is None and not instance.sound_active
        instance.events.put(("alert", event))
        instance._drain_events()
        assert instance.active_popup is None
        instance.restore_main_window()
        root.update()
        row = instance.history_tree.get_children()[0]
        monkeypatch.setattr(instance.history_menu, "tk_popup", Mock())
        monkeypatch.setattr(instance.history_menu, "grab_release", Mock())
        instance.show_history_context_menu(SimpleNamespace(y=instance.history_tree.bbox(row)[1] + 8, x_root=50, y_root=50))
        assert instance.history_menu.index("end") == 0
        assert instance.history_menu.entrycget(0, "label") == "Sao chép mã đặt phòng"
        instance.history_menu.invoke(0)
        assert root.clipboard_get() == event.booking_id
        instance.open_settings()
        root.update()
        assert "Booking.com" in instance.source_sound_entries
        assert not hasattr(instance, "booking_com_worker") and not hasattr(desktop, "BookingComWorker")
        webbrowser.open.assert_not_called()
        _screenshot("booking-code-settings-windows.png", instance.settings_window)
    finally:
        instance.exit_app()

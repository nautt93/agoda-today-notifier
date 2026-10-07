from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app as desktop
from app import BookingNotifierApp
from booking_notifier.audio import BOOKING_COM_SOUND_PACK, SOURCE_SOUND_SHA256, WindowsMciAudioPlayer
from booking_notifier.booking_com import DETAIL_SNAPSHOT_JS, canonical_details_url
from booking_notifier.booking_com_browser import BookingComBrowser, packaged_browser_smoke
from booking_notifier.config import ConfigStore
from booking_notifier.excel_export import excel_tsv
from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Installed Windows Edge/Chrome and native popup/tray/clipboard")


def basic_event():
    return BookingEvent(source="Booking.com", booking_id="5550000001", checkin_date=date.today(),
                        details_url=canonical_details_url("https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/booking.html?res_id=5550000001&hotel_id=12345"))


def test_installed_browser_real_selectors_hidden_print_excluded_full_multiroom_and_persistent_profile(tmp_path):
    client = BookingComBrowser(tmp_path / "dedicated-test-profile")
    event = basic_event()
    markup = (Path(__file__).parent / "fixtures" / "booking_com_details.html").read_text(encoding="utf-8")
    markup = markup.replace("ARRIVAL", date.today().isoformat()).replace("DEPARTURE", (date.today() + timedelta(days=2)).isoformat())
    try:
        client._launch()
        client.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=markup))
        client.context.set_offline(True)
        full = client.fetch(event)
        snapshot = client._page().evaluate(DETAIL_SNAPSHOT_JS)
        assert len(snapshot["fields"]) == 5 and all("Hoa hồng" not in pair[0] for pair in snapshot["fields"])
        assert full.guest_name == "NGUYỄN SYNTHETIC FULL GUEST"
        assert full.room_type == "Deluxe Double Room x2; Triple City View x1" and full.nights == 2
        assert excel_tsv(full).split("\t")[5] == "1200000"
        client._page().evaluate("localStorage.setItem('synthetic-profile-check', 'persisted')")
        client.context.add_cookies([{"name": "synthetic-session", "value": "not-a-real-session", "url": "https://admin.booking.com",
                                     "expires": time.time() + 86400, "httpOnly": True, "secure": True}])
        client.close()
        client._launch()
        client.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=markup))
        client.context.set_offline(True)
        assert client.fetch(event).room_type == full.room_type
        assert client._page().evaluate("localStorage.getItem('synthetic-profile-check')") == "persisted"
        assert any(cookie["name"] == "synthetic-session" for cookie in client.context.cookies())
    finally:
        client.close()


def test_source_driver_smoke_before_freezing(tmp_path):
    report = tmp_path / "report.json"
    result = packaged_browser_smoke(report)
    assert result == 0 and '"ok": true' in report.read_text(encoding="utf-8"), report.read_text(encoding="utf-8")


def test_native_basic_and_enriched_popup_same_window_two_big_actions_no_second_sound_and_exact_excel(tmp_path, monkeypatch):
    if os.environ.get("BOOKING_COM_UI_CHILD") != "1":
        subprocess.run([sys.executable, "-m", "pytest", "-q", f"{Path(__file__).resolve()}::test_native_basic_and_enriched_popup_same_window_two_big_actions_no_second_sound_and_exact_excel"],
                       env={**os.environ, "BOOKING_COM_UI_CHILD": "1"}, check=True, timeout=60)
        return
    import winsound

    from test_ui_smoke import popup_action_buttons, wait_for_tray

    store = ConfigStore(tmp_path / "config.json")
    store.save({"f92_enabled": False, "quiet_hours_enabled": False, "start_with_windows": False, "update_manifest_source": "",
                "booking_com_enrichment": False})  # Synthetic UI test must never contact Extranet.
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(desktop, "ConfigStore", lambda: store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    play, beep, send = Mock(), Mock(), Mock()
    monkeypatch.setattr(WindowsMciAudioPlayer, "_send", send)
    monkeypatch.setattr(winsound, "PlaySound", play)
    monkeypatch.setattr(winsound, "MessageBeep", beep)
    root = tk.Tk()
    app = BookingNotifierApp(root)

    def screenshot(name, window):
        directory = os.environ.get("BOOKING_UI_SCREENSHOT_DIR")
        if directory:
            from PIL import ImageGrab

            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            x, y = window.winfo_rootx(), window.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + window.winfo_width(), y + window.winfo_height())).save(target / name)

    try:
        wait_for_tray(app)
        app.hide_to_tray()
        root.update()
        event = basic_event()
        app.enqueue_alert(state.register_today_confirmation(event, ("original",), date.today()))
        root.update()
        popup = app.active_popup
        copy, close = popup_action_buttons(popup)
        assert popup.winfo_viewable() and root.state() == "withdrawn" and app.sound_active
        assert app.sound_uses_file
        sound = Path(app.config["booking_com_sound_file"])
        assert sound.name == "5-booking-com.mp3"
        assert hashlib.sha256(sound.read_bytes()).hexdigest() == SOURCE_SOUND_SHA256["booking.com"]
        assert app.config["booking_com_sound_pack"] == store.load()["booking_com_sound_pack"] == BOOKING_COM_SOUND_PACK
        opens = [call.args[0] for call in send.call_args_list if call.args[0].startswith("open ")]
        assert len(opens) == 1 and "5-booking-com.mp3" in opens[0]
        assert send.call_args.args == (f"play {WindowsMciAudioPlayer.ALIAS} repeat",)
        beep.assert_not_called()
        assert play.call_args.args == (None, 0)
        assert app.active_menu.index("end") == 0
        assert not any("print_" in child.winfo_name() for child in popup.winfo_children())
        assert app.active_guest_var.get() == "Booking mới đã nhận"
        screenshot("booking-com-basic-popup.png", popup)
        plays = send.call_count
        full = BookingEvent.from_dict({**event.to_dict(), "guest_name": "NGUYỄN SYNTHETIC FULL GUEST", "room_type": "Deluxe Double Room x2; Triple City View x1",
                                     "checkout_date": (date.today() + timedelta(days=2)).isoformat(), "total_revenue": "VND 1.200.000", "details_loaded_at": "2026-10-07T12:00:00"})
        assert state.enrich_booking_com(full, date.today())
        app.events.put(("history_changed", None))
        app._drain_events()
        root.update()
        assert app.active_popup is popup and app.active_guest_var.get() == full.guest_name
        assert app.active_room_var.get() == full.room_type and send.call_count == plays
        assert app.active_guest_label.winfo_height() >= app.active_guest_label.winfo_reqheight()
        assert app.sound_active and not state.is_acknowledged(full)
        screenshot("booking-com-full-popup.png", popup)
        copy.invoke()
        assert root.clipboard_get() == excel_tsv(full) and len(root.clipboard_get().split("\t")) == 9
        assert send.call_count == plays and app.sound_active
        close.invoke()
        root.update()
        assert state.is_acknowledged(full) and len(state.history()) == 1 and app.active_popup is None
        assert send.call_args.args == (f"close {WindowsMciAudioPlayer.ALIAS}",)
        app.events.put(("alert", event))
        app._drain_events()
        root.update()
        assert app.active_popup is None and not app.sound_active
        app.restore_main_window()
        root.update()
        row = app.history_tree.get_children()[0]
        monkeypatch.setattr(app.history_menu, "tk_popup", Mock())
        monkeypatch.setattr(app.history_menu, "grab_release", Mock())
        app.show_history_context_menu(SimpleNamespace(y=app.history_tree.bbox(row)[1] + 8, x_root=50, y_root=50))
        assert app.history_menu.index("end") == 0 and app.history_menu.entrycget(0, "label") == "Sao chép dòng đã chọn sang Excel"
        app.open_settings()
        root.update()
        assert "Booking.com" in app.source_sound_entries
        assert app.booking_com_browser_var.get() == "auto"

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        hide = next(child for child in descendants(app.settings_window)
                    if child.winfo_name() == "booking_com_background")
        assert hide.cget("text") == "Ẩn trình duyệt"
        background = Mock()
        monkeypatch.setattr(app.booking_com_worker, "background", background)
        hide.invoke()
        background.assert_called_once_with()
        assert app.active_popup is None and not app.sound_active
        screenshot("booking-com-settings.png", app.settings_window)
    finally:
        app.exit_app()

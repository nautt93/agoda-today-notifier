from __future__ import annotations

import os
import tkinter as tk
from datetime import date, timedelta

import pytest

from app import BookingNotifierApp
from booking_notifier.models import BookingEvent


@pytest.mark.skipif(os.name != "nt", reason="The packaged desktop app targets Windows")
def test_windows_ui_builds_with_excel_context_menu():
    root = tk.Tk()
    root.withdraw()
    app = BookingNotifierApp(root)
    try:
        root.update_idletasks()
        assert app.history_menu.index("end") == 1
        assert "Sao chép" in app.history_menu.entrycget(0, "label")
        assert app.history_tree.bind("<Button-3>")
        assert app.history_tree.bind("<Control-c>")

        app.config["f92_enabled"] = False
        app.play_sound = lambda: None
        app.enqueue_alert(BookingEvent(
            source="Agoda",
            booking_id="707908051",
            checkin_date=date.today(),
            guest_name="NGUYEN VAN AN",
            room_type="Deluxe Double Room",
        ))
        root.update()
        assert app.active_popup is not None
        assert app.active_popup.winfo_viewable()
        assert bool(app.active_popup.attributes("-topmost"))
        assert root.state() != "iconic"
    finally:
        app.exit_app()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows popup")
@pytest.mark.parametrize("source", ["Agoda", "Expedia"])
def test_email_to_visible_popup_while_minimized_even_if_sound_fails(tmp_path, monkeypatch, source):
    from email.message import EmailMessage

    import app as desktop
    from booking_notifier import mail_monitor
    from booking_notifier.config import ConfigStore
    from booking_notifier.state import StateStore

    config_store = ConfigStore(tmp_path / "config.json")
    config_store.save({"quiet_hours_enabled": False, "f92_enabled": False, "start_with_windows": False})
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "ConfigStore", lambda: config_store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    root = tk.Tk()
    app = BookingNotifierApp(root)
    try:
        root.iconify()

        def broken_sound():
            raise RuntimeError("unsupported audio file")

        app.play_sound = broken_sound
        message = EmailMessage()
        message["From"] = "booking@agoda.com" if source == "Agoda" else "notify@expediapartnercentral.com"
        message["Subject"] = "Booking confirmation"
        message["Message-ID"] = "<popup-smoke@example>"
        message.set_content(f"Booking ID: 707908051 Check-in: {date.today().isoformat()} Guest name: Test Guest")

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def login(self, *args):
                pass

            def select(self, *args, **kwargs):
                return "OK", [b"1"]

            def response(self, *args):
                return "OK", [b"123"]

            def uid(self, command, *args):
                return ("OK", [b"1"]) if command == "search" else ("OK", [(b"BODY[]", message.as_bytes())])

        monkeypatch.setattr(mail_monitor.imaplib, "IMAP4_SSL", FakeClient)
        monitor = mail_monitor.ImapMonitor({**app.config, "imap_host": "example.com"}, "secret", state, app.events)
        monitor.scan_mailbox()
        app._drain_events()
        root.update()
        assert app.active_alert is not None
        assert app.active_alert.source == source
        assert app.active_popup is not None and app.active_popup.winfo_viewable()
        assert bool(app.active_popup.attributes("-topmost"))
        assert root.state() != "iconic"
        app.acknowledge_alert()
        monitor.scan_mailbox()
        app._drain_events()
        assert app.active_popup is None
        app.enqueue_alert(BookingEvent(source=source, booking_id="FUTURE", checkin_date=date.today() + timedelta(days=1)))
        assert app.active_popup is None
    finally:
        app.exit_app()

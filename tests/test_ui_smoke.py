from __future__ import annotations

import os
import tkinter as tk
from datetime import date

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

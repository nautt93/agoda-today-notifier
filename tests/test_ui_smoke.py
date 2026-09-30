from __future__ import annotations

import os
import tkinter as tk

import pytest

from app import BookingNotifierApp


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
    finally:
        app.exit_app()

from __future__ import annotations

import ctypes as ct
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import date, timedelta
from email import policy
from email.message import EmailMessage
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app as desktop
from app import BookingNotifierApp
from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore
from booking_notifier.windows_print import DEVMODE_PREFIX, PrinterJob, _native, set_a4_mode


def sample_print_message():
    message = EmailMessage(policy=policy.default)
    message["From"] = "Traveloka <hotel@traveloka.com>"
    message["Subject"] = "CONFIRMED - Traveloka Itinerary ID 20261234000001 (Test Hotel, VIETNAM)"
    message.set_content((Path(__file__).parent / "fixtures/traveloka_print_test.html").read_text(encoding="utf-8"), subtype="html")
    return message


def print_app():
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.config = {"email_address": "hotel@example.invalid", "password_encrypted": "protected-test"}
    app.closing = app.print_loading = app.print_spooling = False
    app.events, app.set_status = queue.Queue(), Mock()
    return app


def test_traveloka_print_load_freezes_clicked_booking_rejects_repeated_click_and_sanitizes_errors(monkeypatch):
    app = print_app()
    monkeypatch.setattr(desktop, "unprotect_secret", lambda value: "synthetic-password")
    thread = Mock()
    constructor = Mock(return_value=thread)
    monkeypatch.setattr(desktop.threading, "Thread", constructor)
    fetch = Mock(side_effect=RuntimeError("Private dummy card payload 4111111111111111"))
    monkeypatch.setattr(desktop, "fetch_traveloka_print", fetch)
    clicked = BookingEvent(source="Traveloka", booking_id="20261234000001", checkin_date=date(2026, 10, 4))
    app.request_booking_print(clicked)
    assert app.print_loading and fetch.call_count == 0
    thread.start.assert_called_once()
    clicked.booking_id = "OTHER-BOOKING"
    clicked.source = "Expedia"
    constructor.call_args.kwargs["target"]()
    selected = fetch.call_args.args[2]
    assert selected.booking_id == "20261234000001" and selected.source == "Traveloka"
    kind, (selected, image, error) = app.events.get_nowait()
    assert kind in {"booking_print_ready", "expedia_print_ready"}
    assert selected.source == "Traveloka" and image is None
    assert "Private" not in error and "4111" not in error
    app.request_booking_print(clicked)
    assert constructor.call_count == 1


def test_traveloka_transient_render_is_closed_if_app_exits_while_loading(monkeypatch):
    app = print_app()
    monkeypatch.setattr(desktop, "unprotect_secret", lambda value: "synthetic-password")
    thread = Mock()
    constructor = Mock(return_value=thread)
    monkeypatch.setattr(desktop.threading, "Thread", constructor)
    data, image = Mock(), Mock()
    monkeypatch.setattr(desktop, "fetch_traveloka_print", Mock(return_value=data))
    monkeypatch.setattr(desktop, "render_booking_a4", Mock(return_value=image))
    app.request_booking_print(BookingEvent(source="Traveloka", booking_id="20261234000001", checkin_date=date.today()))
    app.closing = True
    constructor.call_args.kwargs["target"]()
    desktop.render_booking_a4.assert_called_once_with(data)
    image.close.assert_called_once()
    assert app.events.empty()


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows Traveloka A4 popup/context/preview/tray integration")
def test_traveloka_popup_print_context_transient_preview_and_tray_are_independent(tmp_path, monkeypatch):
    if os.environ.get("BOOKING_TRAVELOKA_PRINT_UI_CHILD") != "1":
        subprocess.run([sys.executable, "-m", "pytest", "-q",
                        f"{Path(__file__).resolve()}::test_traveloka_popup_print_context_transient_preview_and_tray_are_independent"],
                       env={**os.environ, "BOOKING_TRAVELOKA_PRINT_UI_CHILD": "1"}, check=True, timeout=90)
        return
    from booking_notifier.config import ConfigStore
    from booking_notifier.expedia_print import render_booking_a4
    from booking_notifier.traveloka_print import parse_traveloka_print

    store = ConfigStore(tmp_path / "config.json")
    store.save({"f92_enabled": False, "quiet_hours_enabled": False, "start_with_windows": False, "update_manifest_source": ""})
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(desktop, "ConfigStore", lambda: store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    monkeypatch.setattr(BookingNotifierApp, "play_sound", lambda self: setattr(self, "sound_active", True))
    data = parse_traveloka_print(sample_print_message())
    data.booking.checkin_date = date.today()
    data.booking.checkout_date = date.today() + timedelta(days=2)
    root = tk.Tk()
    app = BookingNotifierApp(root)
    try:
        deadline = time.monotonic() + 8
        while not app.tray.available and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert app.tray.available and app.tray.icon.visible
        state.register_today_confirmation(data.booking, ["test-traveloka-print"], date.today())
        # Show the table independently without marking this live booking acknowledged.
        monkeypatch.setattr(state, "history", lambda: [data.booking.to_dict()])
        app.refresh_history()
        app.on_close()
        root.update()
        app.enqueue_alert(data.booking)
        root.update()
        popup = app.active_popup
        assert popup.winfo_viewable() and root.state() == "withdrawn"
        buttons = [child for child in descendants(popup) if isinstance(child, ttk.Button)]
        print_button = next(button for button in buttons if button.winfo_name() == "print_traveloka")
        assert print_button.cget("text") == "In phiếu Traveloka - 1 trang A4" and print_button.winfo_viewable()
        assert not any(button.winfo_name() == "print_expedia" for button in buttons)
        copy_button = next(button for button in buttons if button.winfo_name() == "copy_booking")
        close_button = next(button for button in buttons if button.winfo_name() == "close_notification")
        assert copy_button.winfo_height() >= 96 and close_button.winfo_height() >= 96
        request = Mock()
        monkeypatch.setattr(app, "request_booking_print", request)
        print_button.invoke()
        request.assert_called_once_with(data.booking)
        assert state.pending_for_date(date.today()) and app.sound_active
        assert app.active_menu.index("end") == 1
        assert app.active_menu.entrycget(1, "label") == "In phiếu Traveloka - 1 trang A4"
        app.active_menu.invoke(1)
        assert request.call_count == 2 and request.call_args.args[0].source == "Traveloka"
        app.restore_main_window()
        root.update()
        row = app.history_tree.get_children()[0]
        row_y = app.history_tree.bbox(row)[1] + 8
        monkeypatch.setattr(app.history_menu, "tk_popup", Mock())
        monkeypatch.setattr(app.history_menu, "grab_release", Mock())
        app.show_history_context_menu(SimpleNamespace(y=row_y, x_root=50, y_root=50))
        assert app.history_menu.index("end") == 1
        assert app.history_menu.entrycget(1, "label") == "In phiếu Traveloka - 1 trang A4"
        app.history_menu.invoke(1)
        assert request.call_args.args[0].storage_id == data.booking.storage_id
        app.on_close()
        root.update()
        # The compatible worker event must route to a Traveloka-labelled preview.
        app.events.put(("expedia_print_ready", (data.booking, render_booking_a4(data), "")))
        app._drain_events()
        root.update()
        preview = app.print_preview
        assert preview.winfo_viewable() and "Traveloka" in preview.title() and root.state() == "withdrawn"
        assert popup.winfo_viewable() and app.sound_active and app.active_alert is data.booking
        assert state.pending_for_date(date.today()) and not state.is_acknowledged(data.booking)
        assert preview.winfo_height() < preview.winfo_screenheight()
        screenshot_dir = os.environ.get("BOOKING_UI_SCREENSHOT_DIR")
        if screenshot_dir:
            from PIL import ImageGrab

            target = Path(screenshot_dir)
            target.mkdir(parents=True, exist_ok=True)
            for window, name in ((popup, "traveloka-popup-print.png"), (preview, "traveloka-a4-preview.png")):
                window.lift()
                root.update()
                x, y = window.winfo_rootx(), window.winfo_rooty()
                ImageGrab.grab(bbox=(x, y, x + window.winfo_width(), y + window.winfo_height())).save(target / name)
        preview_print = next(child for child in descendants(preview)
                             if isinstance(child, ttk.Button) and child.cget("text") == "In 1 trang A4")
        native_ready, allow_cancel = threading.Event(), threading.Event()

        def delayed_cancel(_owner):
            native_ready.set()
            assert allow_cancel.wait(3)
            return None

        chooser = Mock(side_effect=delayed_cancel)
        monkeypatch.setattr(desktop, "choose_printer", chooser)
        preview_print.invoke()
        assert native_ready.wait(1) and app.print_spooling
        copy_button.invoke()
        assert root.clipboard_get().split("\t")[-1].startswith("Traveloka ") and app.sound_active
        app.close_booking_print_preview()
        assert app.print_preview is preview  # The native chooser's owner must stay valid.
        allow_cancel.set()
        deadline = time.monotonic() + 4
        while app.print_spooling and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert chooser.call_count == 1 and not app.print_spooling
        image = app.print_preview_image
        app.close_booking_print_preview()
        root.update()
        assert app.print_preview is None and app.print_preview_image is None and app.print_preview_photo is None
        with pytest.raises(ValueError):
            image.getpixel((0, 0))
        assert root.state() == "withdrawn" and app.active_alert is data.booking and app.sound_active
        copy_button.invoke()
        clipboard = root.clipboard_get()
        assert len(clipboard.split("\t")) == 9 and clipboard.split("\t")[-1].startswith("Traveloka ")
        assert "4111" not in clipboard and "CVV" not in clipboard
        serialized = state.path.read_text(encoding="utf-8")
        assert "4111" not in serialized and "cvv" not in serialized.lower() and "card" not in serialized.lower()
        app.history_rows[row] = BookingEvent(source="Agoda", booking_id="AGODA-TEST").to_dict()
        app.show_history_context_menu(SimpleNamespace(y=row_y, x_root=50, y_root=50))
        assert app.history_menu.index("end") == 0
        close_button.invoke()
        assert not app.sound_active and not state.pending_for_date(date.today())
    finally:
        app.exit_app()


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows virtual PDF print of a synthetic Traveloka A4 page")
def test_traveloka_actual_virtual_pdf_print_is_one_a4_page(tmp_path):
    from booking_notifier.expedia_print import render_booking_a4
    from booking_notifier.traveloka_print import parse_traveloka_print

    gdi, _kernel, _dialog = _native()
    # Never send a test to the default or any physical printer.
    spool = ct.WinDLL("winspool.drv", use_last_error=True)
    spool.OpenPrinterW.argtypes = [ct.c_wchar_p, ct.POINTER(ct.c_void_p), ct.c_void_p]
    spool.OpenPrinterW.restype = ct.c_int32
    spool.ClosePrinter.argtypes = [ct.c_void_p]
    spool.DocumentPropertiesW.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_wchar_p, ct.c_void_p, ct.c_void_p, ct.c_uint32]
    spool.DocumentPropertiesW.restype = ct.c_int32
    handle = ct.c_void_p()
    name = "Microsoft Print to PDF"
    if not spool.OpenPrinterW(name, ct.byref(handle), None):
        pytest.skip("Virtual Microsoft Print to PDF printer unavailable")
    try:
        size = spool.DocumentPropertiesW(None, handle, name, None, None, 0)
        assert size >= ct.sizeof(DEVMODE_PREFIX)
        buffer = ct.create_string_buffer(size)
        assert spool.DocumentPropertiesW(None, handle, name, buffer, None, 2) == 1
        set_a4_mode(DEVMODE_PREFIX.from_buffer(buffer))
        gdi.CreateDCW.argtypes = [ct.c_wchar_p, ct.c_wchar_p, ct.c_wchar_p, ct.c_void_p]
        gdi.CreateDCW.restype = ct.c_void_p
        hdc = gdi.CreateDCW("WINSPOOL", name, None, buffer)
        assert hdc
        target = tmp_path / "synthetic-traveloka-a4.pdf"
        image = render_booking_a4(parse_traveloka_print(sample_print_message()))
        try:
            assert image.size == (2480, 3508) and image.info["body_font_points"] >= 8
            PrinterJob(hdc, gdi).print_page(image, str(target))
        finally:
            image.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if target.exists() and target.read_bytes().rstrip().endswith(b"%%EOF"):
                break
            time.sleep(0.1)
        raw = target.read_bytes()
        assert len(re.findall(rb"/Type\s*/Page\b", raw)) == 1
        media = re.search(rb"/MediaBox\s*\[\s*([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s*\]", raw)
        assert media
        assert abs(float(media[3]) - float(media[1]) - 595.28) < 2
        assert abs(float(media[4]) - float(media[2]) - 841.89) < 2
        if os.environ.get("BOOKING_UI_SCREENSHOT_DIR"):
            directory = Path(os.environ["BOOKING_UI_SCREENSHOT_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, directory / "synthetic-traveloka-printed-a4.pdf")
    finally:
        spool.ClosePrinter(handle)

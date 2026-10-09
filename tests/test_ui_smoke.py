from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import ttk
from unittest.mock import Mock

import pytest

from app import BookingNotifierApp
from booking_notifier.models import BookingEvent


def run_in_fresh_tk_process(node: str) -> bool:
    # Windows Tcl can retain invalid initialization state after destroying its
    # first root. Each real app launch owns one interpreter, so test that way too.
    if os.environ.get("BOOKING_UI_SMOKE_CHILD") == "1":
        return False
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", f"{Path(__file__).resolve()}::{node}"],
        env={**os.environ, "BOOKING_UI_SMOKE_CHILD": "1"}, check=True, timeout=60,
    )
    return True


def popup_action_buttons(popup):
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)

    buttons = [child for child in descendants(popup) if isinstance(child, ttk.Button)
               and child.winfo_name() in {"copy_booking", "close_notification"}]
    assert [button.cget("text") for button in buttons] in (
        ["Sao chép", "Đóng thông báo"], ["Sao chép mã", "Đóng thông báo"],
    )
    assert all(button.winfo_viewable() and button.winfo_height() >= 96 for button in buttons)
    assert all(button.winfo_width() >= 240 for button in buttons)
    assert abs(buttons[0].winfo_width() - buttons[1].winfo_width()) <= 1
    for button in buttons:
        assert button.winfo_width() >= button.winfo_reqwidth()
        assert button.winfo_rooty() >= popup.winfo_rooty()
        assert button.winfo_rooty() + button.winfo_height() <= popup.winfo_rooty() + popup.winfo_height()
        assert button.cget("takefocus")
    return buttons


def wait_for_tray(app):
    deadline = time.monotonic() + 8
    while not app.tray.available and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.02)
    assert app.tray.available
    assert app.tray.icon.visible
    assert app.tray.icon._hwnd and app.tray.icon._icon_handle
    app._drain_events()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows startup with legacy missing-ID pending booking")
def test_legacy_no_booking_id_restores_popup_and_excel_and_keeps_next_arrival(tmp_path, monkeypatch):
    if run_in_fresh_tk_process("test_legacy_no_booking_id_restores_popup_and_excel_and_keeps_next_arrival"):
        return
    import app as desktop
    from booking_notifier.config import ConfigStore
    from booking_notifier.excel_export import excel_tsv
    from booking_notifier.state import StateStore

    store = ConfigStore(tmp_path / "config.json")
    store.save({"f92_enabled": False, "quiet_hours_enabled": False, "start_with_windows": False, "update_manifest_source": ""})
    state = StateStore(tmp_path / "state.json")
    record = {"checkin_date": date.today().isoformat(), "checkout_date": (date.today() + timedelta(days=1)).isoformat(),
              "guest_name": "Test Legacy Guest", "room_type": "Deluxe x1", "total_revenue": "VND 200,000",
              "subject": "Agoda test booking confirmation", "received_at": "test-arrival"}
    identity = StateStore._record_storage_id(record)
    state.data["bookings"][identity] = {**record, "status": "active"}
    state.data["pending_alerts"].append(record)  # Legacy record has neither source nor booking_id.
    state.data["history"].append({"guest_name": "Test Old Guest", "checkin_date": "30/09/2026"})
    state.stage_mailbox_reads("incremental:test:123", list(range(1, 11)), parser_version="p10")
    reloaded = StateStore(state.path)
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(desktop, "ConfigStore", lambda: store)
    monkeypatch.setattr(desktop, "StateStore", lambda: reloaded)
    monkeypatch.setattr(desktop.BookingNotifierApp, "play_sound", lambda self: setattr(self, "sound_active", True))
    root = tk.Tk()
    app = BookingNotifierApp(root)
    try:
        root.update()
        assert app.active_alert.source == "Agoda" and app.active_alert.booking_id == ""
        assert app.active_alert.guest_name == "Test Legacy Guest"
        assert app.active_popup.winfo_viewable() and app.sound_active
        wait_for_tray(app)
        app.on_close()
        root.update()
        assert root.state() == "withdrawn" and app.active_popup.winfo_viewable()
        next_booking = BookingEvent(source="Expedia", booking_id="1234567890", checkin_date=date.today(),
                                    guest_name="Test New Guest", room_type="Superior x1")
        alert = reloaded.register_today_confirmation(next_booking, ("test-new",), date.today())
        app.events.put(("alert", alert))
        app._drain_events()
        copy, close = popup_action_buttons(app.active_popup)
        copy.invoke()
        assert root.clipboard_get() == excel_tsv(BookingEvent.from_dict(record))
        assert len(root.clipboard_get().split("\t")) == 9
        assert root.clipboard_get().endswith("Agoda Deluxe x1") and app.sound_active
        close.invoke()
        root.update()
        assert app.active_alert.booking_id == "1234567890" and app.active_popup.winfo_viewable()
        assert root.state() == "withdrawn" and app.sound_active
        popup_action_buttons(app.active_popup)[1].invoke()
        assert app.active_popup is None and not app.sound_active
        assert not reloaded.pending_for_date(date.today())
        assert len(reloaded.history()) == 3
        assert reloaded.mailbox_read_position("incremental:test:123") == (10, list(range(1, 11)))
        assert reloaded.register_today_confirmation(BookingEvent.from_dict(record), ("alias",), date.today()) is None
    finally:
        app.exit_app()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows Expedia delayed-event notification deduplication")
def test_expedia_late_duplicate_event_does_not_reopen_popup_or_replay_sound(tmp_path, monkeypatch):
    if run_in_fresh_tk_process("test_expedia_late_duplicate_event_does_not_reopen_popup_or_replay_sound"):
        return
    import app as desktop
    from booking_notifier.config import ConfigStore
    from booking_notifier.state import StateStore

    store = ConfigStore(tmp_path / "config.json")
    store.save({"f92_enabled": False, "quiet_hours_enabled": False, "start_with_windows": False, "update_manifest_source": ""})
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(desktop, "ConfigStore", lambda: store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    root = tk.Tk()
    app = BookingNotifierApp(root)
    app.play_sound = Mock(side_effect=lambda: setattr(app, "sound_active", True))
    app.f92_worker.notify = Mock()
    app.config["f92_enabled"] = True  # Exercise the mocked F92 notification path, not real hardware.
    try:
        wait_for_tray(app)
        app.on_close()
        root.update()
        event = BookingEvent(source="Expedia", booking_id="1234567890", checkin_date=date.today(),
                             guest_name="Test Guest", room_type="Deluxe x1")
        late_alert = state.register_today_confirmation(event, ("test-email",), date.today())
        # Force the minute tick between the worker's state commit and queue emission.
        app._minute_tick()
        root.update()
        popup = app.active_popup
        assert popup.winfo_viewable() and root.state() == "withdrawn"
        copy, close = popup_action_buttons(popup)
        copy.invoke()
        assert not state.is_acknowledged(event) and app.sound_active
        close.invoke()
        assert app.active_popup is None and state.is_acknowledged(event)
        app.state = StateStore(state.path)
        for _ in range(2):
            app.events.put(("alert", late_alert))
        app._drain_events()
        app._minute_tick()
        root.update()
        assert app.active_popup is None and not app.alert_queue and not app.sound_active
        assert len(app.state.history()) == 1 and len(app.history_tree.get_children()) == 1
        app.play_sound.assert_called_once()
        app.f92_worker.notify.assert_called_once()
        # Another reservation for the same named guest must still be shown normally.
        other = BookingEvent(source="Expedia", booking_id="1234567891", checkin_date=date.today(), guest_name="Test Guest")
        app.events.put(("alert", app.state.register_today_confirmation(other, ("test-other",), date.today())))
        app._drain_events()
        root.update()
        assert app.active_popup.winfo_viewable() and app.active_alert.booking_id == other.booking_id
        assert app.play_sound.call_count == 2
        popup_action_buttons(app.active_popup)[1].invoke()
        assert len(app.state.history()) == 2 and not app.state.pending_for_date(date.today())
    finally:
        app.exit_app()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows Expedia A4 preview and notification integration")
def test_expedia_print_preview_buttons_context_and_tray_do_not_acknowledge(tmp_path, monkeypatch):
    if run_in_fresh_tk_process("test_expedia_print_preview_buttons_context_and_tray_do_not_acknowledge"):
        return
    from email import policy
    from email.message import EmailMessage
    from types import SimpleNamespace

    import app as desktop
    from booking_notifier.config import ConfigStore
    from booking_notifier.expedia_print import parse_expedia_print, render_expedia_a4
    from booking_notifier.state import StateStore

    store = ConfigStore(tmp_path / "config.json")
    store.save({"f92_enabled": False, "quiet_hours_enabled": False, "start_with_windows": False, "update_manifest_source": ""})
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(desktop, "ConfigStore", lambda: store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    monkeypatch.setattr(desktop.BookingNotifierApp, "play_sound", lambda self: setattr(self, "sound_active", True))
    message = EmailMessage(policy=policy.default)
    message["From"] = "Expedia <notify@expedia.com>"
    message["Subject"] = "Expedia - New Booking - Arriving on 10 Sep 2026"
    message.set_content((Path(__file__).parent / "fixtures/expedia_print_test.html").read_text(encoding="utf-8"), subtype="html")
    data = parse_expedia_print(message)
    data.booking.checkin_date = date.today()
    data.booking.checkout_date = date.today() + timedelta(days=7)
    root = tk.Tk()
    app = BookingNotifierApp(root)
    try:
        wait_for_tray(app)
        state.register_today_confirmation(data.booking, ["test-print"], date.today())
        # The main table lists acknowledged history, while this live popup remains pending.
        monkeypatch.setattr(state, "history", lambda: [data.booking.to_dict()])
        app.refresh_history()
        app.on_close()
        root.update()
        assert root.state() == "withdrawn"
        app.enqueue_alert(data.booking)
        root.update()
        popup = app.active_popup
        copy_button, close_button = popup_action_buttons(popup)
        print_buttons = [child for frame in popup.winfo_children() for child in frame.winfo_children()
                         if isinstance(child, ttk.Button) and child.winfo_name() == "print_expedia"]
        assert len(print_buttons) == 1 and print_buttons[0].winfo_viewable()
        request = Mock()
        monkeypatch.setattr(app, "request_expedia_print", request)
        print_buttons[0].invoke()
        request.assert_called_once_with(data.booking)
        assert app.active_alert is data.booking and app.sound_active
        assert app.active_menu.index("end") == 1

        app.restore_main_window()
        root.update()
        row = app.history_tree.get_children()[0]
        monkeypatch.setattr(app.history_menu, "tk_popup", Mock())
        monkeypatch.setattr(app.history_menu, "grab_release", Mock())
        row_y = app.history_tree.bbox(row)[1] + 8
        app.show_history_context_menu(SimpleNamespace(y=row_y, x_root=50, y_root=50))
        assert app.history_menu.index("end") == 1
        app.history_menu.invoke(1)
        assert request.call_args.args[0].booking_id == data.booking.booking_id
        assert request.call_args.args[0].source == "Expedia"
        app.on_close()
        root.update()
        app.show_expedia_print_preview(data.booking, render_expedia_a4(data))
        root.update()
        assert app.print_preview.winfo_viewable() and root.state() == "withdrawn"
        assert popup.winfo_viewable() and app.sound_active
        assert app.active_alert is data.booking
        assert state.pending_for_date(date.today())
        assert app.print_preview.winfo_height() < app.print_preview.winfo_screenheight()
        screenshot_dir = os.environ.get("BOOKING_UI_SCREENSHOT_DIR")
        if screenshot_dir:
            from PIL import ImageGrab

            target = Path(screenshot_dir)
            target.mkdir(parents=True, exist_ok=True)
            for window, name in ((popup, "expedia-popup-print.png"), (app.print_preview, "expedia-a4-preview.png")):
                window.lift()
                root.update()
                x, y = window.winfo_rootx(), window.winfo_rooty()
                ImageGrab.grab(bbox=(x, y, x + window.winfo_width(), y + window.winfo_height())).save(target / name)

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        preview_print = next(child for child in descendants(app.print_preview)
                             if isinstance(child, ttk.Button) and child.cget("text") == "In 1 trang A4")
        native_ready, allow_cancel = threading.Event(), threading.Event()

        def cancel_after_other_ui_actions(_owner):
            native_ready.set()
            assert allow_cancel.wait(3)
            return None

        chooser = Mock(side_effect=cancel_after_other_ui_actions)
        monkeypatch.setattr(desktop, "choose_printer", chooser)
        preview_print.invoke()
        assert native_ready.wait(1) and app.print_spooling
        copy_button.invoke()  # The booking UI stays responsive while the native dialog waits.
        assert app.sound_active and root.clipboard_get().split("\t")[1] == "MINH TRẦN"
        app.close_expedia_print_preview()
        assert app.print_preview is not None  # Keep native dialog owner alive until cancellation.
        allow_cancel.set()
        deadline = time.monotonic() + 3
        while app.print_spooling and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert chooser.call_count == 1 and app.sound_active and not app.print_spooling
        app.close_expedia_print_preview()
        root.update()
        assert app.print_preview is None and app.print_preview_image is None and app.print_preview_photo is None
        assert root.state() == "withdrawn" and app.active_alert is data.booking and app.sound_active
        copy_button.invoke()
        assert root.clipboard_get().split("\t")[8].startswith("Expedia Superior Double Room")
        assert "4111" not in root.clipboard_get() and "CVV" not in root.clipboard_get()
        close_button.invoke()
        assert not app.sound_active
        app.enqueue_alert(BookingEvent(source="Agoda", booking_id="AGODA-TEST", checkin_date=date.today(), guest_name="Agoda Test"))
        root.update()
        assert not any(isinstance(child, ttk.Button) and child.winfo_name() == "print_expedia"
                       for child in descendants(app.active_popup))
        assert app.active_menu.index("end") == 0
        assert len(popup_action_buttons(app.active_popup)) == 2
        # Switching right-click source removes Expedia's print entry.
        row = app.history_tree.get_children()[0]
        app.history_rows[row] = BookingEvent(source="Agoda", booking_id="AGODA-TEST").to_dict()
        app.show_history_context_menu(SimpleNamespace(y=row_y, x_root=50, y_root=50))
        assert app.history_menu.index("end") == 0
        serialized = (tmp_path / "state.json").read_text(encoding="utf-8")
        assert "4111" not in serialized and "cvv" not in serialized and "card" not in serialized
    finally:
        app.exit_app()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows source audio controls and tray popup queue")
def test_source_sound_settings_preview_and_popup_queue_in_tray(tmp_path, monkeypatch):
    if run_in_fresh_tk_process("test_source_sound_settings_preview_and_popup_queue_in_tray"):
        return
    import winsound

    import app as desktop
    from booking_notifier.audio import default_source_sound
    from booking_notifier.config import ConfigStore
    from booking_notifier.state import StateStore

    store = ConfigStore(tmp_path / "config.json")
    store.save({"f92_enabled": False, "quiet_hours_enabled": False, "start_with_windows": False, "update_manifest_source": ""})
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(desktop, "ConfigStore", lambda: store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    sound_calls = Mock()
    monkeypatch.setattr(winsound, "PlaySound", sound_calls)
    monkeypatch.setattr(winsound, "MessageBeep", Mock())
    root = tk.Tk()
    app = BookingNotifierApp(root)
    try:
        wait_for_tray(app)
        app.open_settings()
        root.update()
        app.settings_canvas.yview_moveto(0.45)
        root.update()
        assert set(app.source_sound_vars) == {"Agoda", "Expedia", "Traveloka", "Trip", "Booking.com"}
        for source in app.source_sound_vars:
            assert app.sound_preview_buttons[source].cget("text") == f"Nghe thử {source}"
            assert app.sound_choose_buttons[source].cget("text") == "Chọn tệp"
        screenshot_dir = os.environ.get("BOOKING_UI_SCREENSHOT_DIR")
        if screenshot_dir:
            from PIL import ImageGrab

            window = app.settings_window
            x, y = window.winfo_rootx(), window.winfo_rooty()
            target = Path(screenshot_dir)
            target.mkdir(parents=True, exist_ok=True)
            ImageGrab.grab(bbox=(x, y, x + window.winfo_width(), y + window.winfo_height())).save(target / "source-sounds-settings.png")

        agoda = default_source_sound("Agoda", tmp_path / "âm thanh riêng")
        expedia = default_source_sound("Expedia", tmp_path / "âm thanh riêng")
        for source, path in (("Agoda", agoda), ("Expedia", expedia)):
            monkeypatch.setattr(desktop.filedialog, "askopenfilename", Mock(return_value=str(path)))
            app.sound_choose_buttons[source].invoke()
            assert app.source_sound_vars[source].get() == str(path)
            app.sound_preview_buttons[source].invoke()
            assert sound_calls.call_args.args[0] == str(path)
            assert app.sound_preview_job and app.sound_active
        app.close_settings()
        assert not app.sound_active and app.sound_preview_job is None
        store.save(app._collect_config())
        app.config = store.load()
        app._load_config()
        assert app.config["agoda_sound_file"] == str(agoda)
        assert app.config["expedia_sound_file"] == str(expedia)
        app.on_close()
        root.update()
        assert root.state() == "withdrawn"
        for source in ("Agoda", "Expedia"):
            app.enqueue_alert(BookingEvent(source=source, booking_id=f"{source}-12345", checkin_date=date.today(), guest_name="Test Guest", room_type="Deluxe x1"))
        root.update()
        assert app.active_alert.source == "Agoda"
        assert app.active_popup.winfo_viewable() and root.state() == "withdrawn"
        assert sound_calls.call_args.args[0] == str(agoda)
        copy, close = popup_action_buttons(app.active_popup)
        copy.invoke()
        assert app.sound_active and root.clipboard_get().endswith("Agoda Deluxe x1")
        app.stop_sound_preview()
        assert app.sound_active  # Preview Stop cannot stop this real booking.
        close.invoke()
        root.update()
        assert app.active_alert.source == "Expedia"
        assert app.active_popup.winfo_viewable() and root.state() == "withdrawn"
        assert sound_calls.call_args.args[0] == str(expedia)
        popup_action_buttons(app.active_popup)[1].invoke()
        assert not app.sound_active and app.active_popup is None
        assert sound_calls.call_args.args == (None, 0)
        assert app.sound_repeat_job is app.sound_preview_job is None
    finally:
        app.exit_app()


@pytest.mark.skipif(os.name != "nt", reason="The packaged desktop app targets Windows")
def test_windows_ui_builds_with_excel_context_menu():
    if run_in_fresh_tk_process("test_windows_ui_builds_with_excel_context_menu"):
        return
    root = tk.Tk()
    root.withdraw()
    app = BookingNotifierApp(root)
    try:
        root.update_idletasks()
        assert app.history_menu.index("end") == 0
        assert app.history_menu.entrycget(0, "label") == "Sao chép dòng đã chọn sang Excel"
        assert app.history_tree.bind("<Button-3>")
        assert app.history_tree.bind("<Control-c>")

        app.config["f92_enabled"] = False
        # Notification policy now uses saved configuration, not checkbox drafts.
        app.config["quiet_hours_enabled"] = False
        app.quiet_var.set(False)
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


@pytest.mark.skipif(os.name != "nt", reason="Real Windows main page and settings")
def test_main_page_only_today_with_settings_and_selected_excel(tmp_path, monkeypatch):
    if run_in_fresh_tk_process("test_main_page_only_today_with_settings_and_selected_excel"):
        return
    import app as desktop
    from booking_notifier.config import ConfigStore
    from booking_notifier.state import StateStore

    config_store = ConfigStore(tmp_path / "config.json")
    config_store.save({"f92_enabled": False, "start_with_windows": False, "update_manifest_source": ""})
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "ConfigStore", lambda: config_store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    root = tk.Tk()
    app = BookingNotifierApp(root)
    try:
        def descendants(widget):
            for child in widget.winfo_children():
                if isinstance(child, tk.Toplevel):
                    continue
                yield child
                yield from descendants(child)

        root.update()
        assert root.winfo_rootx() + root.winfo_width() <= root.winfo_screenwidth()
        assert root.winfo_rooty() + root.winfo_height() <= root.winfo_screenheight() - 48
        buttons = [child for child in descendants(root) if isinstance(child, ttk.Button)]
        assert buttons == [app.settings_button]
        assert "Cài đặt" in app.settings_button.cget("text")
        assert not any(isinstance(child, ttk.Notebook) for child in descendants(root))
        assert app.settings_window.state() == "withdrawn"
        assert app.history_menu.index("end") == 0

        today = date.today()
        records = [BookingEvent(source="Agoda", booking_id="OLD", guest_name="Old Guest",
                                checkin_date=today - timedelta(days=1)).to_dict()]
        for booking_id, guest in (("1234567890", "MINH TRẦN"), ("1234567891", "SECOND GUEST")):
            records.append(BookingEvent(source="Expedia", booking_id=booking_id, guest_name=guest,
                                        room_type="Deluxe Double Room x1", checkin_date=today,
                                        checkout_date=today + timedelta(days=1), total_revenue="345,487 VND").to_dict())
        records.append(BookingEvent(source="Agoda", booking_id="FUTURE", guest_name="Future Guest",
                                    checkin_date=today + timedelta(days=1)).to_dict())
        monkeypatch.setattr(state, "history", lambda: records)
        app.refresh_history()
        root.update()
        items = app.history_tree.get_children()
        assert len(items) == 2
        assert app.history_tree.item(items[0], "values")[2] == "MINH TRẦN"
        x, _, width, _ = app.history_tree.bbox(items[0], "revenue")
        assert x + width <= app.history_tree.winfo_width()
        scrollbars = [child for child in app.history_tree.master.winfo_children() if isinstance(child, ttk.Scrollbar)]
        assert len(scrollbars) == 1 and scrollbars[0].winfo_width() > 0
        assert app.history_date_var.get() == today.strftime("Hôm nay • %d/%m/%Y")
        app.history_tree.selection_set(items[1])
        app.history_menu.invoke(0)
        assert root.clipboard_get().split("\t")[1] == "SECOND GUEST"
        assert root.clipboard_get().split("\t")[8] == "Expedia Deluxe Double Room x1"
        assert "\n" not in root.clipboard_get()  # Selected row only, never copy all.

        screenshot_dir = os.environ.get("BOOKING_UI_SCREENSHOT_DIR")
        if screenshot_dir:
            from PIL import ImageGrab

            target = Path(screenshot_dir)
            target.mkdir(parents=True, exist_ok=True)
            x, y = root.winfo_rootx(), root.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height())).save(target / "main-bookings.png")

        app.settings_button.invoke()
        root.update()
        assert app.settings_window.winfo_viewable()
        assert app.settings_window.winfo_rooty() + app.settings_window.winfo_height() <= root.winfo_screenheight() - 48
        settings_buttons = [child.cget("text") for child in descendants(app.settings_window) if isinstance(child, ttk.Button)]
        for label in ("Lưu & khởi động", "Quét email ngay", "Kiểm tra IMAP", "Kiểm tra cập nhật", "Thoát"):
            assert label in settings_buttons
        assert [app.settings_notebook.tab(tab, "text") for tab in app.settings_notebook.tabs()] == ["CẤU HÌNH", "NHẬT KÝ"]
        app.email_var.set("draft@example.com")
        app.log("Settings remain available")
        window = app.settings_window
        app.close_settings()
        root.update()
        assert window.state() == "withdrawn" and not app.closing
        app.open_settings()
        root.update()
        assert app.settings_window is window
        assert app.email_var.get() == "draft@example.com"
        assert "Settings remain available" in app.log_text.get("1.0", "end")
        if screenshot_dir:
            x, y = window.winfo_rootx(), window.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + window.winfo_width(), y + window.winfo_height())).save(target / "settings.png")
        app.close_settings()
        # The daily table rolls over without removing saved historical records.
        class NextDate(date):
            @classmethod
            def today(cls):
                return today + timedelta(days=1)

        monkeypatch.setattr(desktop, "date", NextDate)
        app._minute_tick()
        assert len(app.history_tree.get_children()) == 1
        assert app.history_tree.item(app.history_tree.get_children()[0], "values")[1] == "FUTURE"
        assert len(state.history()) == 4
    finally:
        app.exit_app()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows popup")
@pytest.mark.parametrize("source", ["Agoda", "Expedia"])
def test_email_to_visible_popup_while_minimized_even_if_sound_fails(tmp_path, monkeypatch, source):
    if run_in_fresh_tk_process(f"test_email_to_visible_popup_while_minimized_even_if_sound_fails[{source}]"):
        return
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
        wait_for_tray(app)
        app.on_close()
        root.update()
        assert root.state() == "withdrawn"

        def broken_sound():
            raise RuntimeError("unsupported audio file")

        app.play_sound = broken_sound
        message = EmailMessage()
        message["From"] = "booking@agoda.com" if source == "Agoda" else "notify@expediapartnercentral.com"
        message["Subject"] = "Booking confirmation"
        message["Message-ID"] = "<popup-smoke@example>"
        message.set_content(f"Booking ID: 707908051 Check-in: {date.today().isoformat()} Guest name: Test Guest")
        if source == "Expedia":
            fixture = "expedia_new_booking.html"
            guest = "MINH TRẦN"
            room = "Deluxe Double Room - Room Only x1"
        else:
            fixture = "agoda_bilingual_confirmation.html"
            guest = "Minh Trần"
            room = "Bunk Bed in Mixed Dormitory Room x1"
        html = (Path(__file__).parent / "fixtures" / fixture).read_text(encoding="utf-8")
        html = html.replace("30-Sep-2026 (30-09-2026)", date.today().isoformat())
        html = html.replace("Oct 13, 2026", date.today().isoformat())
        html = html.replace("Oct 14, 2026", (date.today() + timedelta(days=1)).isoformat())
        message.add_alternative(html, subtype="html")

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
        assert app.active_alert.guest_name == guest
        assert app.active_alert.room_type == room
        assert app.active_guest_var.get() == guest
        assert app.active_room_var.get() == room
        assert app.active_popup is not None and app.active_popup.winfo_viewable()
        assert bool(app.active_popup.attributes("-topmost"))
        assert app.active_popup.title() == f"{source} • Check-in hôm nay"
        assert root.state() == "withdrawn"  # Booking must not restore the main page.
        popup_before_hide = app.active_popup
        app.on_close()  # Hiding again cannot dismiss/acknowledge an existing alert.
        root.update()
        assert app.active_popup is popup_before_hide and popup_before_hide.winfo_viewable()
        copy_button, close_button = popup_action_buttons(app.active_popup)
        screenshot_dir = os.environ.get("BOOKING_UI_SCREENSHOT_DIR")
        if screenshot_dir:
            from PIL import ImageGrab

            target = Path(screenshot_dir)
            target.mkdir(parents=True, exist_ok=True)
            popup = app.active_popup
            root.update()
            x, y = popup.winfo_rootx(), popup.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + popup.winfo_width(), y + popup.winfo_height())).save(
                target / f"{source.lower()}-popup.png",
            )
        copy_button.invoke()
        cells = root.clipboard_get().split("\t")
        assert len(cells) == 9
        assert cells[0] == "" and cells[1] == guest
        assert cells[6:9] == ["", "", f"{source} {room}"]
        if source == "Expedia":
            assert cells[2:6] == [str(date.today().day), str((date.today() + timedelta(days=1)).day), "1", "345487"]
        assert app.active_popup is not None  # Copy does not dismiss the alert.
        close_button.invoke()
        assert app.active_popup is None
        assert not app.sound_active
        assert root.state() == "withdrawn"
        values = app.history_tree.item(app.history_tree.get_children()[0], "values")
        assert values[2:4] == (guest, room)
        monitor.scan_mailbox()
        app._drain_events()
        assert app.active_popup is None
        app.enqueue_alert(BookingEvent(source=source, booking_id="FUTURE", checkin_date=date.today() + timedelta(days=1)))
        assert app.active_popup is None
    finally:
        app.exit_app()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows tray and Tk lifecycle")
def test_tray_minimize_restore_settings_and_exit(tmp_path, monkeypatch):
    if run_in_fresh_tk_process("test_tray_minimize_restore_settings_and_exit"):
        return
    import app as desktop
    from booking_notifier.config import ConfigStore
    from booking_notifier.state import StateStore

    config_store = ConfigStore(tmp_path / "config.json")
    config_store.save({
        "f92_enabled": False, "start_with_windows": False,
        "update_manifest_source": "", "start_minimized": True,
    })
    monkeypatch.setattr(desktop, "ConfigStore", lambda: config_store)
    monkeypatch.setattr(desktop, "StateStore", lambda: StateStore(tmp_path / "state.json"))
    root = tk.Tk()
    app = BookingNotifierApp(root)
    try:
        wait_for_tray(app)
        app._minimize_if_no_alert()
        root.update()
        assert root.state() == "withdrawn"
        menu = list(app.tray.icon.menu.items)
        assert len(menu) == 4 and menu[0].default
        assert [menu[0].text, menu[1].text, menu[-1].text] == [
            "Mở Booking hôm nay", "Cài đặt", "Thoát ứng dụng",
        ]

        def native_callback(item):
            worker = threading.Thread(target=lambda: item(app.tray.icon))
            worker.start()
            worker.join(2)
            assert not worker.is_alive()  # No cross-thread Tk calls/deadlock.
            app._drain_events()
            if not app.closing:
                root.update()

        native_callback(menu[0])
        assert root.state() == "normal" and not app.hidden_to_tray
        root.iconify()  # Native minimize control, not just the X handler.
        root.update()
        assert root.state() == "withdrawn"
        native_callback(menu[1])
        assert root.state() == "normal" and app.settings_window.winfo_viewable()
        app.email_var.set("draft@example.com")
        app.on_close()
        root.update()
        assert root.state() == app.settings_window.state() == "withdrawn"
        native_callback(menu[1])
        assert app.email_var.get() == "draft@example.com"
        assert app.settings_window.winfo_viewable()
        native_callback(menu[-1])
        assert app.closing and not app.tray.available
    finally:
        if not app.closing:
            app.exit_app()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows popup repair")
@pytest.mark.parametrize("source", ["Agoda", "Expedia"])
def test_repaired_pending_details_update_same_popup_queue_and_excel(tmp_path, monkeypatch, source):
    if run_in_fresh_tk_process(f"test_repaired_pending_details_update_same_popup_queue_and_excel[{source}]"):
        return
    import app as desktop
    from booking_notifier.config import ConfigStore
    from booking_notifier.mail_monitor import ImapMonitor
    from booking_notifier.state import StateStore

    config_store = ConfigStore(tmp_path / "config.json")
    config_store.save({"quiet_hours_enabled": False, "f92_enabled": False, "start_with_windows": False})
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "ConfigStore", lambda: config_store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    root = tk.Tk()
    root.withdraw()
    app = BookingNotifierApp(root)
    try:
        app.play_sound = Mock()
        app.f92_worker.notify = Mock()
        today = date.today()
        for booking_id in ("987654321", "987654322"):
            incomplete = BookingEvent(source=source, booking_id=booking_id, checkin_date=today, guest_name="Minh")
            app.enqueue_alert(state.register_today_confirmation(incomplete, (booking_id,), today))
        root.update()
        original_popup = app.active_popup
        assert app.active_guest_var.get() == "Minh"
        assert app.active_room_var.get() == "—"
        from email.message import EmailMessage

        messages = {}
        for booking_id in ("987654321", "987654322"):
            message = EmailMessage()
            message["From"] = "booking@agoda.com" if source == "Agoda" else "booknotif@expedia.com"
            message["Subject"] = f"{source} Booking confirmation {booking_id}"
            message.set_content(
                f"Booking ID: {booking_id}\nCheck-in: {today.isoformat()}\n"
                "Guest Name: Minh Trần\nRoom Type: Bunk Bed in Mixed Dormitory Room\nRooms: 1",
            )
            messages[booking_id] = message.as_bytes()

        class RecoveryClient:
            def uid(self, command, *args):
                if command == "search":
                    return "OK", [args[-1].strip('"').encode("ascii")]
                return "OK", [(b"BODY[]", messages[args[0].decode("ascii")])]

        monitor = ImapMonitor(app.config, "test-secret", state, app.events)
        monitor._recover_incomplete_details(RecoveryClient(), state.incomplete_confirmations(today), set())
        app._drain_events()
        root.update()
        assert app.active_popup is original_popup and original_popup.winfo_viewable()
        assert app.active_guest_var.get() == "Minh Trần"
        assert app.active_room_var.get() == "Bunk Bed in Mixed Dormitory Room x1"
        assert len(app.alert_queue) == 1 and app.alert_queue[0].guest_name == "Minh Trần"
        app.play_sound.assert_called_once()
        assert app.f92_worker.notify.call_args.kwargs == {"play_sound": False}
        app.copy_active_alert()
        cells = root.clipboard_get().split("\t")
        assert cells[1] == "Minh Trần"
        assert cells[8] == f"{source} Bunk Bed in Mixed Dormitory Room x1"
        app.acknowledge_alert()
        assert state.history()[0]["guest_name"] == "Minh Trần"
        assert state.history()[0]["room_type"] == "Bunk Bed in Mixed Dormitory Room x1"
    finally:
        app.exit_app()

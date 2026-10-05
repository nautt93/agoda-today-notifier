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

from app import BookingNotifierApp
from booking_notifier.excel_export import excel_tsv
from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore


def test_traveloka_excel_keeps_original_nine_column_layout_and_full_room_note():
    event = BookingEvent(source="Traveloka", booking_id="20261234000001", guest_name="Synthetic Full Guest",
                         checkin_date=date(2026, 10, 4), checkout_date=date(2026, 10, 6),
                         room_type="Deluxe Double Room x2", total_revenue="VND 400,000")
    assert excel_tsv(event).split("\t") == ["", "Synthetic Full Guest", "4", "6", "2", "400000", "", "", "Traveloka Deluxe Double Room x2"]
    event.room_type = "Traveloka Deluxe Double Room x2"
    assert excel_tsv(event).split("\t")[-1] == event.room_type


def test_traveloka_late_ui_worker_event_after_close_cannot_reopen_or_duplicate_history(tmp_path):
    state = StateStore(tmp_path / "state.json")
    event = BookingEvent(source="Traveloka", booking_id="20261234000001", checkin_date=date.today(), guest_name="Synthetic Guest")
    late = state.register_today_confirmation(event, ("test-email",), date.today())
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.state, app.quiet_var = state, SimpleNamespace(get=lambda: False)
    app.queued_ids, app.alert_queue, app.active_alert = set(), [], None
    app._show_next_alert, app.play_sound, app.f92_worker = Mock(), Mock(), Mock()
    app.enqueue_alert(state.pending_for_date(date.today())[0])
    app._show_next_alert.assert_called_once()
    state.acknowledge(late)
    app.queued_ids.clear()
    app.alert_queue.clear()
    app.state = StateStore(state.path)
    for _ in range(2):
        app.enqueue_alert(late)
    assert app._show_next_alert.call_count == 1 and not app.alert_queue and not app.queued_ids
    app.play_sound.assert_not_called()
    assert len(app.state.history()) == 1 and not app.state.pending_for_date(date.today())


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def large_actions(popup):
    actions = [child for child in descendants(popup) if isinstance(child, ttk.Button)
               and child.winfo_name() in {"copy_booking", "close_notification"}]
    assert [button.cget("text") for button in actions] == ["Sao chép", "Đóng thông báo"]
    assert all(button.winfo_viewable() and button.winfo_height() >= 96 and button.winfo_width() >= 240 for button in actions)
    assert abs(actions[0].winfo_width() - actions[1].winfo_width()) <= 1
    return actions


@pytest.mark.skipif(os.name != "nt", reason="Native Windows Traveloka popup, tray, clipboard and MP3 integration")
def test_traveloka_popup_tray_copy_close_and_queued_provider_mp3s(tmp_path, monkeypatch):
    if os.environ.get("BOOKING_TRAVELOKA_UI_CHILD") != "1":
        subprocess.run([sys.executable, "-m", "pytest", "-q", f"{Path(__file__).resolve()}::test_traveloka_popup_tray_copy_close_and_queued_provider_mp3s"],
                       env={**os.environ, "BOOKING_TRAVELOKA_UI_CHILD": "1"}, check=True, timeout=60)
        return
    import winsound

    import app as desktop
    from booking_notifier.audio import SOURCE_SOUND_PACK, WindowsMciAudioPlayer
    from booking_notifier.config import ConfigStore

    store = ConfigStore(tmp_path / "config.json")
    store.save({"f92_enabled": False, "quiet_hours_enabled": False, "start_with_windows": False, "update_manifest_source": ""})
    state = StateStore(tmp_path / "state.json")
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(desktop, "ConfigStore", lambda: store)
    monkeypatch.setattr(desktop, "StateStore", lambda: state)
    send = Mock()  # Use actual MCI command construction, with no physical audio device.
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
        assert app.config["source_sound_pack"] == store.load()["source_sound_pack"] == SOURCE_SOUND_PACK
        expected_files = {"Agoda": "1-agoda.mp3", "Expedia": "2-expedia.mp3", "Traveloka": "3-traveloka.mp3", "Trip": "4-trip.mp3"}
        assert set(app.source_sound_vars) == set(expected_files)
        for source, filename in expected_files.items():
            path = Path(app.config[f"{source.lower()}_sound_file"])
            assert path.name == filename and path.is_file()
        app.on_close()
        root.update()
        assert root.state() == "withdrawn"

        events = [BookingEvent(source=source, booking_id=str(20261234000001 + index), guest_name=f"Synthetic {source} Guest",
                               checkin_date=date.today(), checkout_date=date.today() + timedelta(days=2),
                               room_type="Deluxe Double Room x2", total_revenue="VND 400,000")
                  for index, source in enumerate(("Traveloka", "Expedia", "Agoda"))]
        for event in events:
            app.enqueue_alert(state.register_today_confirmation(event, (f"test-{event.source}",), date.today()))
        root.update()
        for event in events:
            assert app.active_alert.storage_id == event.storage_id
            assert root.state() == "withdrawn" and app.active_popup.winfo_viewable()
            assert app.sound_active and app.sound_uses_file
            opens = [call.args[0] for call in send.call_args_list if call.args[0].startswith("open ")]
            assert expected_files[event.source] in opens[-1]
            assert send.call_args.args == (f"play {WindowsMciAudioPlayer.ALIAS} repeat",)
            buttons = list(child for child in descendants(app.active_popup) if isinstance(child, ttk.Button))
            if event.source == "Traveloka":
                assert len(buttons) == 3 and not any(button.winfo_name() == "print_expedia" for button in buttons)
                traveloka_print = next(button for button in buttons if button.winfo_name() == "print_traveloka")
                assert traveloka_print.cget("text") == "In phiếu Traveloka - 1 trang A4"
                assert app.active_menu.index("end") == 1
            copy, close = large_actions(app.active_popup)
            copy.invoke()
            assert root.clipboard_get() == excel_tsv(event)
            assert len(root.clipboard_get().split("\t")) == 9
            assert root.clipboard_get().endswith(f"{event.source} Deluxe Double Room x2")
            assert not state.is_acknowledged(event) and app.sound_active
            close.invoke()
            root.update()
            assert state.is_acknowledged(event)

        assert app.active_popup is None and not app.sound_active
        assert not state.pending_for_date(date.today()) and len(state.history()) == 3
        opens_before_duplicate = [call for call in send.call_args_list if call.args[0].startswith("open ")]
        for _ in range(2):
            app.events.put(("alert", events[0]))
        app._drain_events()
        app._minute_tick()
        root.update()
        assert app.active_popup is None and not app.sound_active and len(state.history()) == 3
        assert [call for call in send.call_args_list if call.args[0].startswith("open ")] == opens_before_duplicate
        app.restore_main_window()
        root.update()
        row = next(row for row, record in app.history_rows.items() if record["source"] == "Traveloka")
        monkeypatch.setattr(app.history_menu, "tk_popup", Mock())
        monkeypatch.setattr(app.history_menu, "grab_release", Mock())
        app.show_history_context_menu(SimpleNamespace(y=app.history_tree.bbox(row)[1] + 8, x_root=50, y_root=50))
        assert app.history_menu.index("end") == 1
        assert app.history_menu.entrycget(0, "label") == "Sao chép dòng đã chọn sang Excel"
        assert app.history_menu.entrycget(1, "label") == "In phiếu Traveloka - 1 trang A4"
    finally:
        app.exit_app()

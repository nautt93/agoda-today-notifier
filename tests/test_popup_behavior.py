"""Clipboard actions preserve notification ownership and other sources' Excel format."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from imaplib import IMAP4
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app as desktop
from app import BookingNotifierApp
from booking_notifier.excel_export import excel_tsv
from booking_notifier.mail_monitor import ImapAuthenticationError
from booking_notifier.models import BookingEvent


def event(source="Booking.com"):
    return BookingEvent(source=source, booking_id="5550000001", checkin_date=date.today(),
                        checkout_date=date.today() + timedelta(days=2), guest_name="SYNTHETIC GUEST",
                        room_type="Deluxe Room x2", total_revenue="VND 800.000")


def popup_app(alert):
    instance = BookingNotifierApp.__new__(BookingNotifierApp)
    instance.root, instance.state, instance.f92_worker = Mock(), Mock(), Mock()
    instance.active_alert, instance.active_popup = alert, Mock()
    instance.sound_active = True
    for name in ("copy_feedback_var", "stop_sound", "play_sound", "set_status",
                 "_fit_popup_guest_header", "_show_next_alert", "acknowledge_alert",
                 "active_guest_var", "active_room_var", "active_revenue_var",
                 "active_checkout_var", "active_nights_var"):
        setattr(instance, name, Mock())
    instance.alert_queue = []
    return instance


@pytest.mark.parametrize("details", [True, False])
def test_booking_com_copies_only_code_without_acknowledging_or_stopping_sound(details):
    alert = event() if details else BookingEvent(source="Booking.com", booking_id="5550000001", checkin_date=date.today())
    instance = popup_app(alert)
    popup = instance.active_popup
    instance.copy_active_alert()
    instance.root.clipboard_append.assert_called_once_with(alert.booking_id)
    instance.copy_feedback_var.set.assert_called_once_with("Đã sao chép mã đặt phòng")
    instance.set_status.assert_called_once_with("Đã sao chép mã đặt phòng")
    assert instance.active_alert is alert and instance.active_popup is popup and instance.sound_active
    instance.stop_sound.assert_not_called()
    instance.state.acknowledge.assert_not_called()
    instance._show_next_alert.assert_not_called()
    popup.destroy.assert_not_called()


@pytest.mark.parametrize("source", ["Agoda", "Expedia", "Traveloka", "Trip"])
def test_other_sources_keep_exact_nine_column_copy_and_popup_sound(source):
    alert = event(source)
    instance = popup_app(alert)
    instance.copy_active_alert()
    expected = excel_tsv(alert)
    assert len(expected.split("\t")) == 9
    instance.root.clipboard_append.assert_called_once_with(expected)
    instance.copy_feedback_var.set.assert_called_once_with("Đã sao chép • Mở Excel và nhấn Ctrl+V")
    instance.stop_sound.assert_not_called()
    instance.state.acknowledge.assert_not_called()


def test_no_active_booking_does_not_change_clipboard():
    instance = popup_app(None)
    instance.copy_active_alert()
    instance.root.clipboard_clear.assert_not_called()
    instance.copy_feedback_var.set.assert_not_called()


def test_multiple_selected_sources_copy_codes_and_keep_other_excel_rows():
    instance = popup_app(None)
    booking, agoda = event(), event("Agoda")
    assert instance._copy_alerts_to_clipboard([booking, agoda], "Đã sao chép 2 booking")
    instance.root.clipboard_append.assert_called_once_with(booking.booking_id + "\r\n" + excel_tsv(agoda))


def test_retired_details_do_not_repopulate_code_only_popup_or_trigger_f92():
    original = BookingEvent(source="Booking.com", booking_id="5550000001", checkin_date=date.today())
    instance = popup_app(original)
    instance.state.pending_for_date.return_value = [event()]
    instance._refresh_pending_details()
    assert not original.guest_name and not original.room_type and original.checkout_date is None
    instance.active_guest_var.set.assert_not_called()
    instance.f92_worker.notify.assert_not_called()


@pytest.mark.parametrize("source", ["Agoda", "Expedia", "Traveloka", "Trip"])
def test_email_repair_updates_other_sources_same_popup_without_replaying_sound(source):
    saved = event(source)
    original = replace(saved, guest_name="", room_type="", checkout_date=None, total_revenue="")
    instance = popup_app(original)
    popup = instance.active_popup
    instance.state.pending_for_date.return_value = [saved]
    instance._refresh_pending_details()
    assert instance.active_popup is popup and instance.active_alert is original
    assert excel_tsv(original) == excel_tsv(saved)
    instance.active_guest_var.set.assert_called_once_with(saved.guest_name)
    instance.active_room_var.set.assert_called_once_with(saved.room_type)
    instance.f92_worker.notify.assert_called_once_with(original, play_sound=False)
    instance.play_sound.assert_not_called()
    instance.stop_sound.assert_not_called()


@pytest.mark.parametrize("error,credential_error", [(IMAP4.abort("socket EOF"), False),
                                                   (ImapAuthenticationError("LOGIN rejected"), True)])
def test_settings_imap_test_uses_correct_error_explanation(monkeypatch, error, credential_error):
    instance = popup_app(None)
    instance.events = Queue()
    instance._collect_config = Mock(return_value={})
    instance.password_var = Mock(get=Mock(return_value="synthetic-password"))
    monkeypatch.setattr(desktop, "test_imap_connection", Mock(side_effect=error))
    monkeypatch.setattr(desktop.threading, "Thread", lambda *, target, **_kwargs: SimpleNamespace(start=target))
    instance.test_connection()
    kind, (ok, explanation) = instance.events.get_nowait()
    assert kind == "connection_test" and not ok
    assert ("mật khẩu ứng dụng" in explanation) is credential_error
    if not credential_error:
        assert "kết nối lại" in explanation

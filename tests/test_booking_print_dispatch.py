from __future__ import annotations

import queue
from unittest.mock import Mock

import pytest

import app as desktop
from app import BookingNotifierApp
from booking_notifier.models import BookingEvent


@pytest.mark.parametrize("source", ["Agoda", "Unknown"])
def test_unsupported_print_sources_do_not_open_mail_or_decrypt_password(monkeypatch, source):
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    decrypt = Mock()
    monkeypatch.setattr(desktop, "unprotect_secret", decrypt)
    app.request_booking_print(BookingEvent(source=source, booking_id="123456789"))
    decrypt.assert_not_called()


def test_print_action_keeps_expedia_compatibility_and_routes_traveloka():
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.request_expedia_print, app.request_booking_print = Mock(), Mock()
    expedia = BookingEvent(source="Expedia", booking_id="123456789")
    traveloka = BookingEvent(source="Traveloka", booking_id="20261234000001")
    app._request_source_print(expedia)
    app._request_source_print(traveloka)
    app.request_expedia_print.assert_called_once_with(expedia)
    app.request_booking_print.assert_called_once_with(traveloka)


def test_missing_credentials_error_is_traveloka_named_and_owned_by_visible_popup(monkeypatch):
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.closing = app.print_loading = app.print_spooling = False
    app.config = {"email_address": "hotel@example.invalid", "password_encrypted": "unavailable"}
    app.root, app.active_popup = Mock(), Mock()
    monkeypatch.setattr(desktop, "unprotect_secret", Mock(side_effect=ValueError("PRIVATE SECRET")))
    dialog = Mock()
    monkeypatch.setattr(desktop.messagebox, "showerror", dialog)
    app.request_booking_print(BookingEvent(source="Traveloka", booking_id="20261234000001"))
    assert not app.print_loading
    assert dialog.call_args.args[0] == "In phiếu Traveloka"
    assert "PRIVATE" not in dialog.call_args.args[1]
    assert dialog.call_args.kwargs["parent"] is app.active_popup


@pytest.mark.parametrize("kind", ["expedia_print_ready", "booking_print_ready"])
def test_print_ready_dispatch_uses_selected_traveloka_source_for_dialog(monkeypatch, kind):
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.closing = False
    app.print_loading = True
    app.root, app.active_popup = Mock(), Mock()
    app.events = queue.Queue()
    app.events.put((kind, (BookingEvent(source="Traveloka", booking_id="20261234000001"), None, "Không có email khớp")))
    dialog = Mock()
    monkeypatch.setattr(desktop.messagebox, "showerror", dialog)
    app._drain_events()
    assert not app.print_loading
    assert dialog.call_args.args[0] == "In phiếu Traveloka"
    assert dialog.call_args.kwargs["parent"] is app.active_popup

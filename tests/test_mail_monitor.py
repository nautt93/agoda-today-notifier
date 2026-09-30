from __future__ import annotations

import ssl
from datetime import date

import pytest

from booking_notifier import mail_monitor
from booking_notifier.models import BOOKING_STATUS_MODIFIED, BookingEvent
from booking_notifier.state import StateStore


def test_lifecycle_notifications_only_affect_today():
    today = date(2026, 9, 30)
    assert mail_monitor.lifecycle_affects_today({today}, today)
    assert mail_monitor.lifecycle_affects_today({date(2026, 10, 1)}, today) is False
    assert mail_monitor.lifecycle_affects_today({today, date(2026, 10, 1)}, today)


@pytest.mark.parametrize("source", ["Agoda", "Expedia"])
def test_agoda_and_expedia_share_today_only_lifecycle_rule(tmp_path, source):
    today = date(2026, 9, 30)
    state = StateStore(tmp_path / f"{source}.json")
    initial = BookingEvent(source=source, booking_id="BOOKING-123", checkin_date=today)
    state.apply_event(initial, (f"{source}:new",))
    changed = BookingEvent(
        source=source,
        booking_id="BOOKING-123",
        status=BOOKING_STATUS_MODIFIED,
        checkin_date=date(2026, 10, 1),
    )
    affected = state.apply_event(changed, (f"{source}:modified",))
    assert affected == {today, date(2026, 10, 1)}
    assert mail_monitor.lifecycle_affects_today(affected, today)
    assert mail_monitor.lifecycle_affects_today(affected, date(2026, 10, 2)) is False


def test_imap_connection_requires_verified_tls(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, host, port, ssl_context, timeout):
            captured.update(host=host, port=port, context=ssl_context, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def login(self, address, password):
            captured.update(address=address, password=password)

        def select(self, mailbox, readonly):
            return "OK", [b"1"]

    monkeypatch.setattr(mail_monitor.imaplib, "IMAP4_SSL", FakeClient)
    mail_monitor.test_imap_connection(
        {"imap_host": "imap.example.com", "imap_port": 993, "email_address": "hotel@example.com"},
        "secret",
    )
    assert captured["context"].verify_mode == ssl.CERT_REQUIRED
    assert captured["context"].check_hostname is True
    assert captured["timeout"] == 30

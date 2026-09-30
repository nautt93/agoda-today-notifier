from __future__ import annotations

import queue
import ssl
from datetime import date
from email.message import EmailMessage

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


def test_agoda_email_to_pending_alert_survives_bad_email_and_deduplicates(tmp_path, monkeypatch):
    message = EmailMessage()
    message["From"] = "booking@agoda.com"
    message["Subject"] = "Agoda voucher"
    message["Message-ID"] = "<agoda-new@example>"
    message.set_content(f"Booking ID: 987654321\nCheck-in: {date.today().isoformat()}\nGuest Name: Jane Doe")
    raw = message.as_bytes()
    bad = raw.replace(b"<agoda-new@example>", b"<bad@example>")

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
            return "OK", [b"2"]

        def response(self, *args):
            return "OK", [b"123"]

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"1 2"]
            return "OK", [(b"BODY[]", bad if args[0] == b"1" else raw)]

    parse = mail_monitor.parse_booking_message

    def parse_with_bad_message(message):
        if message["Message-ID"] == "<bad@example>":
            raise ValueError("broken template")
        return parse(message)

    monkeypatch.setattr(mail_monitor.imaplib, "IMAP4_SSL", FakeClient)
    monkeypatch.setattr(mail_monitor, "parse_booking_message", parse_with_bad_message)
    state = StateStore(tmp_path / "state.json")
    events = queue.Queue()
    monitor = mail_monitor.ImapMonitor(
        {"imap_host": "example.com", "email_address": "hotel@example.com", "quiet_hours_enabled": False},
        "secret", state, events,
    )
    assert monitor.scan_mailbox() == 1
    emitted = list(events.queue)
    assert [payload.booking_id for kind, payload in emitted if kind == "alert"] == ["987654321"]
    assert len(state.pending_for_date(date.today())) == 1
    state.acknowledge(state.pending_for_date(date.today())[0])
    assert monitor.scan_mailbox() == 0
    assert len([kind for kind, _ in events.queue if kind == "alert"]) == 1

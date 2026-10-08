from __future__ import annotations

from datetime import date
from email.message import EmailMessage

import pytest
from test_mail_monitor import fake_inbox, make_monitor

from booking_notifier.booking_com import is_booking_com_nonbooking_notice
from booking_notifier.mail_monitor import PARSER_STATE_VERSION


def _message(subject: str, identifier: str, body: str = "Information for the property") -> EmailMessage:
    message = EmailMessage()
    message["From"] = "notify@booking.com"
    message["Subject"] = subject
    message["Message-ID"] = f"<{identifier}@example.invalid>"
    message.set_content(body)
    return message


@pytest.mark.parametrize("subject", [
    "Booking.com Newsletter",
    "Booking.com - Payment reminder: your invoice",
    "Review requested",
    "Booking.com - Bản tin tháng này",
    "Booking.com: Nhắc nhở thanh toán",
    "Booking.com - Yêu cầu đánh giá",
])
def test_recognized_unrelated_notice_is_fetched_once_across_scans(tmp_path, monkeypatch, subject):
    message = _message(subject, "unrelated")
    fetched = fake_inbox(monkeypatch, {1: message.as_bytes()})
    monitor, state, events = make_monitor(tmp_path)

    assert monitor.scan_mailbox() == 1
    assert monitor.scan_mailbox() == monitor.scan_mailbox() == 0

    assert fetched == [1]
    key = f"incremental:v1:{monitor.identity_hash}:123"
    assert state.mailbox_read_position(key) == (1, [])
    assert state.data["parse_failures"] == {}
    assert not any(kind in {"alert", "deferred_alert"} for kind, _ in events.queue)


def test_old_pending_unrelated_notice_finishes_without_reading_it_again(tmp_path, monkeypatch):
    monitor, state, _ = make_monitor(tmp_path)
    key = f"incremental:v1:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(key, [7], parser_version=PARSER_STATE_VERSION)
    fetched = fake_inbox(monkeypatch, {7: _message("Booking.com Newsletter", "old-unrelated").as_bytes()})

    assert monitor.scan_mailbox() == 1
    assert monitor.scan_mailbox() == 0
    assert fetched == [7]
    assert state.mailbox_read_position(key) == (7, [])


@pytest.mark.parametrize("subject", [
    "Booking.com - Đặt phòng mới! (5550000001)",
    "Booking.com New booking",
    "Booking.com Reservation confirmation",
    "Booking.com Newsletter: new reservation received",
])
def test_malformed_or_unknown_booking_stays_retryable_and_following_valid_email_alerts(tmp_path, monkeypatch, subject):
    messages = {1: _message(subject, "malformed").as_bytes()}
    fetched = fake_inbox(monkeypatch, messages)
    monitor, state, events = make_monitor(tmp_path)

    assert monitor.scan_mailbox() == 0
    assert monitor.scan_mailbox() == 0
    key = f"incremental:v1:{monitor.identity_hash}:123"
    assert state.mailbox_read_position(key) == (1, [1])
    assert [value["attempts"] for value in state.data["parse_failures"].values()] == [2]

    today = date.today()
    valid_subject = f"Booking.com - Đặt phòng mới! (5550000002, {today.day} tháng {today.month}, {today.year})"
    messages[2] = _message(valid_subject, "valid-today").as_bytes()
    assert monitor.scan_mailbox() == 1

    assert fetched == [1, 1, 2, 1]
    alerts = [event for kind, event in events.queue if kind == "alert"]
    assert [event.booking_id for event in alerts] == ["5550000002"]
    assert state.mailbox_read_position(key) == (2, [1])
    assert len(state.pending_for_date(today)) == 1


def test_unrelated_classifier_does_not_apply_to_other_sources_or_ambiguous_sender():
    for sender in ("notify@agoda.com", "notify@booking.com.evil.invalid", "notify@booking.com, other@example.invalid"):
        message = _message("Booking.com Newsletter", "other-source")
        message.replace_header("From", sender)
        assert not is_booking_com_nonbooking_notice(message)

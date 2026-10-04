from __future__ import annotations

import hashlib
import queue
from datetime import date, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path

import pytest

from booking_notifier import mail_monitor
from booking_notifier.excel_export import excel_tsv
from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore


def traveloka_email(booking_id="20261234000001", *, arrival=None, subject_prefix="CONFIRMED", copy=1,
                    message_id=True, sender="Traveloka <hotel@traveloka.com>"):
    arrival = arrival or date.today()
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = f"{subject_prefix} - Traveloka Itinerary ID {booking_id} (Test Hotel, VIETNAM)"
    if message_id:
        message["Message-ID"] = f"<traveloka-{booking_id}-copy-{copy}@example.invalid>"
    message["Date"] = f"Sun, 04 Oct 2026 09:{copy:02d}:00 +0700"
    message.set_content(
        f"Traveloka booking confirmation\nItinerary ID: {booking_id}\nGuest Name: Synthetic Full Guest\n"
        f"Check-in: {arrival.isoformat()}\nCheck-out: {(arrival + timedelta(days=2)).isoformat()}\n"
        "Room Type: Deluxe Double Room\nRooms: 2\nTotal you will receive: VND 400,000"
    )
    return message.as_bytes()


def fake_inbox(monkeypatch, messages, *, validity=None):
    fetched, searches = [], []

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
            assert kwargs["readonly"] is True
            return "OK", [str(len(messages)).encode()]

        def response(self, *args):
            return "OK", [validity[0] if validity else b"123"]

        def uid(self, command, *args):
            if command == "search":
                criteria = args[1:]
                searches.append(criteria)
                ordered = sorted(messages)
                if criteria[0] == "UID":
                    lower = int(criteria[1].split(":")[0])
                    selected = [uid for uid in ordered if uid >= lower] or ordered[-1:]
                elif criteria[0] in {"HEADER", "TEXT"}:
                    needle = criteria[-1].strip('"')
                    selected = []
                    for uid in ordered:
                        raw = messages[uid]
                        if isinstance(raw, Exception):
                            continue
                        message = message_from_bytes(raw, policy=policy.default)
                        text = str(message.get("Subject", "")) if criteria[0] == "HEADER" else raw.decode(errors="replace")
                        if needle in text:
                            selected.append(uid)
                else:
                    assert criteria[0] != "ALL" or not messages, "Do not search the full non-empty inbox"
                    lower = int(criteria[0].split(":")[0]) if messages else 1
                    selected = ordered[lower - 1:]
                return "OK", [b" ".join(str(uid).encode() for uid in selected)]
            assert command == "fetch" and args[1] == "(BODY.PEEK[])"
            uid = int(args[0])
            fetched.append(uid)
            raw = messages[uid]
            if isinstance(raw, Exception):
                raise raw
            return "OK", [(b"BODY[]", raw)]

    monkeypatch.setattr(mail_monitor.imaplib, "IMAP4_SSL", FakeClient)
    return fetched, searches


def make_monitor(tmp_path, state=None, events=None):
    state = state or StateStore(tmp_path / "state.json")
    events = events or queue.Queue()
    config = {"imap_host": "example.invalid", "email_address": "test@example.invalid", "quiet_hours_enabled": False}
    return mail_monitor.ImapMonitor(config, "synthetic-password", state, events), state, events


def emitted_alerts(events):
    return [payload for kind, payload in events.queue if kind == "alert"]


def test_anonymized_traveloka_template_reaches_pending_popup_with_full_name_room_and_excel(tmp_path, monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 4)

    monkeypatch.setattr(mail_monitor, "date", FixedDate)
    message = EmailMessage()
    message["From"] = "Traveloka <hotel@traveloka.com>"
    message["Subject"] = "CONFIRMED - Traveloka Itinerary ID 20261234000001 (Test Hotel, VIETNAM)"
    message["Message-ID"] = "<synthetic-traveloka-fixture@example.invalid>"
    message.set_content((Path(__file__).parent / "fixtures/traveloka_confirmation.html").read_text(encoding="utf-8"), subtype="html")
    fake_inbox(monkeypatch, {1: message.as_bytes()})
    monitor, state, events = make_monitor(tmp_path)
    assert monitor.scan_mailbox() == 1
    alert = emitted_alerts(events)[0]
    assert alert.source == "Traveloka" and alert.booking_id == "20261234000001"
    assert alert.guest_name == "Linh Trần" and alert.room_type == "Deluxe Double - ROOM ONLY x2"
    assert alert.checkin_date == date(2026, 10, 4) and alert.checkout_date == date(2026, 10, 6)
    assert alert.total_revenue == "VND 800,000"
    assert excel_tsv(alert).split("\t") == ["", "Linh Trần", "4", "6", "2", "800000", "", "", "Traveloka Deluxe Double - ROOM ONLY x2"]
    assert state.pending_for_date(FixedDate.today())[0].to_dict() == alert.to_dict()
    assert monitor.scan_mailbox() == 0 and len(emitted_alerts(events)) == 1


def test_traveloka_first_start_reads_only_latest_twenty_and_incremental_new_uid(tmp_path, monkeypatch):
    messages = {uid: f"From: friend@example.invalid\r\nSubject: Hello\r\nMessage-ID: <nonbooking-{uid}@example.invalid>\r\n\r\nHello".encode()
                for uid in range(1, 2001)}
    messages[1] = traveloka_email("20261234000000")  # Older than the bootstrap window: never download it.
    messages[2000] = traveloka_email()
    fetched, searches = fake_inbox(monkeypatch, messages)
    monitor, state, events = make_monitor(tmp_path)
    assert monitor.scan_mailbox() == 20
    assert fetched == list(range(2000, 1980, -1)) and searches == [("1981:*",)]
    assert [event.source for event in emitted_alerts(events)] == ["Traveloka"]
    assert emitted_alerts(events)[0].guest_name == "Synthetic Full Guest"
    assert emitted_alerts(events)[0].room_type == "Deluxe Double Room x2"
    fetched.clear()
    messages[2001] = traveloka_email("20261234000002")
    assert monitor.scan_mailbox() == 1 and fetched == [2001]
    assert searches[-1] == ("UID", "2001:*")
    assert len(state.pending_for_date(date.today())) == 2
    fetched.clear()
    assert monitor.scan_mailbox() == 0 and fetched == []


def test_traveloka_payment_receipt_finishes_once_without_popup_or_infinite_retry(tmp_path, monkeypatch):
    message = message_from_bytes(traveloka_email(), policy=policy.default)
    message.replace_header("Subject", "PAYMENT COMPLETED - Payment ID 1779000000000001")
    fetched, _ = fake_inbox(monkeypatch, {1: message.as_bytes()})
    monitor, state, events = make_monitor(tmp_path)
    assert monitor.scan_mailbox() == 1 and fetched == [1]
    assert not emitted_alerts(events) and not state.pending_for_date(date.today())
    key = f"incremental:v1:{monitor.identity_hash}:123"
    assert state.mailbox_read_position(key) == (1, [])
    assert not state.data["parse_failures"]
    fetched.clear()
    assert monitor.scan_mailbox() == 0 and fetched == []


def test_p11_upgrade_refreshes_known_booking_without_reopening_or_resetting_cursor(tmp_path, monkeypatch):
    monitor, state, events = make_monitor(tmp_path)
    booking = BookingEvent(source="Traveloka", booking_id="20261234000001", checkin_date=date.today(),
                           guest_name="Legacy Short", room_type="Old Room x2")
    state.acknowledge(state.register_today_confirmation(booking, ("old-mail",), date.today()))
    key = f"incremental:v1:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(key, [], minimum_cursor=100, parser_version="p11")
    state.remember_processed_aliases(f"uid:p11:{monitor.identity_hash}:123:1")
    fetched, _ = fake_inbox(monkeypatch, {1: traveloka_email()})
    assert monitor.scan_mailbox() == 1 and fetched == [1]
    assert state.history()[0]["guest_name"] == "Synthetic Full Guest"
    assert state.history()[0]["room_type"] == "Deluxe Double Room x2"
    assert state.mailbox_read_position(key) == (100, [])
    assert state.mailbox_parser_version(key) == mail_monitor.PARSER_STATE_VERSION
    assert not emitted_alerts(events) and not state.pending_for_date(date.today())
    fetched.clear()
    assert monitor.scan_mailbox() == 0 and fetched == []


@pytest.mark.parametrize("has_message_id", [True, False])
def test_traveloka_resends_stay_deduplicated_after_close_restart_and_uidvalidity_change(tmp_path, monkeypatch, has_message_id):
    messages = {1: traveloka_email(copy=1, message_id=has_message_id),
                2: traveloka_email(copy=2, message_id=has_message_id)}
    validity = [b"123"]
    fake_inbox(monkeypatch, messages, validity=validity)
    monitor, state, events = make_monitor(tmp_path)
    assert monitor.scan_mailbox() == 2
    assert len(emitted_alerts(events)) == len(state.pending_for_date(date.today())) == 1
    state.acknowledge(emitted_alerts(events)[0])
    history = state.history()
    messages[3] = traveloka_email(copy=3, message_id=has_message_id)
    monitor, state, _ = make_monitor(tmp_path, StateStore(state.path), events)
    assert monitor.scan_mailbox() == 1
    assert state.history() == history and not state.pending_for_date(date.today())
    assert len(emitted_alerts(events)) == 1
    validity[0] = b"456"
    assert monitor.scan_mailbox() == 0 and len(emitted_alerts(events)) == 1
    messages[4] = traveloka_email("20261234000002", copy=4, message_id=has_message_id)
    assert monitor.scan_mailbox() == 1
    assert [event.booking_id for event in emitted_alerts(events)] == ["20261234000001", "20261234000002"]


def test_traveloka_ignores_other_days_lifecycle_notices_and_spoofed_sender(tmp_path, monkeypatch):
    messages = {
        1: traveloka_email("20261234000001"),
        2: traveloka_email("20261234000002", arrival=date.today() + timedelta(days=1)),
        3: traveloka_email("20261234000003", arrival=date.today() - timedelta(days=1)),
        4: traveloka_email("20261234000004", subject_prefix="MODIFIED"),
        5: traveloka_email("20261234000005", subject_prefix="CANCELLED"),
        6: traveloka_email("20261234000006", sender="Traveloka <hotel@traveloka.com.evil.invalid>"),
        7: traveloka_email("20261234000007", sender='"Traveloka hotel@traveloka.com" <attacker@example.invalid>'),
        8: traveloka_email("20261234000008"),
    }
    fake_inbox(monkeypatch, messages)
    monitor, state, events = make_monitor(tmp_path)
    assert monitor.scan_mailbox() == len(messages)
    assert [event.booking_id for event in emitted_alerts(events)] == ["20261234000008", "20261234000001"]
    assert set(state.data["bookings"]) == {"traveloka:20261234000001", "traveloka:20261234000008"}
    assert monitor.scan_mailbox() == 0


def test_traveloka_pending_reads_survive_failed_fetch_and_restart_without_resetting_cursor(tmp_path, monkeypatch):
    monitor, state, events = make_monitor(tmp_path)
    key = f"incremental:v1:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(key, list(range(101, 111)), parser_version=mail_monitor.PARSER_STATE_VERSION)
    messages = {uid: traveloka_email(str(20261234000000 + uid)) for uid in range(101, 111)}
    messages[105] = RuntimeError("temporary fetch failure")
    fetched, searches = fake_inbox(monkeypatch, messages)
    assert monitor.scan_mailbox() == 9
    assert searches == [("UID", "111:*")] and fetched == list(range(110, 100, -1))
    assert state.mailbox_read_position(key) == (110, [105]) and len(emitted_alerts(events)) == 9
    messages[105] = traveloka_email(str(20261234000000 + 105))
    messages[111] = traveloka_email(str(20261234000000 + 111))
    fetched.clear()
    monitor, state, _ = make_monitor(tmp_path, StateStore(state.path), events)
    assert monitor.scan_mailbox() == 2 and fetched == [111, 105]
    assert state.mailbox_read_position(key) == (111, [])
    assert len(emitted_alerts(events)) == len(state.pending_for_date(date.today())) == 11


def test_traveloka_targeted_detail_repair_does_not_realert_or_scan_full_mailbox(tmp_path, monkeypatch):
    monitor, state, events = make_monitor(tmp_path)
    today = date.today()
    incomplete = BookingEvent(source="Traveloka", booking_id="20261234000001", checkin_date=today, guest_name="Synthetic")
    state.acknowledge(state.register_today_confirmation(incomplete, ("old-traveloka",), today))
    key = f"incremental:v1:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(key, [], minimum_cursor=100, parser_version=mail_monitor.PARSER_STATE_VERSION)
    fetched, searches = fake_inbox(monkeypatch, {1: traveloka_email()})
    assert monitor.scan_mailbox() == 0
    assert fetched == [1] and searches == [("UID", "101:*"), ("TEXT", '"20261234000001"')]
    assert state.history()[0]["guest_name"] == "Synthetic Full Guest"
    assert state.history()[0]["room_type"] == "Deluxe Double Room x2"
    assert state.incomplete_confirmations(today) == [] and not emitted_alerts(events)
    assert state.mailbox_read_position(key) == (100, [])


@pytest.mark.parametrize("arrival_offset", [0, 1, -1])
def test_p10_upgrade_recognizes_previous_traveloka_only_today_and_preserves_cursor_pending_history(tmp_path, monkeypatch, arrival_offset):
    monitor, state, events = make_monitor(tmp_path)
    arrival = date.today() + timedelta(days=arrival_offset)
    messages = {uid: f"From: friend@example.invalid\r\nSubject: Hello\r\nMessage-ID: <nonbooking-{uid}@example.invalid>\r\n\r\nHello".encode()
                for uid in range(1, 31)}
    messages[5] = traveloka_email("20261234000005")  # Unfinished body outside the recent-20 repair window.
    messages[12] = traveloka_email("20261234000012", arrival=arrival)
    agoda = EmailMessage()
    agoda["From"], agoda["Subject"], agoda["Message-ID"] = "booking@agoda.com", "Agoda booking confirmation", "<old-agoda@example.invalid>"
    agoda.set_content(f"Booking ID: 123456789\nGuest Name: Synthetic Full Guest\nRoom Type: Deluxe Double Room\nRooms: 2\nCheck-in: {date.today().isoformat()}")
    messages[30] = agoda.as_bytes()
    known = BookingEvent(source="Agoda", booking_id="123456789", checkin_date=date.today(), guest_name="Synthetic Full Guest", room_type="Deluxe Double Room x2",
                         subject="Agoda booking confirmation", sender="booking@agoda.com")
    state.acknowledge(state.register_today_confirmation(known, ("old-confirmation",), date.today()))
    old_history = state.history()
    message_id = str(message_from_bytes(messages[12], policy=policy.default)["Message-ID"])
    # The older parser marked unknown Traveloka as processed; retry only recent bodies.
    state.remember_processed_aliases(f"uid:p10:{monitor.identity_hash}:123:12",
                                    f"msg:p10:{monitor.identity_hash}:{hashlib.sha256(message_id.encode()).hexdigest()}")
    key = f"incremental:v1:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(key, [5], minimum_cursor=100, parser_version="p10")
    fetched, searches = fake_inbox(monkeypatch, messages)
    assert mail_monitor.PARSER_STATE_VERSION != "p10"
    assert monitor.scan_mailbox() == 21
    assert searches == [("UID", "101:*"), ("11:*",)]
    assert fetched == [*range(30, 10, -1), 5]
    assert state.mailbox_read_position(key) == (100, [])
    assert state.mailbox_parser_version(key) == mail_monitor.PARSER_STATE_VERSION
    assert state.history() == old_history
    expected_ids = ["20261234000012", "20261234000005"] if arrival_offset == 0 else ["20261234000005"]
    assert [event.booking_id for event in emitted_alerts(events)] == expected_ids
    fetched.clear()
    monitor, state, _ = make_monitor(tmp_path, StateStore(state.path), events)
    assert monitor.scan_mailbox() == 0 and fetched == []
    assert state.mailbox_read_position(key) == (100, [])


def test_same_itinerary_id_across_providers_and_same_guest_different_bookings_are_distinct(tmp_path):
    state = StateStore(tmp_path / "state.json")
    for source in ("Agoda", "Expedia", "Traveloka"):
        event = BookingEvent(source=source, booking_id="20261234000001", checkin_date=date.today(), guest_name="Synthetic Guest")
        alert = state.register_today_confirmation(event, (f"{source}:mail",), date.today())
        assert alert is not None
        state.acknowledge(alert)
        assert StateStore(state.path).register_today_confirmation(event, (f"{source}:resend",), date.today()) is None
    other = BookingEvent(source="Traveloka", booking_id="20261234000002", checkin_date=date.today(), guest_name="Synthetic Guest")
    assert state.register_today_confirmation(other, ("other-mail",), date.today()) is not None
    assert len(state.history()) == 3 and len(state.pending_for_date(date.today())) == 1

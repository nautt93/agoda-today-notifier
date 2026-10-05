from __future__ import annotations

import hashlib
import queue
import re
from datetime import date, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path

import pytest

from booking_notifier import mail_monitor
from booking_notifier.excel_export import excel_amount_value, excel_tsv
from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore

FIXTURES = Path(__file__).parent / "fixtures"
IDENTIFIER = "1666000000000001"
ARRIVAL = date(2026, 10, 5)


def trip_email(identifier=IDENTIFIER, *, arrival=None, subject_prefix="", reservation_type="New(0)", copy=1,
               has_message_id=True, sender='"Trip.com" <notify@trip.com>'):
    arrival = arrival or date.today()
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = f"{subject_prefix}Trip.com New Reservation: {identifier}"
    if has_message_id:
        message["Message-ID"] = f"<trip-{identifier}-copy-{copy}@example.invalid>"
    message["Date"] = f"Mon, 05 Oct 2026 09:{copy:02d}:00 +0700"
    message.set_content(f"Reservation type: {reservation_type}\nReservation: {identifier}\nGuest name: SYNTHETIC/GUEST\n"
                        f"Check-in date: {arrival.isoformat()}\nCheck-out date: {(arrival + timedelta(days=2)).isoformat()}\n"
                        "Room type: Deluxe Double Room\nRoom(s): 2\nTotal amount: VND 800,000.00")
    return message


def fixture_email(kind, *, copy=1, reminder_gross=False):
    message = EmailMessage()
    message["From"] = '"Trip.com" <notify@trip.com>'
    message["Subject"] = (f"Urgent action required - new booking received (booking no. #{IDENTIFIER}#)"
                          if kind == "original" else f"[Reminder] Trip.com New Reservation: {IDENTIFIER}")
    message["Message-ID"] = f"<trip-{kind}-copy-{copy}@example.invalid>"
    message["Date"] = f"Mon, 05 Oct 2026 10:{copy:02d}:00 +0700"
    filename = "trip_new_booking.html" if kind == "original" else "trip_reminder.html"
    body = (FIXTURES / filename).read_text(encoding="utf-8")
    if reminder_gross:
        body, changed = re.subn(r"800,?000(?:\.00)?", "1,000,000.00", body)
        assert changed, "The synthetic reminder must exercise gross amount different from original payout"
    message.set_content(body, subtype="html")
    return message


def fake_inbox(monkeypatch, messages):
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
            assert kwargs["readonly"]
            return "OK", [str(len(messages)).encode()]

        def response(self, *args):
            return "OK", [b"123"]

        def uid(self, command, *args):
            if command == "search":
                criteria, ordered = args[1:], sorted(messages)
                searches.append(criteria)
                if criteria[0] == "UID":
                    lower = int(criteria[1].split(":")[0])
                    selected = [uid for uid in ordered if uid >= lower] or ordered[-1:]
                elif criteria[0] in {"HEADER", "TEXT"}:
                    needle = criteria[-1].strip('"')
                    selected = [uid for uid in ordered if needle in messages[uid].decode(errors="replace")]
                else:
                    assert criteria[0] != "ALL" or not messages
                    selected = ordered[int(criteria[0].split(":")[0]) - 1:] if messages else []
                return "OK", [b" ".join(str(uid).encode() for uid in selected)]
            assert command == "fetch" and args[1] == "(BODY.PEEK[])"
            uid = int(args[0])
            fetched.append(uid)
            return "OK", [(b"BODY[]", messages[uid])]

    monkeypatch.setattr(mail_monitor.imaplib, "IMAP4_SSL", FakeClient)
    return fetched, searches


def monitor_for(tmp_path, state=None, events=None):
    state, events = state or StateStore(tmp_path / "state.json"), events or queue.Queue()
    config = {"imap_host": "example.invalid", "email_address": "hotel@example.invalid", "quiet_hours_enabled": False}
    return mail_monitor.ImapMonitor(config, "synthetic-password", state, events), state, events


def alerts(events):
    return [event for kind, event in events.queue if kind == "alert"]


def use_fixture_date(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 5)

    monkeypatch.setattr(mail_monitor, "date", FixedDate)


def assert_original_details(record, original):
    assert record["guest_name"] == original.guest_name
    assert record["room_type"] == original.room_type
    assert excel_amount_value(record["total_revenue"]) == "800000"
    assert record["subject"] == original.subject


@pytest.mark.parametrize("newest", ["original", "reminder"])
def test_trip_original_and_reminder_both_arrival_orders_notify_once_and_keep_original_net_room(tmp_path, monkeypatch, newest):
    use_fixture_date(monkeypatch)
    original, reminder = fixture_email("original"), fixture_email("reminder", reminder_gross=True)
    reference = mail_monitor.parse_booking_message(original)
    assert reference.source == "Trip" and excel_amount_value(reference.total_revenue) == "800000"
    ordered = [reminder, original] if newest == "original" else [original, reminder]
    messages = {index: message.as_bytes() for index, message in enumerate(ordered, 1)}
    fake_inbox(monkeypatch, messages)
    monitor, state, events = monitor_for(tmp_path)
    assert monitor.scan_mailbox() == 2
    assert len(alerts(events)) == len(state.pending_for_date(ARRIVAL)) == 1
    assert alerts(events)[0].booking_id == IDENTIFIER
    assert_original_details(state.data["bookings"][f"trip:{IDENTIFIER}"], reference)
    assert_original_details(state.pending_for_date(ARRIVAL)[0].to_dict(), reference)
    # A stale popup first created from Reminder must not overwrite later original repair on close.
    state.acknowledge(alerts(events)[0])
    assert_original_details(state.history()[0], reference)
    messages[3] = fixture_email("reminder", copy=2, reminder_gross=True).as_bytes()
    messages[4] = fixture_email("original", copy=2).as_bytes()
    monitor, state, _ = monitor_for(tmp_path, StateStore(state.path), events)
    assert monitor.scan_mailbox() == 2
    assert len(alerts(events)) == len(state.history()) == 1 and not state.pending_for_date(ARRIVAL)
    assert_original_details(state.history()[0], reference)


@pytest.mark.parametrize("first", ["original", "reminder"])
def test_trip_original_or_reminder_after_acknowledgement_repairs_without_reopening(tmp_path, monkeypatch, first):
    use_fixture_date(monkeypatch)
    reference = mail_monitor.parse_booking_message(fixture_email("original"))
    messages = {1: fixture_email(first, reminder_gross=first == "reminder").as_bytes()}
    fake_inbox(monkeypatch, messages)
    monitor, state, events = monitor_for(tmp_path)
    assert monitor.scan_mailbox() == 1
    state.acknowledge(alerts(events)[0])
    second = "reminder" if first == "original" else "original"
    messages[2] = fixture_email(second, copy=2, reminder_gross=second == "reminder").as_bytes()
    monitor, state, _ = monitor_for(tmp_path, StateStore(state.path), events)
    assert monitor.scan_mailbox() == 1 and len(alerts(events)) == 1
    assert len(state.history()) == 1 and not state.pending_for_date(ARRIVAL)
    assert_original_details(state.history()[0], reference)
    assert_original_details(state.data["bookings"][f"trip:{IDENTIFIER}"], reference)


@pytest.mark.parametrize("has_message_id", [True, False])
def test_trip_resend_new_uid_and_restart_cannot_duplicate_pending_or_acknowledged_booking(tmp_path, monkeypatch, has_message_id):
    messages = {1: trip_email(copy=1, has_message_id=has_message_id).as_bytes(),
                2: trip_email(copy=2, subject_prefix="[Reminder] ", has_message_id=has_message_id).as_bytes()}
    fake_inbox(monkeypatch, messages)
    monitor, state, events = monitor_for(tmp_path)
    assert monitor.scan_mailbox() == 2 and len(alerts(events)) == 1
    state.acknowledge(alerts(events)[0])
    messages[3] = trip_email(copy=3, subject_prefix="[Reminder] ", has_message_id=has_message_id).as_bytes()
    monitor, state, _ = monitor_for(tmp_path, StateStore(state.path), events)
    assert monitor.scan_mailbox() == 1
    assert len(alerts(events)) == len(state.history()) == 1 and not state.pending_for_date(date.today())


def test_trip_latest_twenty_bootstrap_then_new_uids_only(tmp_path, monkeypatch):
    messages = {uid: f"From: friend@example.invalid\r\nSubject: Hello\r\nMessage-ID: <filler-{uid}@example.invalid>\r\n\r\nHello".encode()
                for uid in range(1, 2001)}
    messages[1] = trip_email("1666000000000000").as_bytes()
    messages[2000] = trip_email().as_bytes()
    fetched, searches = fake_inbox(monkeypatch, messages)
    monitor, state, events = monitor_for(tmp_path)
    assert monitor.scan_mailbox() == 20 and fetched == list(range(2000, 1980, -1))
    assert searches == [("1981:*",)] and len(alerts(events)) == 1
    fetched.clear()
    messages[2001] = trip_email("1666000000000002").as_bytes()
    monitor, state, _ = monitor_for(tmp_path, StateStore(state.path), events)
    assert monitor.scan_mailbox() == 1 and fetched == [2001]
    assert searches[-1] == ("UID", "2001:*") and len(state.pending_for_date(date.today())) == 2


def test_trip_only_new_arriving_today_alerts_not_modified_cancelled_other_days_or_spoofed_sender(tmp_path, monkeypatch):
    messages = {
        1: trip_email().as_bytes(),
        2: trip_email("1666000000000002", arrival=date.today() + timedelta(days=1)).as_bytes(),
        3: trip_email("1666000000000003", arrival=date.today() - timedelta(days=1)).as_bytes(),
        4: trip_email("1666000000000004", subject_prefix="Modified reservation - ", reservation_type="Modified").as_bytes(),
        5: trip_email("1666000000000005", subject_prefix="Cancelled reservation - ", reservation_type="Cancelled").as_bytes(),
        6: trip_email("1666000000000006", sender="Trip.com <notify@trip.com.evil.invalid>").as_bytes(),
        7: trip_email("1666000000000007", sender='"Trip.com notify@trip.com" <attacker@example.invalid>').as_bytes(),
    }
    fake_inbox(monkeypatch, messages)
    monitor, state, events = monitor_for(tmp_path)
    assert monitor.scan_mailbox() == len(messages)
    assert [event.booking_id for event in alerts(events)] == [IDENTIFIER]
    assert set(state.data["bookings"]) == {f"trip:{IDENTIFIER}"}
    assert monitor.scan_mailbox() == 0


@pytest.mark.parametrize("trip_already_acknowledged", [False, True])
def test_p12_to_p13_rereads_processed_trip_latest_twenty_once_preserves_cursor_pending_and_ack_guards(tmp_path, monkeypatch, trip_already_acknowledged):
    use_fixture_date(monkeypatch)
    monitor, state, events = monitor_for(tmp_path)
    original, reminder = fixture_email("original"), fixture_email("reminder", reminder_gross=True)
    reference = mail_monitor.parse_booking_message(original)
    messages = {uid: f"From: friend@example.invalid\r\nSubject: Hello\r\nMessage-ID: <filler-{uid}@example.invalid>\r\n\r\nHello".encode()
                for uid in range(1, 31)}
    messages[5] = trip_email("1666000000000005", arrival=ARRIVAL).as_bytes()
    messages[12], messages[30] = reminder.as_bytes(), original.as_bytes()
    known = BookingEvent(source="Agoda", booking_id="123456789", checkin_date=ARRIVAL, guest_name="Synthetic Guest", room_type="Deluxe x1")
    state.acknowledge(state.register_today_confirmation(known, ("old-agoda",), ARRIVAL))
    if trip_already_acknowledged:
        state.acknowledge(state.register_today_confirmation(reference, ("old-trip",), ARRIVAL))
    old_history_count = len(state.history())
    for uid in (12, 30):
        identifier = str(message_from_bytes(messages[uid], policy=policy.default)["Message-ID"])
        state.remember_processed_aliases(f"uid:p12:{monitor.identity_hash}:123:{uid}",
                                        f"msg:p12:{monitor.identity_hash}:{hashlib.sha256(identifier.encode()).hexdigest()}")
    key = f"incremental:v1:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(key, [5], minimum_cursor=100, parser_version="p12")
    fetched, searches = fake_inbox(monkeypatch, messages)
    assert mail_monitor.PARSER_STATE_VERSION != "p12"
    assert monitor.scan_mailbox() == 21
    assert searches == [("UID", "101:*"), ("11:*",)] and fetched == [*range(30, 10, -1), 5]
    assert state.mailbox_read_position(key) == (100, [])
    assert state.mailbox_parser_version(key) == mail_monitor.PARSER_STATE_VERSION
    expected_ids = ["1666000000000005"] if trip_already_acknowledged else [IDENTIFIER, "1666000000000005"]
    assert [event.booking_id for event in alerts(events)] == expected_ids
    assert len(state.history()) == old_history_count
    assert_original_details(state.data["bookings"][f"trip:{IDENTIFIER}"], reference)
    fetched.clear()
    monitor, state, _ = monitor_for(tmp_path, StateStore(state.path), events)
    assert monitor.scan_mailbox() == 0 and fetched == [] and state.mailbox_read_position(key) == (100, [])


def test_trip_source_identity_isolated_and_clipboard_keeps_original_nine_columns(tmp_path):
    state = StateStore(tmp_path / "state.json")
    for source in ("Agoda", "Expedia", "Traveloka", "Trip"):
        event = BookingEvent(source=source, booking_id=IDENTIFIER, checkin_date=ARRIVAL, guest_name="Synthetic Guest")
        alert = state.register_today_confirmation(event, (f"{source}:mail",), ARRIVAL)
        assert alert is not None
        state.acknowledge(alert)
    assert len(state.history()) == 4
    event = BookingEvent(source="Trip", booking_id=IDENTIFIER, guest_name="TRẦN/LINH", checkin_date=ARRIVAL,
                         checkout_date=date(2026, 10, 7), room_type="Deluxe Double Room x2", total_revenue="VND 800,000.00")
    assert excel_tsv(event).split("\t") == ["", "TRẦN/LINH", "5", "7", "2", "800000", "", "", "Trip Deluxe Double Room x2"]

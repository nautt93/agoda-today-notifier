from __future__ import annotations

import queue
import ssl
from datetime import date, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path

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


def recent_message(source="Agoda", arrival=None, booking_id="987654321", subject="Booking confirmation"):
    message = EmailMessage()
    message["From"] = "booking@agoda.com" if source == "Agoda" else "notify@expediapartnercentral.com"
    message["Subject"] = subject
    message["Message-ID"] = f"<{source}-{booking_id}@example>"
    label = "Booking ID" if source == "Agoda" else "Itinerary ID"
    message.set_content(f"{label}: {booking_id}\nGuest Name: Jane Doe\nCheck-in: {arrival or date.today().isoformat()}\nRoom Type: Standard Room\nRooms: 1")
    return message.as_bytes()


def fake_inbox(monkeypatch, messages, before_fetch=None, uid_validity=None, searches=None):
    fetched = []

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
            return "OK", [str(len(messages)).encode()]

        def response(self, *args):
            return "OK", [uid_validity[0] if uid_validity is not None else b"123"]

        def uid(self, command, *args):
            if command == "search":
                if searches is not None:
                    searches.append(args[1:])
                assert "FROM" not in args
                ordered = sorted(messages)
                if args[1] == "UID":
                    lower = int(args[2].split(":")[0])
                    selected = [uid for uid in ordered if uid >= lower]
                    # Simulate IMAP's surprising reversed-range behavior as well.
                    if not selected:
                        selected = ordered[-1:]
                elif args[1] == "ALL":
                    selected = ordered
                elif args[1] in {"HEADER", "TEXT"}:
                    needle = args[-1].strip('"')
                    selected = []
                    for uid in ordered:
                        raw = messages[uid]
                        if isinstance(raw, Exception):
                            continue
                        message = message_from_bytes(raw, policy=policy.default)
                        searchable = str(message.get("Subject", "")) if args[1] == "HEADER" else raw.decode("utf-8", errors="replace")
                        if needle in searchable:
                            selected.append(uid)
                else:
                    lower = int(args[1].split(":")[0])
                    selected = ordered[lower - 1:]
                return "OK", [b" ".join(str(uid).encode() for uid in selected)]
            assert command == "fetch"
            assert args[1] == "(BODY.PEEK[])"  # No extra header fetch/filter.
            uid = int(args[0])
            if before_fetch:
                before_fetch(uid)
            fetched.append(uid)
            raw = messages[uid]
            if isinstance(raw, Exception):
                raise raw
            return "OK", [(b"BODY[]", raw)]

    monkeypatch.setattr(mail_monitor.imaplib, "IMAP4_SSL", FakeClient)
    return fetched


def make_monitor(tmp_path, **config):
    state = StateStore(tmp_path / "state.json")
    events = queue.Queue()
    monitor = mail_monitor.ImapMonitor({
        "imap_host": "example.com", "email_address": "hotel@example.com", "quiet_hours_enabled": False,
        **config,
    }, "secret", state, events)
    return monitor, state, events


@pytest.mark.parametrize("has_message_id", [True, False])
def test_expedia_resends_with_different_uids_do_not_notify_twice_after_restart(tmp_path, monkeypatch, has_message_id):
    def email_copy(copy_number, booking_id="1234567890"):
        message = message_from_bytes(recent_message(source="Expedia", booking_id=booking_id), policy=policy.default)
        if has_message_id:
            message.replace_header("Message-ID", f"<resend-{copy_number}@example.invalid>")
        else:
            del message["Message-ID"]
        message["Date"] = f"Sun, 04 Oct 2026 09:{copy_number:02d}:00 +0700"
        return message.as_bytes()

    inbox = {1: email_copy(1), 2: email_copy(2)}
    fake_inbox(monkeypatch, inbox)
    monitor, state, events = make_monitor(tmp_path)
    assert monitor.scan_mailbox() == 2
    alerts = [payload for kind, payload in events.queue if kind == "alert"]
    assert len(alerts) == 1 and len(state.pending_for_date(date.today())) == 1
    state.acknowledge(alerts[0])
    saved_history = state.history()
    # A fresh IMAP UID and Message-ID (or changed raw content) still has the same booking ID.
    inbox[3] = email_copy(3)
    resumed = mail_monitor.ImapMonitor(monitor.config, "secret", StateStore(state.path), events)
    assert resumed.scan_mailbox() == 1
    assert len([kind for kind, _ in events.queue if kind == "alert"]) == 1
    assert resumed.state.history() == saved_history and not resumed.state.pending_for_date(date.today())
    # A rebuilt mailbox can give all copies new UIDs; the persisted booking guard survives.
    fake_inbox(monkeypatch, inbox, uid_validity=[b"456"])
    assert resumed.scan_mailbox() == 0  # Previously processed Message-IDs/raw copies retain their aliases.
    assert len([kind for kind, _ in events.queue if kind == "alert"]) == 1
    # Never suppress a different reservation just because the guest name is identical.
    inbox[4] = email_copy(4, booking_id="1234567891")
    assert resumed.scan_mailbox() == 1
    assert [payload.booking_id for kind, payload in events.queue if kind == "alert"] == ["1234567890", "1234567891"]


def test_ten_unfinished_emails_resume_despite_legacy_history_without_booking_id(tmp_path, monkeypatch):
    monitor, state, events = make_monitor(tmp_path)
    today = date.today()
    legacy = {"guest_name": "Legacy Test Guest", "checkin_date": "30/09/2026", "custom": "preserve"}
    state.data["history"].append(legacy)
    mailbox_key = f"incremental:v1:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(mailbox_key, list(range(101, 111)), parser_version=mail_monitor.PARSER_STATE_VERSION)
    # The screenshot has zero new emails, with ten unfinished UIDs still pending.
    messages = {uid: recent_message("Agoda" if uid % 2 else "Expedia", booking_id=str(1000000 + uid))
                for uid in range(101, 111)}
    searches = []
    fetched = fake_inbox(monkeypatch, messages, searches=searches)
    assert monitor.scan_mailbox() == 10
    assert fetched == list(range(110, 100, -1))
    assert searches == [("UID", "111:*")]
    assert state.mailbox_read_position(mailbox_key) == (110, [])
    alerts = [payload for kind, payload in events.queue if kind == "alert"]
    assert len(alerts) == 10 and all(alert.checkin_date == today for alert in alerts)
    assert {alert.source for alert in alerts} == {"Agoda", "Expedia"}
    assert state.history() == [legacy]
    assert len(state.pending_for_date(today)) == 10
    for alert in alerts:
        state.acknowledge(alert)
    reloaded = StateStore(state.path)
    assert len(reloaded.history()) == 11 and reloaded.history()[-1] == legacy
    assert reloaded.pending_for_date(today) == []
    fetched.clear()
    assert monitor.scan_mailbox() == 0 and fetched == []
    assert len([kind for kind, _ in events.queue if kind == "alert"]) == 10


def test_agoda_confirmation_without_id_does_not_block_next_booking(tmp_path, monkeypatch):
    monitor, state, events = make_monitor(tmp_path)
    raw = recent_message(booking_id="", subject="Agoda new booking confirmation")
    messages = {2: raw, 1: recent_message("Expedia", booking_id="1234567890")}
    fetched = fake_inbox(monkeypatch, messages)
    assert monitor.scan_mailbox() == 2 and fetched == [2, 1]
    alerts = [payload for kind, payload in events.queue if kind == "alert"]
    assert [(alert.source, alert.booking_id) for alert in alerts] == [("Agoda", ""), ("Expedia", "1234567890")]
    assert len(state.pending_for_date(date.today())) == 2
    for alert in alerts:
        state.acknowledge(alert)
    reloaded = StateStore(state.path)
    assert len(reloaded.history()) == 2 and reloaded.pending_for_date(date.today()) == []
    assert monitor.scan_mailbox() == 0
    assert len([kind for kind, _ in events.queue if kind == "alert"]) == 2


def test_legacy_no_id_row_does_not_turn_other_days_or_lifecycle_into_alerts(tmp_path, monkeypatch):
    monitor, state, events = make_monitor(tmp_path)
    legacy = {"guest_name": "Legacy Test Guest", "checkin_date": date.today().isoformat()}
    state.data["history"].append(legacy)
    fake_inbox(monkeypatch, {
        4: recent_message(booking_id="", arrival=(date.today() + timedelta(days=1)).isoformat()),
        3: recent_message(booking_id="1000003", subject="Booking cancelled"),
        2: recent_message("Expedia", booking_id="1000002", subject="Reservation modified"),
        1: recent_message("Expedia", booking_id="1000001"),
    })
    assert monitor.scan_mailbox() == 4
    alerts = [payload for kind, payload in events.queue if kind == "alert"]
    assert [alert.booking_id for alert in alerts] == ["1000001"]
    assert len(state.pending_for_date(date.today())) == 1 and state.history() == [legacy]


@pytest.mark.parametrize("source", ["Agoda", "Expedia"])
def test_recover_four_cached_rows_outside_recent_twenty_without_realert(tmp_path, monkeypatch, source):
    monitor, state, events = make_monitor(tmp_path)
    today = date.today()
    messages = {uid: b"From: newsletter@example.com\r\nSubject: News\r\n\r\nNews" for uid in range(5, 101)}
    fixture = "agoda_bilingual_confirmation.html" if source == "Agoda" else "expedia_new_booking.html"
    html = (Path(__file__).parent / "fixtures" / fixture).read_text(encoding="utf-8")
    original_id = "987654321" if source == "Agoda" else "1234567890"
    for uid in range(1, 5):
        booking_id = str(1000000 + uid)
        incomplete = BookingEvent(source=source, booking_id=booking_id, checkin_date=today, guest_name="Minh")
        alert = state.register_today_confirmation(incomplete, (mail_monitor.mailbox_uid_key(monitor.identity_hash, "123", str(uid)),), today)
        if uid <= 2:
            state.acknowledge(alert)
        message = EmailMessage()
        message["From"] = "booking@agoda.com" if source == "Agoda" else "booknotif@expedia.com"
        message["Subject"] = f"{source} Booking ID {booking_id} - CONFIRMED"
        body = html.replace(original_id, booking_id)
        body = body.replace("30-Sep-2026 (30-09-2026)", today.isoformat())
        body = body.replace("Oct 13, 2026", today.isoformat())
        body = body.replace("Oct 14, 2026", (today + timedelta(days=1)).isoformat())
        message.set_content(body, subtype="html")
        messages[uid] = message.as_bytes()
    mailbox_key = f"incremental:v1:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(mailbox_key, [], minimum_cursor=100, parser_version=mail_monitor.PARSER_STATE_VERSION)
    searches = []
    fetched = fake_inbox(monkeypatch, messages, searches=searches)
    assert monitor.scan_mailbox() == 0  # No new arrivals; targeted recovery only.
    assert fetched == [1, 2, 3, 4]  # No 20/500-body scan, including already processed emails.
    assert len(searches) == 5 and searches[0] == ("UID", "101:*")
    expected_name = "Minh Trần" if source == "Agoda" else "MINH TRẦN"
    expected_room = "Bunk Bed in Mixed Dormitory Room x1" if source == "Agoda" else "Deluxe Double Room - Room Only x1"
    assert all(row["guest_name"] == expected_name and row["room_type"] == expected_room for row in state.history())
    pending = state.pending_for_date(today)
    assert len(pending) == 2 and all(item.guest_name == expected_name and item.room_type == expected_room for item in pending)
    assert len(state.history()) == 2 and not any(kind in {"alert", "deferred_alert"} for kind, _ in events.queue)
    assert any(kind == "history_changed" for kind, _ in events.queue)
    assert state.mailbox_read_position(mailbox_key) == (100, [])
    assert state.incomplete_confirmations(today) == []
    fetched.clear()
    searches.clear()
    monitor.scan_mailbox()
    assert fetched == [] and searches == [("UID", "101:*")]


def test_recovery_retries_are_throttled_and_manual_scan_retries(tmp_path, monkeypatch):
    monitor, state, _ = make_monitor(tmp_path)
    incomplete = BookingEvent(source="Agoda", booking_id="987654321", checkin_date=date.today(), guest_name="Minh")
    state.register_today_confirmation(incomplete, (), date.today())
    key = f"incremental:v1:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(key, [], minimum_cursor=100, parser_version=mail_monitor.PARSER_STATE_VERSION)
    searches = []
    fake_inbox(monkeypatch, {}, searches=searches)
    monitor.scan_mailbox()
    assert searches[-1] == ("HEADER", "Subject", '"987654321"')
    searches.clear()
    monitor.scan_mailbox()
    assert searches == [("UID", "101:*")]
    monitor.check_now()
    searches.clear()
    monitor.scan_mailbox()
    assert searches[-1] == ("HEADER", "Subject", '"987654321"')


def test_targeted_recovery_reads_at_most_three_matches_and_ignores_other_days(tmp_path, monkeypatch):
    monitor, state, events = make_monitor(tmp_path)
    today = date.today()
    event = BookingEvent(source="Agoda", booking_id="987654321", checkin_date=today, guest_name="Minh")
    state.acknowledge(state.register_today_confirmation(event, (), today))
    key = f"incremental:v1:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(key, [], minimum_cursor=100, parser_version=mail_monitor.PARSER_STATE_VERSION)
    messages = {uid: recent_message(arrival=(today + timedelta(days=1)).isoformat(), subject="Agoda Booking ID 987654321 - CONFIRMED") for uid in range(1, 11)}
    fetched = fake_inbox(monkeypatch, messages)
    monitor.scan_mailbox()
    assert fetched == [10, 9, 8]
    assert state.history()[0]["guest_name"] == "Minh" and not state.history()[0]["room_type"]
    assert not any(kind == "alert" for kind, _ in events.queue)


@pytest.mark.parametrize("invalid", ["sender", "id", "date", "modified", "cancelled", "provider"])
def test_targeted_recovery_rejects_wrong_or_untrusted_message(tmp_path, monkeypatch, invalid):
    monitor, state, events = make_monitor(tmp_path)
    today = date.today()
    incomplete = BookingEvent(source="Agoda", booking_id="987654321", checkin_date=today, guest_name="Minh")
    state.acknowledge(state.register_today_confirmation(incomplete, (), today))
    key = f"incremental:v1:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(key, [], minimum_cursor=100, parser_version=mail_monitor.PARSER_STATE_VERSION)
    message = EmailMessage()
    message["From"] = "spoof@example.com" if invalid == "sender" else "booking@agoda.com"
    if invalid == "provider":
        message.replace_header("From", "booknotif@expedia.com")
    subject = {"modified": "Booking modified", "cancelled": "Booking cancelled"}.get(invalid, "Booking confirmation")
    message["Subject"] = f"{subject} 987654321"
    booking_id = "987654322" if invalid == "id" else "987654321"
    arrival = today + timedelta(days=1) if invalid == "date" else today
    message.set_content(f"Booking ID: {booking_id}\nCheck-in: {arrival.isoformat()}\nGuest Name: Minh Tran\nRoom Type: Deluxe\nRooms: 1")
    fetched = fake_inbox(monkeypatch, {1: message.as_bytes()})
    monitor.scan_mailbox()
    assert fetched == [1]
    assert state.history()[0]["guest_name"] == "Minh" and not state.history()[0]["room_type"]
    assert not any(kind in {"alert", "history_changed"} for kind, _ in events.queue)


@pytest.mark.parametrize(("offset", "subject", "expected"), [
    (0, "Expedia - New Booking", 1),
    (1, "Expedia - New Booking", 0),
    (-1, "Expedia - New Booking", 0),
    (0, "Expedia - Reservation modified", 0),
    (0, "Expedia - Reservation cancelled", 0),
])
def test_real_expedia_structure_only_alerts_new_arrivals_today(tmp_path, monkeypatch, offset, subject, expected):
    arrival = date.today() + timedelta(days=offset)
    html = (Path(__file__).parent / "fixtures" / "expedia_new_booking.html").read_text(encoding="utf-8")
    html = html.replace("Oct 13, 2026", arrival.isoformat())
    html = html.replace("Oct 14, 2026", (arrival + timedelta(days=1)).isoformat())
    message = EmailMessage()
    message["From"] = "Expedia Group <booknotif@expedia.com>"
    message["Subject"] = subject
    message["Message-ID"] = "<real-expedia-structure@example>"
    message.set_content(html, subtype="html")
    fake_inbox(monkeypatch, {1: message.as_bytes()})
    monitor, state, events = make_monitor(tmp_path)
    assert monitor.scan_mailbox() == 1  # Parsed mail count, not notification count.
    alerts = [payload for kind, payload in events.queue if kind == "alert"]
    assert len(alerts) == expected
    if expected:
        assert alerts[0].source == "Expedia"
        assert alerts[0].guest_name == "MINH TRẦN"
        assert alerts[0].room_type == "Deluxe Double Room - Room Only x1"
        assert len(state.pending_for_date(arrival)) == 1
    else:
        assert state.pending_for_date(arrival) == []
    assert monitor.scan_mailbox() == 0  # No duplicate notification.


@pytest.mark.parametrize("source", ["Agoda", "Expedia"])
def test_alert_is_emitted_before_next_email_and_survives_connection_drop(tmp_path, monkeypatch, source):
    monitor, state, events = make_monitor(tmp_path)

    def before_fetch(uid):
        if uid == 1:
            assert any(kind == "alert" for kind, _ in events.queue), "Alert waited for the whole inbox"
            assert len(StateStore(state.path).pending_for_date(date.today())) == 1

    fetched = fake_inbox(monkeypatch, {
        1: mail_monitor.imaplib.IMAP4.abort("connection dropped"), 2: recent_message(source),
    }, before_fetch)
    with pytest.raises(mail_monitor.imaplib.IMAP4.abort):
        monitor.scan_mailbox()
    assert fetched == [2, 1]
    assert [item.source for kind, item in events.queue if kind == "alert"] == [source]


def test_only_latest_20_are_downloaded_and_old_mail_does_not_block_new(tmp_path, monkeypatch):
    monitor, _, _ = make_monitor(tmp_path)
    non_booking = b"From: friend@example.com\r\nSubject: Hello\r\n\r\nHello"
    searches = []
    fetched = fake_inbox(monkeypatch, {uid: non_booking for uid in range(1, 2001)}, searches=searches)
    monitor.scan_mailbox()
    assert fetched == list(range(2000, 1980, -1))
    fetched.clear()
    monitor.scan_mailbox()
    assert fetched == []
    assert searches == [("1981:*",), ("UID", "2001:*")]


@pytest.mark.parametrize("source", ["Agoda", "Expedia"])
def test_restart_catches_up_all_new_mail_even_more_than_20(tmp_path, monkeypatch, source):
    monitor, _, _ = make_monitor(tmp_path)
    messages = {90: recent_message(source, booking_id="1000090")}
    searches = []
    fetched = fake_inbox(monkeypatch, messages, searches=searches)
    monitor.scan_mailbox()
    fetched.clear()
    # A new process after downtime must not bootstrap again or cap the backlog.
    monitor, state, events = make_monitor(tmp_path)
    messages.update({uid: recent_message(source, booking_id=str(1000000 + uid)) for uid in range(91, 126)})
    assert monitor.scan_mailbox() == 35
    assert fetched == list(range(125, 90, -1))
    assert searches[-1] == ("UID", "91:*")
    assert len([kind for kind, _ in events.queue if kind == "alert"]) == 35
    assert len(state.pending_for_date(date.today())) == 36
    fetched.clear()
    monitor.scan_mailbox()
    assert fetched == []


def test_interrupted_batch_recovers_lower_uids_and_new_arrivals(tmp_path, monkeypatch):
    monitor, _, _ = make_monitor(tmp_path)
    messages = {
        1: recent_message(booking_id="1000001"),
        2: mail_monitor.imaplib.IMAP4.abort("lost connection"),
        3: recent_message(booking_id="1000003"),
    }
    fetched = fake_inbox(monkeypatch, messages)
    with pytest.raises(mail_monitor.imaplib.IMAP4.abort):
        monitor.scan_mailbox()
    assert fetched == [3, 2]
    messages[2] = recent_message(booking_id="1000002")
    messages[4] = recent_message(booking_id="1000004")
    monitor, state, events = make_monitor(tmp_path)
    fetched.clear()
    assert monitor.scan_mailbox() == 3
    assert fetched == [4, 2, 1]
    assert len(state.pending_for_date(date.today())) == 4
    assert [event.booking_id for kind, event in events.queue if kind == "alert"] == ["1000004", "1000002", "1000001"]


def test_failed_fetch_retries_after_cursor_has_advanced(tmp_path, monkeypatch):
    monitor, _, _ = make_monitor(tmp_path)
    messages = {1: RuntimeError("temporary failure"), 2: recent_message(booking_id="1000002")}
    fetched = fake_inbox(monkeypatch, messages)
    assert monitor.scan_mailbox() == 1
    messages[1] = recent_message(booking_id="1000001")
    monitor, _, events = make_monitor(tmp_path)
    fetched.clear()
    assert monitor.scan_mailbox() == 1
    assert fetched == [1]
    assert [event.booking_id for kind, event in events.queue if kind == "alert"] == ["1000001"]


def test_failed_parse_retries_without_refetching_completed_mail(tmp_path, monkeypatch):
    monitor, _, _ = make_monitor(tmp_path)
    messages = {1: recent_message(booking_id="1000001"), 2: recent_message(booking_id="1000002")}
    fetched = fake_inbox(monkeypatch, messages)
    parse = mail_monitor.parse_booking_message

    def parse_with_failure(message):
        return None if "1000001" in message["Message-ID"] else parse(message)

    monkeypatch.setattr(mail_monitor, "parse_booking_message", parse_with_failure)
    assert monitor.scan_mailbox() == 1
    monitor, _, events = make_monitor(tmp_path)
    monkeypatch.setattr(mail_monitor, "parse_booking_message", parse)
    fetched.clear()
    assert monitor.scan_mailbox() == 1
    assert fetched == [1]
    assert [event.booking_id for kind, event in events.queue if kind == "alert"] == ["1000001"]


def test_stopped_scan_durably_keeps_unread_batch(tmp_path, monkeypatch):
    monitor, _, _ = make_monitor(tmp_path)
    messages = {uid: recent_message(booking_id=str(1000000 + uid)) for uid in range(1, 4)}

    def stop_after_first(uid):
        monitor.stop()

    fetched = fake_inbox(monkeypatch, messages, before_fetch=stop_after_first)
    assert monitor.scan_mailbox() == 1
    assert fetched == [3]
    monitor, _, _ = make_monitor(tmp_path)
    # Replace the stopped-process callback for the resumed process.
    fetched = fake_inbox(monkeypatch, messages)
    assert monitor.scan_mailbox() == 2
    assert fetched == [2, 1]


def test_bootstrap_uses_sequence_positions_not_uid_numbers(tmp_path, monkeypatch):
    monitor, _, _ = make_monitor(tmp_path)
    messages = {uid: recent_message(booking_id=str(1000000 + uid)) for uid in range(10, 301, 10)}
    fetched = fake_inbox(monkeypatch, messages)
    monitor.scan_mailbox()
    assert fetched == list(range(300, 100, -10))


def test_parser_upgrade_repairs_recent_saved_booking_without_duplicate_alert(tmp_path, monkeypatch):
    monitor, state, events = make_monitor(tmp_path)
    today = date.today()
    incomplete = BookingEvent(source="Agoda", booking_id="987654321", checkin_date=today, guest_name="NGUYEN")
    alert = state.register_today_confirmation(incomplete, ("old-parser",), today)
    state.acknowledge(alert)
    old_key = f"incremental:p7:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(old_key, [100])
    state.finish_mailbox_read(old_key, 100)
    message = EmailMessage()
    message["From"] = "booking@agoda.com"
    message["Subject"] = "Booking confirmation"
    message.set_content(f"Booking ID: 987654321\nCheck-in: {today.isoformat()}\nGuest Name: NGUYEN\nVAN AN\nRoom Type: Superior Double Room")
    searches = []
    fetched = fake_inbox(monkeypatch, {100: message.as_bytes()}, searches=searches)
    monitor.scan_mailbox()
    assert fetched == [100]
    assert state.history()[0]["guest_name"] == "NGUYEN VAN AN"
    assert state.history()[0]["room_type"] == "Superior Double Room"
    assert not any(kind == "alert" for kind, _ in events.queue)
    fetched.clear()
    monitor, _, _ = make_monitor(tmp_path)
    monitor.scan_mailbox()
    assert fetched == []
    assert searches == [("UID", "101:*"), ("1:*",), ("UID", "101:*")]


def test_parser_upgrade_keeps_old_offline_cursor_and_pending_batch(tmp_path, monkeypatch):
    monitor, state, events = make_monitor(tmp_path)
    old_key = f"incremental:p7:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(old_key, [89, 90])
    state.finish_mailbox_read(old_key, 90)
    messages = {uid: recent_message(booking_id=str(1000000 + uid)) for uid in [89, 90, *range(91, 126)]}
    fetched = fake_inbox(monkeypatch, messages)
    assert monitor.scan_mailbox() == 36
    assert fetched == [*range(125, 90, -1), 89]
    assert len([kind for kind, _ in events.queue if kind == "alert"]) == 36
    cursor, pending = state.mailbox_read_position(f"incremental:v1:{monitor.identity_hash}:123")
    assert cursor == 125 and pending == []


def test_uidvalidity_change_bootstraps_new_mailbox_not_old_cursor(tmp_path, monkeypatch):
    monitor, _, events = make_monitor(tmp_path)
    messages = {100: recent_message(booking_id="1000100")}
    validity = [b"123"]
    searches = []
    fetched = fake_inbox(monkeypatch, messages, uid_validity=validity, searches=searches)
    monitor.scan_mailbox()
    validity[0] = b"456"
    messages.clear()
    messages[1] = recent_message("Expedia", booking_id="1000001")
    fetched.clear()
    assert monitor.scan_mailbox() == 1
    assert fetched == [1]
    assert searches[-1] == ("1:*",)
    assert len([kind for kind, _ in events.queue if kind == "alert"]) == 2


def test_empty_inbox_then_new_mail(tmp_path, monkeypatch):
    monitor, _, events = make_monitor(tmp_path)
    messages = {}
    fetched = fake_inbox(monkeypatch, messages)
    assert monitor.scan_mailbox() == 0
    messages[10] = recent_message()
    assert monitor.scan_mailbox() == 1
    assert fetched == [10]
    assert len([kind for kind, _ in events.queue if kind == "alert"]) == 1


def test_missing_uidvalidity_cannot_silently_skip_mail(tmp_path, monkeypatch):
    monitor, _, _ = make_monitor(tmp_path)
    fetched = fake_inbox(monkeypatch, {1: recent_message()}, uid_validity=[None])
    with pytest.raises(RuntimeError, match="UIDVALIDITY"):
        monitor.scan_mailbox()
    assert fetched == []


def test_bilingual_parser_upgrade_repairs_yesterdays_history_without_popup(tmp_path, monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 1)

    monkeypatch.setattr(mail_monitor, "date", FixedDate)
    monitor, state, events = make_monitor(tmp_path)
    past = date(2026, 9, 30)
    original = BookingEvent(source="Agoda", booking_id="987654321", checkin_date=past, guest_name="Minh")
    state.acknowledge(state.register_today_confirmation(original, ("old",), past))
    mailbox_key = f"incremental:v1:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(mailbox_key, [100], parser_version="p8")
    state.finish_mailbox_read(mailbox_key, 100)
    message = EmailMessage()
    message["From"] = "booking@agoda.com"
    message["Subject"] = "Booking confirmation"
    html = (Path(__file__).parent / "fixtures" / "agoda_bilingual_confirmation.html").read_text(encoding="utf-8")
    message.set_content(html, subtype="html")
    fetched = fake_inbox(monkeypatch, {100: message.as_bytes()})
    assert monitor.scan_mailbox() == 1
    assert fetched == [100]
    assert state.history()[0]["guest_name"] == "Minh Trần"
    assert state.history()[0]["room_type"] == "Bunk Bed in Mixed Dormitory Room x1"
    assert not any(kind in {"alert", "deferred_alert", "booking_cancelled", "booking_modified"} for kind, _ in events.queue)
    assert not state.data["pending_alerts"]
    assert state.mailbox_parser_version(mailbox_key) == mail_monitor.PARSER_STATE_VERSION
    fetched.clear()
    monitor.scan_mailbox()
    assert fetched == []


def test_future_and_modified_cancelled_messages_never_create_reminders(tmp_path, monkeypatch):
    monitor, state, events = make_monitor(tmp_path)
    future = date.today() + timedelta(days=1)
    # Leftover bookings saved by 1.6/1.7 must not become reminders in the new-reader mode.
    state.apply_event(BookingEvent(source="Agoda", booking_id="OLD-12345", checkin_date=date.today()), ("old",))
    fake_inbox(monkeypatch, {
        1: recent_message("Agoda", future.isoformat(), "1000001"),
        2: recent_message("Expedia", future.isoformat(), "1000002"),
        3: recent_message("Agoda", booking_id="1000003", subject="Booking cancelled"),
        4: recent_message("Expedia", booking_id="1000004", subject="Reservation modified"),
    })
    monitor.scan_mailbox()
    assert not any(kind in {"alert", "deferred_alert", "booking_cancelled", "booking_modified"}
                   for kind, _ in events.queue)
    assert state.pending_for_date(date.today()) == []
    assert state.pending_for_date(future) == []
    assert set(state.data["bookings"]) == {"agoda:OLD-12345"}


def test_quiet_hours_persist_then_release_pending_without_duplicate(tmp_path, monkeypatch):
    monitor, state, events = make_monitor(tmp_path, quiet_hours_enabled=True)
    fake_inbox(monkeypatch, {1: recent_message()})
    monkeypatch.setattr(mail_monitor, "is_quiet_hours", lambda: True)
    monitor.scan_mailbox()
    assert [kind for kind, _ in events.queue if kind == "alert"] == []
    assert len([kind for kind, _ in events.queue if kind == "deferred_alert"]) == 1
    assert len(state.pending_for_date(date.today())) == 1
    monitor.scan_mailbox()
    assert len(state.pending_for_date(date.today())) == 1

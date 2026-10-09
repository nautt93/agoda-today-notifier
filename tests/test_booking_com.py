from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from email.message import EmailMessage

import pytest

from booking_notifier.config import DEFAULT_CONFIG, ConfigStore, atomic_json_write
from booking_notifier.parsing import parse_booking_message
from booking_notifier.state import StateStore

DETAIL_PATH = "/hotel/hoteladmin/extranet_ng/manage/booking.html"
IDENTIFIER = "5550000001"


def booking_message(day=None, subject=None, sender='"Booking.com" <notify@booking.com>', url=None, copy=1):
    day = day or date.today()
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject or f"Booking.com - Đặt phòng mới! ({IDENTIFIER}, {day.day} tháng {day.month}, {day.year})"
    message["Message-ID"] = f"<synthetic-booking-{copy}@example.invalid>"
    link = url or f"https://admin.booking.com{DETAIL_PATH}?res_id={IDENTIFIER}&hotel_id=12345&_e=synthetic-token&ses=synthetic-session"
    message.set_content("Bạn vừa nhận được đặt phòng mới. Khách sẽ thanh toán khi đến.")
    message.add_alternative(f'<p>Đặt phòng mới</p><a href="{link.replace("&", "&amp;")}">Xem đặt phòng</a>', subtype="html")
    return message


def assert_code_only(event):
    assert event.guest_name == event.room_type == event.total_revenue == event.details_url == event.details_loaded_at == ""
    assert event.checkout_date is None


def test_short_email_alert_contains_only_code_and_arrival_and_never_keeps_web_tokens(tmp_path):
    event = parse_booking_message(booking_message())
    assert event.source == "Booking.com" and event.booking_id == IDENTIFIER and event.checkin_date == date.today()
    assert_code_only(event)
    state = StateStore(tmp_path / "state.json")
    assert state.register_today_confirmation(event, ("mail-1",), date.today())
    assert len(state.pending_for_date(date.today())) == 1
    assert state.incomplete_confirmations(date.today()) == []
    saved = state.path.read_text(encoding="utf-8")
    assert "synthetic-token" not in saved and "synthetic-session" not in saved and "admin.booking.com" not in saved


def test_even_rich_email_does_not_load_guest_room_payment_or_checkout():
    message = booking_message()
    message.get_payload(0).set_content(f"Reservation number: {IDENTIFIER}\nCheck-in: {date.today().isoformat()}\n"
                                     f"Check-out: {(date.today() + timedelta(days=3)).isoformat()}\nGuest name: FULL GUEST NAME\n"
                                     "Room type: Deluxe Suite\nRooms: 3\nTotal amount: VND 1,200,000.00\nCard number: 0000 0000 0000 0000")
    event = parse_booking_message(message)
    assert event.booking_id == IDENTIFIER and event.checkin_date == date.today()
    assert_code_only(event)


@pytest.mark.parametrize("sender", ['"Booking.com" <x@booking.com.evil.invalid>', '"notify@booking.com" <x@evil.invalid>',
                                   '"Booking.com" <x@booking.com>, <x@evil.invalid>', 'team: x@booking.com;'])
def test_fake_sender_ignored(sender):
    assert parse_booking_message(booking_message(sender=sender)) is None


@pytest.mark.parametrize("subject", ["Booking.com Payment reminder", "Booking.com Newsletter", "Review requested"])
def test_non_booking_mail_ignored(subject):
    assert parse_booking_message(booking_message(subject=subject)) is None


@pytest.mark.parametrize(("subject", "status"), [
    (f"Booking.com - Đặt phòng đã hủy ({IDENTIFIER})", "cancelled"),
    (f"Booking.com - Thay đổi đặt phòng ({IDENTIFIER})", "modified"),
])
def test_lifecycle_classified_for_monitor_to_ignore(subject, status):
    event = parse_booking_message(booking_message(subject=subject))
    assert event.status == status
    assert_code_only(event)


@pytest.mark.parametrize("bad", [
    f"http://admin.booking.com{DETAIL_PATH}?res_id=5550000002&hotel_id=12345",
    f"https://admin.booking.com.evil.invalid{DETAIL_PATH}?res_id=5550000002&hotel_id=12345",
    f"https://user:pass@admin.booking.com{DETAIL_PATH}?res_id=5550000002&hotel_id=12345",
    f"https://admin.booking.com:444{DETAIL_PATH}?res_id=5550000002&hotel_id=12345",
    f"https://admin.booking.com{DETAIL_PATH}?res_id=5550000002&res_id=5550000003&hotel_id=12345",
])
def test_unsafe_or_ambiguous_link_does_not_override_authenticated_subject(bad):
    event = parse_booking_message(booking_message(url=bad))
    assert event.booking_id == IDENTIFIER
    assert_code_only(event)


def test_email_subject_and_valid_link_id_mismatch_rejected():
    assert parse_booking_message(booking_message(
        url=f"https://admin.booking.com{DETAIL_PATH}?res_id=5550000002&hotel_id=12345",
    )) is None


@pytest.mark.parametrize("identifier_location", ["body", "html_link", "text_link"])
def test_code_and_checkin_recovered_from_email_without_browser(identifier_location):
    today = date.today()
    message = EmailMessage()
    message["From"] = "notify@booking.com"
    message["Subject"] = "Booking.com - New booking"
    body = f"Check-in: {today.isoformat()}\n"
    if identifier_location == "body":
        message.set_content(body + f"Reservation number: {IDENTIFIER}")
    elif identifier_location == "text_link":
        message.set_content(body + f"https://admin.booking.com{DETAIL_PATH}?res_id={IDENTIFIER}&hotel_id=12345&_e=token")
    else:
        message.set_content(body)
        message.add_alternative(f'<p>{body}</p><a href="https://admin.booking.com{DETAIL_PATH}?res_id={IDENTIFIER}&amp;hotel_id=12345">Open</a>', subtype="html")
    event = parse_booking_message(message)
    assert event.booking_id == IDENTIFIER and event.checkin_date == today
    assert_code_only(event)


def test_code_only_booking_deduplicates_pending_and_closed_notifications_across_restart(tmp_path):
    today = date.today()
    event = parse_booking_message(booking_message())
    state = StateStore(tmp_path / "state.json")
    alert = state.register_today_confirmation(event, ("first-uid",), today)
    assert state.register_today_confirmation(event, ("resent-pending",), today) is None
    reloaded = StateStore(state.path)
    assert len(reloaded.pending_for_date(today)) == 1
    reloaded.acknowledge(alert)
    history = reloaded.history()
    reloaded = StateStore(state.path)
    assert reloaded.register_today_confirmation(event, ("resent-closed",), today) is None
    reloaded.acknowledge(alert)
    assert reloaded.history() == history and not reloaded.pending_for_date(today)
    assert reloaded.is_processed("first-uid", "resent-pending", "resent-closed")


def test_upgrade_preserves_legacy_history_cursor_and_suppresses_realert(tmp_path):
    today = date.today()
    event = parse_booking_message(booking_message())
    old = replace(event, guest_name="Saved Legacy Guest", room_type="Legacy Room x2", total_revenue="VND 500.000",
                  checkout_date=today + timedelta(days=2), details_url="https://admin.booking.com/legacy", details_loaded_at="legacy-timestamp")
    record = old.to_dict()
    path = tmp_path / "state.json"
    atomic_json_write(path, {"schema": 5, "bookings": {event.storage_id: {**record, "status": "active", "alerted_for": today.isoformat()}},
                             "history": [{**record, "acknowledged_at": "legacy-close"}], "pending_alerts": [],
                             "processed_keys": ["legacy-uid"], "mailbox_reads": {"legacy-inbox": {"cursor": 10, "pending_uids": [8]}}})
    state = StateStore(path)
    history = state.history()
    assert state.register_today_confirmation(event, ("new-alias",), today) is None
    state = StateStore(path)
    assert state.history() == history
    assert state.data["bookings"][event.storage_id]["guest_name"] == "Saved Legacy Guest"
    assert state.mailbox_read_position("legacy-inbox") == (10, [8])
    assert state.is_processed("legacy-uid", "new-alias") and not state.pending_for_date(today)


def test_code_only_still_rejects_future_confirmation_at_state_boundary(tmp_path):
    state = StateStore(tmp_path / "state.json")
    event = parse_booking_message(booking_message(date.today() + timedelta(days=1)))
    with pytest.raises(ValueError, match="hôm nay"):
        state.register_today_confirmation(event, ("future",), date.today())
    assert state.active_bookings() == []


def test_legacy_booking_details_do_not_schedule_repair_or_accept_new_enrichment(tmp_path):
    state = StateStore(tmp_path / "state.json")
    event = parse_booking_message(booking_message())
    state.acknowledge(state.register_today_confirmation(event, ("original",), date.today()))
    previous = state.path.read_bytes()
    formerly_rich = replace(event, guest_name="Imported Guest", room_type="Imported Room x2")
    assert not state.repair_known_confirmation(formerly_rich)
    assert not state.incomplete_confirmations(date.today())
    assert state.path.read_bytes() == previous


def test_configuration_upgrade_removes_advanced_booking_options_preserves_credentials_sound_and_quiet_times(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    profile = {"email_address": "hotel@example.invalid", "password_encrypted": "synthetic-preserved-secret",
               "booking_com_browser": "chrome", "booking_com_enrichment": True, "booking_com_enrichment_mode": "visible",
               "booking_com_sound_file": "custom-booking.mp3", "agoda_sound_file": "custom-agoda.wav", "poll_seconds": 90,
               "quiet_hours_enabled": True, "quiet_start_time": "22:15", "quiet_end_time": "06:45"}
    atomic_json_write(store.path, profile)
    original = store.path.read_bytes()
    loaded = store.load()
    assert store.path.read_bytes() == original
    advanced = {"booking_com_browser", "booking_com_enrichment", "booking_com_enrichment_mode"}
    assert not advanced.intersection(DEFAULT_CONFIG) and not advanced.intersection(loaded)
    assert all(loaded[key] == value for key, value in profile.items() if key not in advanced)
    store.save({**loaded, **{key: profile[key] for key in advanced}})
    persisted = json.loads(store.path.read_text(encoding="utf-8"))
    assert persisted == loaded and not advanced.intersection(persisted)


@pytest.mark.parametrize("bad", [None, "", "invalid", False, {}, [], 3.14])
def test_malformed_legacy_imap_numbers_cannot_block_startup_or_erase_credentials(tmp_path, bad):
    store = ConfigStore(tmp_path / "config.json")
    profile = {"email_address": "hotel@example.invalid", "password_encrypted": "preserved-application-secret",
               "imap_host": "imap.example.invalid", "imap_port": bad, "poll_seconds": bad, "scan_days": bad,
               "booking_com_sound_file": "custom-booking.wav"}
    atomic_json_write(store.path, profile)
    previous = store.path.read_bytes()
    loaded = store.load()
    assert (loaded["imap_port"], loaded["poll_seconds"], loaded["scan_days"]) == (993, 60, 90)
    assert all(loaded[key] == profile[key] for key in ("imap_host", "email_address", "password_encrypted", "booking_com_sound_file"))
    assert store.path.read_bytes() == previous


@pytest.mark.parametrize(("port", "poll", "days", "expected"), [
    ("1993", "90", "30", (1993, 90, 30)),
    ("0", "0", "0", (993, 30, 1)),
    (70000, 9000, 9999, (993, 3600, 365)),
])
def test_valid_legacy_numeric_strings_and_bounds(port, poll, days, expected, tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    atomic_json_write(store.path, {"imap_port": port, "poll_seconds": poll, "scan_days": days})
    loaded = store.load()
    assert (loaded["imap_port"], loaded["poll_seconds"], loaded["scan_days"]) == expected


def test_monitor_only_latest_twenty_today_confirmation_and_silent_resend(tmp_path, monkeypatch):
    from test_trip_monitor import alerts, fake_inbox, monitor_for

    tomorrow = date.today() + timedelta(days=1)
    messages = {uid: booking_message(tomorrow, copy=uid).as_bytes() for uid in range(1, 38)}
    messages[35] = booking_message(subject=f"Booking.com - Thay đổi đặt phòng ({IDENTIFIER})", copy=35).as_bytes()
    messages[36] = booking_message(subject=f"Booking.com - Đặt phòng đã hủy ({IDENTIFIER})", copy=36).as_bytes()
    messages[37] = booking_message(copy=37).as_bytes()
    fetched, searches = fake_inbox(monkeypatch, messages)
    monitor, state, events = monitor_for(tmp_path)
    assert monitor.scan_mailbox() == 20
    assert fetched == list(range(37, 17, -1)) and searches == [("18:*",)]
    assert [event.booking_id for event in alerts(events)] == [IDENTIFIER]
    assert_code_only(alerts(events)[0])
    state.acknowledge(alerts(events)[0])
    messages[38] = booking_message(copy=38).as_bytes()
    monitor, state, _ = monitor_for(tmp_path, StateStore(state.path), events)
    assert monitor.scan_mailbox() == 1
    assert len(alerts(events)) == len(state.history()) == 1 and not state.pending_for_date(date.today())
    assert not state.incomplete_confirmations(date.today())

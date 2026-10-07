from __future__ import annotations

import queue
import time
from dataclasses import replace
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import BookingNotifierApp
from booking_notifier.audio import SOURCE_SOUND_KEYS, configured_sound_paths, default_source_sound
from booking_notifier.booking_com import (
    DETAIL_PATH,
    BookingComDetailError,
    canonical_details_url,
    parse_booking_com_details,
)
from booking_notifier.booking_com_browser import BookingComBrowserError, BookingComWorker
from booking_notifier.excel_export import excel_tsv
from booking_notifier.parsing import parse_booking_message
from booking_notifier.state import StateStore


def booking_message(day=None, subject=None, sender='"Booking.com" <notify@booking.com>', url=None):
    day = day or date.today()
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject or f"Booking.com - Đặt phòng mới! (5550000001, {day.day} tháng {day.month}, {day.year})"
    message["Message-ID"] = "<synthetic-booking@example.invalid>"
    safe = url or (f"https://admin.booking.com{DETAIL_PATH}?res_id=5550000001&hotel_id=12345&_e=synthetic-email-token&_s=synthetic-secret&ses=synthetic-session")
    message.set_content("Bạn vừa nhận được đặt phòng mới. Khách sẽ thanh toán khi đến.")
    message.add_alternative(f'<p>Đặt phòng mới</p><a href="{safe.replace("&", "&amp;")}">Xem đặt phòng</a>', subtype="html")
    return message


def details(event=None):
    event = event or parse_booking_message(booking_message())
    return {
        "url": event.details_url,
        "names": ["NGUYỄN SYNTHETIC FULL GUEST"],
        "rooms": ["Deluxe Double Room", "Deluxe Double Room", "Triple City View"],
        "fields": [["Mã số đặt phòng:", event.booking_id], ["Nhận phòng", event.checkin_date.isoformat()],
                   ["Trả phòng", (event.checkin_date + timedelta(days=2)).isoformat()], ["Tổng số căn", "3"],
                   ["Tổng tiền phòng", "VND 1.200.000"], ["Hoa hồng ước tính", "VND 240.000"]],
    }


def test_short_email_basic_alert_identity_arrival_and_no_secrets(tmp_path):
    event = parse_booking_message(booking_message())
    assert event.source == "Booking.com" and event.booking_id == "5550000001" and event.checkin_date == date.today()
    assert event.guest_name == event.room_type == event.total_revenue == "" and event.checkout_date is None
    assert "_e=" not in event.details_url and "_s=" not in event.details_url and "ses=" not in event.details_url
    state = StateStore(tmp_path / "state.json")
    assert state.register_today_confirmation(event, ("mail-1",), date.today())
    assert len(state.pending_for_date(date.today())) == 1 and len(state.booking_com_candidates(date.today())) == 1
    assert state.incomplete_confirmations(date.today()) == []  # No futile full-mailbox name lookup.
    assert "synthetic-secret" not in state.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("sender", ['"Booking.com" <x@booking.com.evil.invalid>', '"notify@booking.com" <x@evil.invalid>',
                                   '"Booking.com" <x@booking.com>, <x@evil.invalid>', 'team: x@booking.com;'])
def test_fake_sender_ignored(sender):
    assert parse_booking_message(booking_message(sender=sender)) is None


@pytest.mark.parametrize("subject", ["Booking.com Payment reminder", "Booking.com Newsletter", "Review requested"])
def test_non_booking_mail_ignored(subject):
    assert parse_booking_message(booking_message(subject=subject)) is None


@pytest.mark.parametrize(("subject", "status"), [
    ("Booking.com - Đặt phòng đã hủy (5550000001)", "cancelled"),
    ("Booking.com - Thay đổi đặt phòng (5550000001)", "modified"),
])
def test_lifecycle_classified_for_monitor_to_ignore(subject, status):
    assert parse_booking_message(booking_message(subject=subject)).status == status


@pytest.mark.parametrize("bad", [
    f"http://admin.booking.com{DETAIL_PATH}?res_id=5550000001&hotel_id=12345",
    f"https://admin.booking.com.evil.invalid{DETAIL_PATH}?res_id=5550000001&hotel_id=12345",
    f"https://user:pass@admin.booking.com{DETAIL_PATH}?res_id=5550000001&hotel_id=12345",
    f"https://admin.booking.com:444{DETAIL_PATH}?res_id=5550000001&hotel_id=12345",
    f"https://admin.booking.com{DETAIL_PATH}?res_id=5550000001&res_id=5550000002&hotel_id=12345",
    f"https://admin.booking.com{DETAIL_PATH}?res_id=5550000001&hotel_id=12345&hotel_id=12346",
])
def test_unsafe_or_ambiguous_url_not_retained(bad):
    assert canonical_details_url(bad) == ""
    event = parse_booking_message(booking_message(url=bad))
    assert event and event.details_url == ""  # Still basic from authenticated sender/subject.


def test_email_subject_and_link_id_mismatch_rejected():
    assert parse_booking_message(booking_message(url=f"https://admin.booking.com{DETAIL_PATH}?res_id=5550000002&hotel_id=12345")) is None


def test_complete_details_multiroom_fullname_gross_room_price_and_exact_excel():
    event = parse_booking_message(booking_message())
    full = parse_booking_com_details(details(event), event)
    assert full.guest_name == "NGUYỄN SYNTHETIC FULL GUEST"
    assert full.room_type == "Deluxe Double Room x2; Triple City View x1"
    assert full.nights == 2 and full.details_loaded_at
    row = excel_tsv(full).split("\t")
    assert len(row) == 9 and row[0] == row[6] == row[7] == ""
    assert row[1] == full.guest_name and row[5] == "1200000"
    assert row[8] == "Booking.com Deluxe Double Room x2; Triple City View x1"


@pytest.mark.parametrize("change", ["id", "hotel", "arrival", "checkout", "name", "amount", "allocation", "single_header", "contradiction"])
def test_unverified_details_never_overwrite_basic_event(change):
    event = parse_booking_message(booking_message())
    snap = details(event)
    if change == "id":
        snap["fields"][0][1] = "5550000002"
    elif change == "hotel":
        snap["url"] = snap["url"].replace("12345", "12346")
    elif change == "arrival":
        snap["fields"][1][1] = (date.today() + timedelta(days=1)).isoformat()
    elif change == "checkout":
        snap["fields"][2][1] = date.today().isoformat()
    elif change == "name":
        snap["names"].append("OTHER GUEST")
    elif change == "amount":
        snap["fields"][4][1] = ""
    elif change == "allocation":
        snap["rooms"].pop()
    elif change == "single_header":
        snap["rooms"] = snap["rooms"][:1]
    else:
        snap["fields"].append(["Tổng số căn", "5"])
    with pytest.raises(BookingComDetailError):
        parse_booking_com_details(snap, event)
    assert event.guest_name == event.room_type == "" and not event.details_loaded_at


@pytest.mark.parametrize("close_first", [False, True])
def test_enrichment_updates_pending_or_closed_history_without_new_alert_and_survives_restart(tmp_path, close_first):
    state = StateStore(tmp_path / "state.json")
    basic = parse_booking_message(booking_message())
    stale = state.register_today_confirmation(basic, ("mail1",), date.today())
    state.stage_mailbox_reads("test", [101, 102], parser_version="p14")
    cursor = state.mailbox_read_position("test")
    if close_first:
        state.acknowledge(stale)
    full = parse_booking_com_details(details(basic), basic)
    assert state.enrich_booking_com(full, date.today())
    assert not state.booking_com_candidates(date.today())
    assert state.register_today_confirmation(basic, ("resent-mail",), date.today()) is None
    if not close_first:
        assert state.pending_for_date(date.today())[0].guest_name == full.guest_name
        state.acknowledge(stale)  # UI copy may predate worker update; do not downgrade.
    reloaded = StateStore(state.path)
    assert reloaded.history()[0]["guest_name"] == full.guest_name
    assert reloaded.history()[0]["room_type"] == full.room_type
    assert reloaded.history()[0]["checkout_date"] == full.checkout_date.isoformat()
    assert not reloaded.pending_for_date(date.today()) and len(reloaded.history()) == 1
    assert reloaded.mailbox_read_position("test") == cursor
    assert reloaded.is_processed("mail1", "resent-mail")


def test_future_booking_no_candidates_or_enrichment_unknown_id_or_property(tmp_path):
    state = StateStore(tmp_path / "state.json")
    basic = parse_booking_message(booking_message())
    full = parse_booking_com_details(details(basic), basic)
    assert not state.enrich_booking_com(full, date.today())
    state.register_today_confirmation(basic, ("mail1",), date.today())
    assert not state.enrich_booking_com(replace(full, details_url=full.details_url.replace("12345", "12346")), date.today())
    assert not state.booking_com_candidates(date.today() + timedelta(days=1))


def test_refresh_updates_same_popup_and_excel_without_sound_restart(tmp_path):
    state = StateStore(tmp_path / "state.json")
    basic = parse_booking_message(booking_message())
    stale = state.register_today_confirmation(basic, ("mail1",), date.today())
    full = parse_booking_com_details(details(basic), basic)
    state.enrich_booking_com(full, date.today())
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.state, app.alert_queue, app.active_alert = state, [], stale
    for name in ("active_guest_var", "active_room_var", "active_revenue_var", "active_checkout_var", "active_nights_var", "active_booking_details_var"):
        setattr(app, name, Mock())
    app.play_sound, app.stop_sound, app.f92_worker = Mock(), Mock(), Mock()
    app.sound_active = True
    app._refresh_pending_details()
    assert app.active_alert is stale and excel_tsv(stale) == excel_tsv(full)
    assert app.sound_active
    app.play_sound.assert_not_called()
    app.stop_sound.assert_not_called()
    app.f92_worker.notify.assert_called_once_with(stale, play_sound=False)


def test_worker_lazy_disabled_retry_and_enrich_emit_history_not_alert(tmp_path):
    state, events, factory = StateStore(tmp_path / "state.json"), queue.Queue(), Mock()
    worker = BookingComWorker(events, state, {}, tmp_path, factory)
    worker.refresh()
    factory.assert_not_called()
    basic = parse_booking_message(booking_message())
    state.register_today_confirmation(basic, ("mail1",), date.today())
    worker.config["booking_com_enrichment"] = False
    worker.refresh()
    factory.assert_not_called()
    worker.config["booking_com_enrichment"] = True
    client = factory.return_value
    client.fetch.side_effect = BookingComBrowserError("Đăng nhập trong Cài đặt")
    worker.refresh()
    worker.refresh()
    assert client.fetch.call_count == 1 and len(state.pending_for_date(date.today())) == 1
    client.fetch.side_effect = None
    client.fetch.side_effect = lambda candidate: parse_booking_com_details(details(candidate), candidate)
    worker.retry_after.clear()
    worker.refresh()
    kinds = [kind for kind, _ in list(events.queue)]
    assert kinds.count("history_changed") == 1 and "alert" not in kinds
    worker.refresh()
    assert client.fetch.call_count == 2 and not state.booking_com_candidates(date.today())


def test_monitor_only_latest_twenty_and_today_new_then_duplicate_after_enrichment_is_silent(tmp_path, monkeypatch):
    from test_trip_monitor import alerts, fake_inbox, monitor_for

    messages = {uid: booking_message(date.today() + timedelta(days=1)).as_bytes() for uid in range(1, 35)}
    messages[35] = booking_message(subject="Booking.com - Thay đổi đặt phòng (5550000001)").as_bytes()
    messages[36] = booking_message(subject="Booking.com - Đặt phòng đã hủy (5550000001)").as_bytes()
    messages[37] = booking_message().as_bytes()
    # Distinct emails, not an accidental shared Message-ID masking source/ID dedup.
    for uid, raw in list(messages.items()):
        messages[uid] = raw.replace(b"synthetic-booking@example.invalid", f"synthetic-{uid}@example.invalid".encode())
    fetched, searches = fake_inbox(monkeypatch, messages)
    monitor, state, events = monitor_for(tmp_path)
    monitor.scan_mailbox()
    assert len(fetched) == 20 and len(alerts(events)) == 1
    assert len(state.pending_for_date(date.today())) == len(state.data["bookings"]) == 1
    basic = alerts(events)[0]
    full = parse_booking_com_details(details(basic), basic)
    assert state.enrich_booking_com(full, date.today())
    state.acknowledge(basic)
    messages[38] = booking_message().as_bytes().replace(b"synthetic-booking@example.invalid", b"resent-copy@example.invalid")
    monitor.scan_mailbox()
    assert fetched[-1] == 38 and len(alerts(events)) == 1 and len(state.history()) == 1
    assert state.history()[0]["guest_name"] == full.guest_name
    assert not any(criteria[0] in {"TEXT", "HEADER", "ALL"} for criteria in searches)


def test_cancelled_cache_cannot_be_enriched_by_inflight_worker(tmp_path):
    state = StateStore(tmp_path / "state.json")
    basic = parse_booking_message(booking_message())
    state.register_today_confirmation(basic, ("mail1",), date.today())
    full = parse_booking_com_details(details(basic), basic)
    state.apply_event(replace(basic, status="cancelled"), ("cancel",))
    assert not state.booking_com_candidates(date.today()) and not state.enrich_booking_com(full, date.today())


def test_booking_sound_is_distinct_and_selected_key_has_no_dot(tmp_path):
    assert SOURCE_SOUND_KEYS["booking.com"] == "booking_com_sound_file"
    assert configured_sound_paths({"booking_com_sound_file": "booking.mp3", "sound_file": "old.wav"}, "Booking.com") == [Path("booking.mp3"), Path("old.wav")]
    sounds = [default_source_sound(source, tmp_path).read_bytes() for source in ("Agoda", "Expedia", "Traveloka", "Trip", "Booking.com")]
    assert len(set(sounds)) == 5


def test_booking_print_is_unsupported_without_fetch():
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.closing = app.print_loading = app.print_spooling = False
    app._collect_config = Mock()
    app.request_booking_print(parse_booking_message(booking_message()))
    app._collect_config.assert_not_called()


def test_login_preserves_own_settings_and_does_not_require_email_password(tmp_path):
    from booking_notifier.config import DEFAULT_CONFIG, ConfigStore

    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.config = dict(DEFAULT_CONFIG)
    app.config_store = ConfigStore(tmp_path / "config.json")
    app.booking_com_enabled_var = SimpleNamespace(get=lambda: True)
    app.booking_com_browser_var = SimpleNamespace(get=lambda: "chrome")
    app.booking_com_status_var, app.booking_com_worker = Mock(), Mock()
    app.login_booking_com()
    assert app.config_store.load()["booking_com_browser"] == "chrome"
    app.booking_com_worker.login.assert_called_once_with(None)


def test_visible_login_is_not_navigated_away_while_user_enters_credentials(tmp_path, monkeypatch):
    from booking_notifier.booking_com_browser import BookingComBrowser

    client = BookingComBrowser(tmp_path)
    client.visible = True
    page = Mock()
    page.url, page.is_closed.return_value = "https://account.booking.com/sign-in", False
    client.context = SimpleNamespace(pages=[page])
    monkeypatch.setattr(client, "_launch", Mock())
    with pytest.raises(BookingComBrowserError, match="đăng nhập"):
        client.fetch(parse_booking_message(booking_message()))
    page.goto.assert_not_called()


def test_worker_login_command_runs_on_own_thread_and_close_releases_only_owned_client(tmp_path):
    state, events, factory = StateStore(tmp_path / "state.json"), queue.Queue(), Mock()
    worker = BookingComWorker(events, state, {"booking_com_enrichment": False}, tmp_path, factory)
    worker.start()
    try:
        worker.login()
        deadline = time.monotonic() + 3
        while not factory.return_value.login.called and time.monotonic() < deadline:
            worker.join(0.02)
        factory.return_value.login.assert_called_once_with(None)
        assert worker.is_alive()
    finally:
        worker.close()
    assert not worker.is_alive()
    factory.return_value.close.assert_called_once()


def test_basic_copy_does_not_erase_clipboard_or_pretend_full_excel_is_ready(monkeypatch):
    import app as desktop

    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.root = Mock()
    notice = Mock()
    monkeypatch.setattr(desktop.messagebox, "showinfo", notice)
    assert not app._copy_alerts_to_clipboard([parse_booking_message(booking_message())], "copied")
    notice.assert_called_once()
    app.root.clipboard_clear.assert_not_called()
    app.root.clipboard_append.assert_not_called()

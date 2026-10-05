from __future__ import annotations

from datetime import date
from email.message import EmailMessage, Message
from pathlib import Path

import pytest

from booking_notifier.models import BOOKING_STATUS_CANCELLED, BOOKING_STATUS_MODIFIED, BOOKING_STATUS_NEW
from booking_notifier.parsing import extract_revenue, html_to_text, parse_booking_message, sender_source

FIXTURES = Path(__file__).parent / "fixtures"
IDENTIFIER = "1666000000000001"
ORIGINAL_SUBJECT = f"Urgent action required - new booking received (booking no. #{IDENTIFIER}#)"
REMINDER_SUBJECT = f"[Reminder] Trip.com New Reservation: {IDENTIFIER}"


def message(template: str = "trip_new_booking.html", *, html: str | None = None, subject: str | None = None,
            sender: str = '"Trip.com" <notify@trip.com>', plain: bool = False) -> EmailMessage:
    mail = EmailMessage()
    mail["From"] = sender
    mail["Subject"] = subject or (REMINDER_SUBJECT if template == "trip_reminder.html" else ORIGINAL_SUBJECT)
    mail["Date"] = "Mon, 05 Oct 2026 11:00:00 +0700"
    source = html if html is not None else (FIXTURES / template).read_text(encoding="utf-8")
    mail.set_content(html_to_text(source) if plain else source, subtype="plain" if plain else "html")
    return mail


@pytest.mark.parametrize("template", ["trip_new_booking.html", "trip_reminder.html"])
def test_trip_original_and_reminder_preserve_complete_booking_fields(template):
    booking = parse_booking_message(message(template))
    assert booking is not None
    assert booking.source == "Trip"
    assert booking.booking_id == IDENTIFIER and booking.storage_id == f"trip:{IDENTIFIER}"
    assert booking.status == BOOKING_STATUS_NEW
    assert booking.guest_name == "TRẦN/LINH"
    assert booking.checkin_date == date(2026, 10, 5) and booking.checkout_date == date(2026, 10, 7)
    assert booking.nights == 2
    assert booking.room_type == ("Deluxe Double Room Test Room Only - Booking Base x2" if template == "trip_new_booking.html"
                                 else "Deluxe Double Room x2")
    assert booking.total_revenue == "VND 800,000.00"


def test_trip_original_and_reminder_have_same_stable_identity_not_subject_hash():
    original = parse_booking_message(message())
    reminder = parse_booking_message(message("trip_reminder.html"))
    assert original.storage_id == reminder.storage_id
    assert original.subject != reminder.subject


@pytest.mark.parametrize("template", ["trip_new_booking.html", "trip_reminder.html"])
def test_trip_plain_alternative_preserves_same_name_rooms_count_and_dates(template):
    html = parse_booking_message(message(template))
    plain = parse_booking_message(message(template, plain=True))
    assert html is not None and plain is not None
    assert (html.guest_name, html.room_type, html.checkin_date, html.checkout_date, html.total_revenue) == (
        plain.guest_name, plain.room_type, plain.checkin_date, plain.checkout_date, plain.total_revenue,
    )


@pytest.mark.parametrize("sender", [
    '"Trip.com" <booking@trip.com>', "TRIP <BOOKING@TRIP.COM>", "notify@hotel.trip.com",
])
def test_trip_trusted_root_and_real_subdomain(sender):
    assert sender_source(sender) == "Trip"
    assert parse_booking_message(message(sender=sender)) is not None


@pytest.mark.parametrize("sender", [
    "Trip.com <booking@trip.com.evil.invalid>", "Trip.com <booking@nottrip.com>",
    "Trip.com <booking@evil-trip.com>", '"booking@trip.com" <attacker@example.invalid>',
    "Trip.com <booking@ctrip.com>", "Trip.com <booking@example.invalid>",
    "Trip.com <booking@trip.com", "Trip.com booking@trip.com <attacker@example.invalid>",
    "Trip.com <booking@trip.com>, Other <attacker@example.invalid>",
])
def test_trip_fake_malformed_or_unproven_sender_is_rejected(sender):
    assert sender_source(sender) == ""
    assert parse_booking_message(message(sender=sender)) is None


def test_trip_duplicate_from_headers_are_rejected():
    mail = Message()
    mail["From"] = "notify@trip.com"
    mail["From"] = "attacker@example.invalid"
    mail["Subject"] = ORIGINAL_SUBJECT
    mail.set_payload(html_to_text((FIXTURES / "trip_new_booking.html").read_text(encoding="utf-8")))
    assert parse_booking_message(mail) is None


@pytest.mark.parametrize(("subject", "status"), [
    (f"Trip.com cancelled reservation #{IDENTIFIER}#", BOOKING_STATUS_CANCELLED),
    (f"Trip.com booking cancellation #{IDENTIFIER}#", BOOKING_STATUS_CANCELLED),
    (f"Trip.com modified reservation #{IDENTIFIER}#", BOOKING_STATUS_MODIFIED),
    (f"Trip.com reservation amended #{IDENTIFIER}#", BOOKING_STATUS_MODIFIED),
    (f"Trip.com booking updated #{IDENTIFIER}#", BOOKING_STATUS_MODIFIED),
    (f"Trip.com cancel booking #{IDENTIFIER}#", BOOKING_STATUS_CANCELLED),
    (f"Trip.com booking modification #{IDENTIFIER}#", BOOKING_STATUS_MODIFIED),
    (f"Trip.com reservation change #{IDENTIFIER}#", BOOKING_STATUS_MODIFIED),
    (f"Trip.com booking amendment #{IDENTIFIER}#", BOOKING_STATUS_MODIFIED),
])
def test_trip_cancelled_and_modified_subjects_are_not_new(subject, status):
    booking = parse_booking_message(message(subject=subject))
    assert booking is not None and booking.status == status


@pytest.mark.parametrize(("kind", "status"), [
    ("Cancel(Prepay)", BOOKING_STATUS_CANCELLED), ("Cancelled", BOOKING_STATUS_CANCELLED),
    ("Cancellation", BOOKING_STATUS_CANCELLED), ("Modify(Prepay)", BOOKING_STATUS_MODIFIED),
    ("Modification", BOOKING_STATUS_MODIFIED), ("Change", BOOKING_STATUS_MODIFIED),
    ("Amendment", BOOKING_STATUS_MODIFIED), ("Update", BOOKING_STATUS_MODIFIED),
])
def test_trip_explicit_reservation_type_overrides_new_subject(kind, status):
    source = (FIXTURES / "trip_reminder.html").read_text(encoding="utf-8").replace("New(Prepay)", kind)
    booking = parse_booking_message(message("trip_reminder.html", html=source))
    assert booking is not None and booking.status == status


@pytest.mark.parametrize("kind", ["Payment", "Invoice", "No show", "Unknown"])
def test_trip_unknown_lifecycle_field_never_becomes_new_booking(kind):
    source = (FIXTURES / "trip_reminder.html").read_text(encoding="utf-8").replace("New(Prepay)", kind)
    assert parse_booking_message(message("trip_reminder.html", html=source)) is None


def test_trip_net_payout_wins_over_gross_total_regardless_of_row_order():
    rows = [["Total amount", "VND 1,000,000"], ["Final room rate (incl. taxes and fees)", "VND 900,000"],
            ["Your payout", "VND 800,000"]]
    assert extract_revenue("", rows, "Trip") == "VND 800,000"
    assert extract_revenue("", list(reversed(rows)), "Trip") == "VND 800,000"


def test_trip_daily_payouts_without_currency_do_not_replace_booking_total_or_get_summed():
    source = (FIXTURES / "trip_new_booking.html").read_text(encoding="utf-8")
    source = source.replace("<td>VND 800,000.00</td>", "<td>800,000.00 VND</td>")
    assert parse_booking_message(message(html=source)).total_revenue == "800,000.00 VND"
    source = source.replace("<tr><td>Your payout</td><td>800,000.00 VND</td></tr>", "")
    assert parse_booking_message(message(html=source)).total_revenue == ""


@pytest.mark.parametrize("template", ["trip_new_booking.html", "trip_reminder.html"])
def test_trip_missing_booking_identifier_is_not_an_idless_alert(template):
    source = (FIXTURES / template).read_text(encoding="utf-8").replace(IDENTIFIER, "")
    assert parse_booking_message(message(template, html=source, subject="Trip.com New Reservation")) is None


def test_trip_unrelated_reservation_word_cannot_be_used_as_identifier():
    assert parse_booking_message(message(html="<p>New reservation</p><p>Reservation: details</p><p>Check-in: 2026/10/05</p>",
                                         subject="Trip.com reservation details")) is None


@pytest.mark.parametrize("period", [
    "2026-10-05 - 2026-10-07", "5 Oct 2026 - 7 Oct 2026", "October 5, 2026–October 7, 2026",
])
def test_trip_stay_period_range_parses_both_dates_without_using_arrival_time(period):
    source = (FIXTURES / "trip_new_booking.html").read_text(encoding="utf-8").replace(
        "Oct 5, 2026 - Oct 7, 2026", period,
    )
    booking = parse_booking_message(message(html=source))
    assert booking.checkin_date == date(2026, 10, 5) and booking.checkout_date == date(2026, 10, 7)


def test_trip_multiline_name_preserves_both_slash_parts():
    source = (FIXTURES / "trip_reminder.html").read_text(encoding="utf-8").replace("TRẦN/LINH", "TRẦN/<br>THỊ LINH")
    booking = parse_booking_message(message("trip_reminder.html", html=source))
    assert booking.guest_name == "TRẦN/THỊ LINH" or booking.guest_name == "TRẦN/ THỊ LINH"


@pytest.mark.parametrize("subject", ["Trip.com Payment completed", "Trip.com Monthly remittance", "Trip.com Invoice"])
def test_trip_payment_notice_with_quoted_confirmation_never_creates_booking(subject):
    assert parse_booking_message(message(subject=subject)) is None


def test_trip_card_fields_are_boundaries_not_booking_name_or_room_content():
    source = """New booking
Reservation: 1666000000000001
Guest Name: Synthetic Guest
Virtual Credit Card: 4111-1111-1111-1111
CVC: 000
Check-in: 2026/10/05
Check-out: 2026/10/07
Room Type: Deluxe Double
Room(s): 1
Total amount: VND 800,000
"""
    mail = message(html="", subject=REMINDER_SUBJECT)
    mail.set_content(source)
    booking = parse_booking_message(mail)
    assert booking.guest_name == "Synthetic Guest" and booking.room_type == "Deluxe Double x1"
    assert not any("4111-1111-1111-1111" in str(value) for value in booking.to_dict().values())

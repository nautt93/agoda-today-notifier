from __future__ import annotations

from datetime import date
from email.message import EmailMessage, Message
from pathlib import Path

import pytest

from booking_notifier.models import BOOKING_STATUS_CANCELLED, BOOKING_STATUS_MODIFIED, BOOKING_STATUS_NEW
from booking_notifier.parsing import html_to_text, parse_booking_message, parse_date, sender_source

FIXTURE = Path(__file__).parent / "fixtures" / "traveloka_confirmation.html"
SUBJECT = "CONFIRMED - Traveloka Itinerary ID 20261234000001 (Test Hotel, VIETNAM)"


def make_message(*, html: str | None = None, text: str = "", subject: str = SUBJECT,
                 sender: str = "Traveloka <booking@traveloka.com>") -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject
    message["Date"] = "Sun, 04 Oct 2026 08:00:00 +0700"
    message.set_content(text)
    if html is not None:
        message.add_alternative(html, subtype="html")
    return message


def test_traveloka_confirmation_matches_anonymized_sample_structure():
    event = parse_booking_message(make_message(html=FIXTURE.read_text(encoding="utf-8")))

    assert event is not None
    assert event.source == "Traveloka"
    assert event.booking_id == "20261234000001"
    assert event.storage_id == "traveloka:20261234000001"
    assert event.status == BOOKING_STATUS_NEW
    assert event.guest_name == "Linh Trần"
    assert event.checkin_date == date(2026, 10, 4)
    assert event.checkout_date == date(2026, 10, 6)
    assert event.nights == 2
    assert event.room_type == "Deluxe Double - ROOM ONLY x2"
    assert event.total_revenue == "VND 800,000"


@pytest.mark.parametrize("sender", [
    "Traveloka <booking@traveloka.com>",
    "TRAVELOKA <BOOKING@TRAVELOKA.COM>",
    "Traveloka <booking@notifications.traveloka.com>",
])
def test_traveloka_trusted_domain_and_real_subdomain(sender):
    assert sender_source(sender) == "Traveloka"
    assert parse_booking_message(make_message(html=FIXTURE.read_text(encoding="utf-8"), sender=sender)) is not None


@pytest.mark.parametrize("sender", [
    "Traveloka <booking@traveloka.com.evil.invalid>",
    "Traveloka <booking@fake-traveloka.com>",
    "Traveloka <booking@nottraveloka.com>",
    '"booking@traveloka.com" <attacker@example.invalid>',
    "Traveloka",
    "Traveloka <booking@example.invalid>",
])
def test_traveloka_fake_sender_is_rejected(sender):
    assert sender_source(sender) == ""
    assert parse_booking_message(make_message(html=FIXTURE.read_text(encoding="utf-8"), sender=sender)) is None


@pytest.mark.parametrize("sender", [
    "Traveloka hotel@traveloka.com <attacker@example.invalid>",
    "hotel@traveloka.com <attacker@example.invalid>",
    "Traveloka <hotel@traveloka.com>, Other <attacker@example.invalid>",
    "Traveloka <hotel@traveloka.com",
    "Traveloka <hotel@traveloka.com>>",
    "Traveloka hotel@traveloka.com",
    "Traveloka: hotel@traveloka.com;",
    "Traveloka <hotel@traveloka.com>\nFrom: attacker@example.invalid",
])
def test_traveloka_raw_sender_must_have_one_unambiguous_mailbox(sender):
    assert sender_source(sender) == ""
    message = Message()
    message["From"] = sender
    message["Subject"] = SUBJECT
    message.set_payload(html_to_text(FIXTURE.read_text(encoding="utf-8")))
    assert parse_booking_message(message) is None


@pytest.mark.parametrize("sender", [
    "Traveloka hotel@traveloka.com <attacker@example.invalid>",
    "hotel@traveloka.com <attacker@example.invalid>",
    "Traveloka <hotel@traveloka.com",
    "Traveloka <hotel@traveloka.com>>",
])
def test_traveloka_normalized_header_defects_cannot_grant_provider_trust(sender):
    message = make_message(html=FIXTURE.read_text(encoding="utf-8"), sender=sender)
    assert message["From"].defects
    assert sender_source(message["From"]) == ""
    assert parse_booking_message(message) is None


def test_traveloka_duplicate_from_headers_are_rejected():
    message = Message()
    message["From"] = "Traveloka <hotel@traveloka.com>"
    message["From"] = "Attacker <attacker@example.invalid>"
    message["Subject"] = SUBJECT
    message.set_payload(html_to_text(FIXTURE.read_text(encoding="utf-8")))
    assert parse_booking_message(message) is None


@pytest.mark.parametrize(("sender", "source"), [
    ("Traveloka <hotel@traveloka.com>", "Traveloka"),
    ('"Traveloka, Booking" <hotel@traveloka.com>', "Traveloka"),
    ("Traveloka\r\n <hotel@traveloka.com>", "Traveloka"),
    ("Traveloka (Bookings) <hotel@traveloka.com>", "Traveloka"),
    ("Agoda <booking@agoda.com>", "Agoda"),
    ("Agoda Booking <booking@mail.agoda.net>", "Agoda"),
    ("Expedia Group <booknotif@expedia.com>", "Expedia"),
    ("notify@mail.expediapartnercentral.com", "Expedia"),
])
def test_strict_mailbox_parser_preserves_legitimate_provider_headers(sender, source):
    assert sender_source(sender) == source


@pytest.mark.parametrize(("subject", "expected"), [
    ("CANCELLED - Traveloka Itinerary ID 20261234000001", BOOKING_STATUS_CANCELLED),
    ("CANCELED - Traveloka Itinerary ID 20261234000001", BOOKING_STATUS_CANCELLED),
    ("Booking Cancellation - Traveloka Itinerary ID 20261234000001", BOOKING_STATUS_CANCELLED),
    ("MODIFIED - Traveloka Itinerary ID 20261234000001", BOOKING_STATUS_MODIFIED),
    ("AMENDED - Traveloka Itinerary ID 20261234000001", BOOKING_STATUS_MODIFIED),
    ("UPDATED - Traveloka Itinerary ID 20261234000001", BOOKING_STATUS_MODIFIED),
])
def test_traveloka_lifecycle_subject_is_not_new_confirmation(subject, expected):
    event = parse_booking_message(make_message(html=FIXTURE.read_text(encoding="utf-8"), subject=subject))
    assert event is not None
    assert event.status == expected


@pytest.mark.parametrize(("status", "expected"), [
    ("Cancelled", BOOKING_STATUS_CANCELLED),
    ("Modified", BOOKING_STATUS_MODIFIED),
])
def test_traveloka_explicit_body_status_overrides_confirmation(status, expected):
    body = FIXTURE.read_text(encoding="utf-8") + f"<p>Booking status: {status}</p>"
    event = parse_booking_message(make_message(html=body))
    assert event is not None
    assert event.status == expected


def test_traveloka_confirmation_does_not_confuse_cancellation_policy_for_event_status():
    body = FIXTURE.read_text(encoding="utf-8").replace(
        "Until the deadline, free of charge; after the deadline, 100% charge",
        "A booking can be cancelled free of charge until the deadline. Cancellation policy applies afterwards.",
    )
    assert parse_booking_message(make_message(html=body)).status == BOOKING_STATUS_NEW


def test_traveloka_requires_id_and_valid_checkin_for_new_notification():
    body = FIXTURE.read_text(encoding="utf-8")
    assert parse_booking_message(make_message(html=body.replace("20261234000001", ""), subject="New Booking")) is None
    assert parse_booking_message(make_message(html=body.replace("Oct 4, 2026", "unknown"))) is None


def test_traveloka_confirmed_subject_accepts_template_without_new_booking_heading():
    body = FIXTURE.read_text(encoding="utf-8").replace("<h2>New Booking</h2>", "")
    assert parse_booking_message(make_message(html=body)) is not None
    assert parse_booking_message(make_message(html=body, subject="Traveloka marketing announcement")) is None


def test_traveloka_plain_alternative_uses_same_fields_without_double_room_count():
    body = FIXTURE.read_text(encoding="utf-8")
    plain = html_to_text(body)
    only_plain = parse_booking_message(make_message(text=plain))
    both = parse_booking_message(make_message(html=body, text=plain))
    assert only_plain is not None
    assert both is not None
    for event in (only_plain, both):
        assert event.guest_name == "Linh Trần"
        assert event.room_type == "Deluxe Double - ROOM ONLY x2"
        assert event.total_revenue == "VND 800,000"


def test_traveloka_wrapped_guest_name_preserves_all_first_and_last_name_lines():
    text = """New Booking
Itinerary ID: 20261234000002
Customer First Name:
Nguyễn
Thị
Customer Last Name:
Minh
Anh
Guest Email: synthetic@example.invalid
Check-in: 04/10/2026
Check-out: 06/10/2026
Room Type: Family Suite
Number of rooms: 3
Total you will receive: VND 900,000
"""
    event = parse_booking_message(make_message(text=text, subject="New Booking"))
    assert event is not None
    assert event.guest_name == "Nguyễn Thị Minh Anh"
    assert event.room_type == "Family Suite x3"
    assert event.total_revenue == "VND 900,000"


def test_traveloka_multiple_room_grids_combine_allocations_not_guest_or_extra_bed_counts():
    body = FIXTURE.read_text(encoding="utf-8").replace("0 per room", "3 per room")
    body += """<table>
<tr><th>Room Information</th><th>Guest Information</th><th>Extra Bed Information</th></tr>
<tr><td>(1 × ) Deluxe Double - ROOM ONLY</td><td>8 Adult(s)</td><td>5 per room</td></tr>
<tr><td>(1 × ) Family Suite</td><td>4 Adult(s)</td><td>2 per room</td></tr>
</table>"""
    event = parse_booking_message(make_message(html=body))
    assert event is not None
    assert event.room_type == "Deluxe Double - ROOM ONLY x3; Family Suite x1"


@pytest.mark.parametrize("count", ["0", "101"])
def test_traveloka_invalid_room_quantity_is_not_invented(count):
    body = FIXTURE.read_text(encoding="utf-8").replace("(2 × )", f"({count} × )")
    event = parse_booking_message(make_message(html=body))
    assert event is not None
    assert event.room_type == ""


def test_traveloka_payout_is_not_subtotal_or_guest_gross_even_if_payout_is_missing():
    body = FIXTURE.read_text(encoding="utf-8").replace(
        "<tr><td>Total you will receive</td><td>VND 800,000</td></tr>", "",
    ) + "<p>Total Amount: VND 950,000</p><p>Grand Total: VND 950,000</p>"
    event = parse_booking_message(make_message(html=body))
    assert event is not None
    assert event.total_revenue == ""


@pytest.mark.parametrize(("value", "expected"), [
    ("04/10/2026", date(2026, 10, 4)),
    ("Oct 4, 2026", date(2026, 10, 4)),
    ("4 October 2026", date(2026, 10, 4)),
    ("2026-10-04", date(2026, 10, 4)),
])
def test_traveloka_supported_checkin_date_formats(value, expected):
    assert parse_date(value, "Traveloka") == expected

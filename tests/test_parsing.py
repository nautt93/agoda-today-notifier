from __future__ import annotations

from datetime import date
from email.message import EmailMessage

import pytest

from booking_notifier.models import BOOKING_STATUS_CANCELLED, BOOKING_STATUS_MODIFIED
from booking_notifier.parsing import (
    extract_booking_id,
    extract_revenue,
    parse_booking_message,
    parse_date,
    sender_source,
)

BASE = """Itinerary ID: 1234567890123
Traveler name: Jane Doe
Check-in: September 30, 2026
Check-out: October 2, 2026
Room Type Name: Deluxe King
Payment Model: Expedia Collect
Amount to Charge Expedia Group: VND 1,800,000
"""


def test_agoda_and_expedia_use_the_same_booking_fields(expedia_message_factory):
    agoda = EmailMessage()
    agoda["From"] = "Agoda <booking@agoda.com>"
    agoda["Subject"] = "New booking confirmation"
    agoda["Date"] = "Wed, 30 Sep 2026 10:00:00 +0700"
    agoda.set_content("""Agoda Booking ID: 987654321
Guest Name: Jane Doe
Check-in: 30/09/2026
Check-out: 02/10/2026
Room Type Name: Deluxe King
Booked and Payable by Agoda: VND 1,800,000
""")
    expedia = parse_booking_message(expedia_message_factory(BASE))
    parsed_agoda = parse_booking_message(agoda)
    assert parsed_agoda is not None
    assert expedia is not None
    assert parsed_agoda.source == "Agoda"
    assert expedia.source == "Expedia"
    assert parsed_agoda.guest_name == expedia.guest_name == "Jane Doe"
    assert parsed_agoda.room_type == expedia.room_type == "Deluxe King"
    assert parsed_agoda.checkin_date == expedia.checkin_date == date(2026, 9, 30)
    assert parsed_agoda.total_revenue == expedia.total_revenue == "VND 1,800,000"


def test_agoda_customer_info_and_room_summary_are_read_in_full():
    message = EmailMessage()
    message["From"] = "Agoda <booking@agoda.com>"
    message["Subject"] = "New booking confirmation"
    message["Date"] = "Wed, 30 Sep 2026 10:00:00 +0700"
    message.set_content("New booking confirmation")
    message.add_alternative("""<html><body>
    <p>New booking confirmation</p>
    <p>Agoda Booking ID: 707908051</p>
    <table><tr><th>Customer Info</th><td>Name: NGUYEN VAN AN, Phone: +84 900 000 000</td></tr></table>
    <p>Check-in: 30/09/2026</p><p>Check-out: 02/10/2026</p>
    <p>Rooms:</p><p>2 x Deluxe Double Room</p><p>1 x Family Suite</p>
    <p>Booked and Payable by Agoda: VND 1,031,040</p>
    </body></html>""", subtype="html")

    alert = parse_booking_message(message)

    assert alert is not None
    assert alert.guest_name == "NGUYEN VAN AN"
    assert alert.room_type == "Deluxe Double Room x2; Family Suite"


def test_agoda_separate_first_last_name_and_room_grid_are_supported():
    message = EmailMessage()
    message["From"] = "Agoda <booking@agoda.com>"
    message["Subject"] = "New reservation confirmation"
    message.set_content("fallback")
    message.add_alternative("""<html><body>
    <p>New reservation confirmation</p><p>Agoda Booking ID: 1780410606</p>
    <table><tr><th>Customer First Name</th><td>Tuấn</td>
    <th>Customer Last Name</th><td>Nguyễn</td></tr></table>
    <p>Check-in: 30/09/2026</p><p>Check-out: 01/10/2026</p>
    <table><tr><th>Room Type Name</th><th>No. of Rooms</th></tr>
    <tr><td>Superior King</td><td>2 rooms</td></tr></table>
    </body></html>""", subtype="html")

    alert = parse_booking_message(message)

    assert alert is not None
    assert alert.guest_name == "Tuấn Nguyễn"
    assert alert.room_type == "Superior King x2"


def test_expedia_happy_path(expedia_message_factory):
    alert = parse_booking_message(expedia_message_factory(BASE))
    assert alert is not None
    assert alert.source == "Expedia"
    assert alert.booking_id == "1234567890123"
    assert alert.checkin_date == date(2026, 9, 30)
    assert alert.checkout_date == date(2026, 10, 2)
    assert alert.nights == 2
    assert alert.guest_name == "Jane Doe"
    assert alert.room_type == "Deluxe King"
    assert alert.total_revenue == "VND 1,800,000"


def test_expedia_html_repeated_rooms(expedia_message_factory):
    html = """<html><body>
    <p>New reservation confirmation</p>
    <p>Itinerary ID: 1234567890123</p><p>Traveler name: Jane Doe</p>
    <p>Check-in: September 30, 2026</p><p>Check-out: October 2, 2026</p>
    <table><tr><th>Room type</th><th>Confirmation ID</th></tr>
    <tr><td>Deluxe King</td><td>ABC123</td></tr>
    <tr><td>Deluxe King</td><td>ABC124</td></tr>
    <tr><td>Family Suite</td><td>DEF123</td></tr></table>
    <table><tr><th>Payment Model</th><td>Expedia Collect</td></tr>
    <tr><th>Amount to Charge Expedia Group</th><td>VND 1,800,000</td></tr></table>
    </body></html>"""
    alert = parse_booking_message(expedia_message_factory("fallback", html=html))
    assert alert is not None
    assert alert.room_type == "Deluxe King x2; Family Suite"


def test_revenue_priority_does_not_depend_on_row_order():
    rows = [
        ["Payment Model", "Expedia Collect"],
        ["Net Rate", "VND 800,000"],
        ["Amount to Charge Expedia Group", "VND 900,000"],
    ]
    text = "\n".join(": ".join(row) for row in rows)
    assert extract_revenue(text, rows, "Expedia") == "VND 900,000"
    assert extract_revenue(text, list(reversed(rows)), "Expedia") == "VND 900,000"


def test_hotel_collect_total_booking_amount_is_supported():
    rows = [
        ["Payment Model", "Hotel Collect"],
        ["Total Booking Amount", "795,273 VND"],
    ]
    text = "Payment Instructions: Hotel collects payment\nTotal Booking Amount: 795,273 VND"
    assert extract_revenue(text, rows, "Expedia") == "795,273 VND"


@pytest.mark.parametrize(
    ("value", "source", "expected"),
    [
        ("09/30/2026", "Expedia", date(2026, 9, 30)),
        ("30/09/2026", "Expedia", date(2026, 9, 30)),
        ("10/04/2026", "Expedia", date(2026, 10, 4)),
        ("04/10/2026", "Agoda", date(2026, 10, 4)),
        ("September 30, 2026", "Expedia", date(2026, 9, 30)),
    ],
)
def test_date_formats(value, source, expected):
    assert parse_date(value, source) == expected


def test_booking_id_requires_a_real_identifier():
    assert extract_booking_id("Itinerary details") == ""
    assert extract_booking_id("Your itinerary includes breakfast") == ""
    assert extract_booking_id("Itinerary confirmation: 1234567") == "1234567"
    assert extract_booking_id("Itinerary ID: ABC-12345") == "ABC-12345"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (BASE + "\nReservation status: modified", BOOKING_STATUS_MODIFIED),
        (BASE + "\nNotification type: Booking Modified", BOOKING_STATUS_MODIFIED),
        (BASE + "\nNotification type: Modification", BOOKING_STATUS_MODIFIED),
        (BASE + "\nStatus: Cancelled", BOOKING_STATUS_CANCELLED),
    ],
)
def test_lifecycle_status_detected_in_body(expedia_message_factory, body, expected):
    event = parse_booking_message(expedia_message_factory(body, subject="Booking notification"))
    assert event is not None
    assert event.status == expected


def test_spoofed_sender_is_rejected(expedia_message_factory):
    message = expedia_message_factory(BASE, sender="Expedia <fraud@example.com>")
    assert parse_booking_message(message) is None
    assert sender_source(str(message["From"])) == ""


def test_subdomain_sender_is_accepted():
    assert sender_source("notify@mail.expediapartnercentral.com") == "Expedia"
    assert sender_source("notify@evil-expedia.com") == ""

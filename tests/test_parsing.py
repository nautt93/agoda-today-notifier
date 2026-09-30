from __future__ import annotations

from datetime import date

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


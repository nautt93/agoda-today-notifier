from __future__ import annotations

from datetime import date
from email.message import EmailMessage
from pathlib import Path

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
    assert alert.room_type == "Deluxe Double Room x2; Family Suite x1"


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


@pytest.mark.parametrize("arrival", ["30-Sep-2026", "30/September/2026", "30 tháng 9 năm 2026", "30‑Sep‑2026"])
def test_agoda_additional_arrival_dates(arrival):
    assert parse_date(arrival, "Agoda") == date(2026, 9, 30)


@pytest.mark.parametrize("arrival", ["30 9, 2026", "30 tháng 9, 2026", "30/09/2026", "30 September 2026"])
@pytest.mark.parametrize("label", ["Check-in", "Ngày nhận phòng", "Arrival date"])
def test_legacy_agoda_inline_confirmation_dates(arrival, label):
    message = EmailMessage()
    message["From"] = "Agoda <no-reply@mail.agoda.com>"
    message["Subject"] = "Booking confirmation"
    message.set_content(f"Booking ID: 987654321 Guest Name: Jane Doe {label}: {arrival} Check-out: 02/10/2026")
    event = parse_booking_message(message)
    assert event is not None
    assert event.checkin_date == date(2026, 9, 30)
    assert event.checkout_date == date(2026, 10, 2)


def test_legacy_agoda_without_readable_booking_id_still_alerts():
    message = EmailMessage()
    message["From"] = "booking@agoda.com"
    message["Subject"] = "Booking confirmation"
    message.set_content("Guest Name: Jane Doe\nCheck-in: 30 9, 2026")
    event = parse_booking_message(message)
    assert event is not None
    assert event.booking_id == ""
    assert event.checkin_date == date(2026, 9, 30)


def test_expedia_reservation_details_template(expedia_message_factory):
    event = parse_booking_message(expedia_message_factory(BASE, subject="Reservation details"))
    assert event is not None
    assert event.source == "Expedia"


def test_agoda_localized_voucher_nested_html_and_horizontal_dates():
    message = EmailMessage()
    message["From"] = "booking@agoda.com"
    message["Subject"] = "Agoda – Phiếu đặt phòng 987654321"
    message.set_content("Vui lòng xem nội dung HTML")
    message.add_alternative("""<table><tr><td><table>
    <tr><td>Agoda Booking ID: 987654321</td></tr>
    <tr><th>Check-in</th><th>Check-out</th></tr>
    <tr><td>30-Sep-2026</td><td>02-Oct-2026</td></tr>
    </table></td><td>Guest Name: Nguyen Van An</td></tr></table>""", subtype="html")
    event = parse_booking_message(message)
    assert event is not None
    assert event.checkin_date == date(2026, 9, 30)
    assert event.checkout_date == date(2026, 10, 2)


def test_agoda_unknown_charset_does_not_block_booking():
    message = EmailMessage()
    message["From"] = "booking@agoda.com"
    message["Subject"] = "Agoda voucher"
    message["Content-Type"] = 'text/plain; charset="unknown-charset"'
    message.set_payload("Booking ID: 987654321\nCheck-in: 30-Sep-2026\nGuest Name: Jane Doe")
    assert parse_booking_message(message).checkin_date == date(2026, 9, 30)


def test_agoda_vietnamese_cancellation_does_not_become_new_booking():
    message = EmailMessage()
    message["From"] = "booking@agoda.com"
    message["Subject"] = "Hủy đặt phòng Agoda"
    message.set_content("Booking ID: 987654321\nCheck-in: 30-Sep-2026")
    assert parse_booking_message(message).status == BOOKING_STATUS_CANCELLED


def detail_message(source, plain, html=None):
    message = EmailMessage()
    message["From"] = "booking@agoda.com" if source == "Agoda" else "notify@expediapartnercentral.com"
    message["Subject"] = "New booking confirmation"
    message.set_content(f"Booking ID: 987654321\nCheck-in: 30/09/2026\n{plain}")
    if html:
        message.add_alternative(html, subtype="html")
    return message


@pytest.mark.parametrize("source", ["Agoda", "Expedia"])
@pytest.mark.parametrize("label", ["Guest Name", "Lead Guest Name", "Traveler Name", "Tên khách"])
def test_wrapped_plain_name_and_room_are_complete(source, label):
    event = parse_booking_message(detail_message(source, f"""{label}: Nguyễn
Văn An
Room Type Name: Deluxe Double
Room with Balcony
No. of Rooms: 2
Phone: +84 900 000 000
"""))
    assert event.guest_name == "Nguyễn Văn An"
    assert event.room_type == "Deluxe Double Room with Balcony x2"


@pytest.mark.parametrize("tag", ["br", "p", "div"])
def test_complete_html_name_wins_over_incomplete_plain_alternative(tag):
    separator = "<br>" if tag == "br" else f"</{tag}><{tag}>"
    name = "NGUYEN" + separator + "VAN AN"
    room = "Superior" + separator + "Double Room"
    if tag != "br":
        name, room = f"<{tag}>{name}</{tag}>", f"<{tag}>{room}</{tag}>"
    message = detail_message("Agoda", "Lead Guest Name: NGUYEN", f"""<table>
    <tr><td>Lead Guest Name</td><td>{name}</td></tr>
    <tr><td>Room Type</td><td>{room}</td></tr>
    <tr><td>Check-in</td><td>30/09/2026</td></tr></table>""")
    event = parse_booking_message(message)
    assert event.guest_name == "NGUYEN VAN AN"
    assert event.room_type == "Superior Double Room"


def test_split_html_first_last_names_win_over_first_line_of_lead_name():
    message = detail_message("Agoda", "Lead Guest Name: Tuấn", """<table>
    <tr><td>Customer First Name</td><td>Tuấn</td><td>Customer Last Name</td><td>Nguyễn</td></tr>
    <tr><td>Room Type Name</td><td>Superior King</td></tr></table>""")
    event = parse_booking_message(message)
    assert event.guest_name == "Tuấn Nguyễn"
    assert event.room_type == "Superior King"


def test_horizontal_name_and_room_headers_read_values_below():
    message = detail_message("Agoda", "fallback", """<table>
    <tr><th>Guest Name</th><th>Room Type</th></tr>
    <tr><td>NGUYEN<br>VAN AN</td><td>Deluxe Double</td></tr></table>""")
    event = parse_booking_message(message)
    assert event.guest_name == "NGUYEN VAN AN"
    assert event.room_type == "Deluxe Double"


def test_customer_info_name_wrapping_stops_before_contact_information():
    event = parse_booking_message(detail_message("Agoda", """Customer Info - Name: Nguyễn
Văn An, Phone: +84 900 000 000
Room Type: Superior Double
Email: guest@example.com
"""))
    assert event.guest_name == "Nguyễn Văn An"
    assert event.room_type == "Superior Double"


def test_room_name_field_is_not_room_code_or_other_booking_metadata():
    event = parse_booking_message(detail_message("Agoda", """Guest Name: Jane Doe
Room Type Code: DLX123
Hạng phòng: Deluxe Double
No. of Rooms: 1
"""))
    assert event.guest_name == "Jane Doe"
    assert event.room_type == "Deluxe Double x1"


def test_empty_fields_do_not_take_the_next_labels_as_values():
    event = parse_booking_message(detail_message("Agoda", """Guest Name:
Room Type:
Phone: +84 900 000 000
"""))
    assert event.guest_name == ""
    assert event.room_type == ""


def test_split_room_summary_quantity_is_supported():
    event = parse_booking_message(detail_message("Agoda", """Guest Name: Jane Doe
Room(s):
2 x
Superior Double Room
1 x Family Suite
Check-out: 02/10/2026
"""))
    assert event.room_type == "Superior Double Room x2; Family Suite x1"


def test_rich_non_table_html_wins_over_shortened_plain_name():
    message = detail_message("Agoda", "Guest Name: NGUYEN", """<div>Guest Name:</div>
    <div>NGUYEN</div><div>VAN AN</div><div>Room Type:</div>
    <div>Superior Double Room</div><div>Check-in: 30/09/2026</div>""")
    event = parse_booking_message(message)
    assert event.guest_name == "NGUYEN VAN AN"
    assert event.room_type == "Superior Double Room"


def test_room_summary_keeps_wrapped_names_and_all_allocations():
    event = parse_booking_message(detail_message("Agoda", """Guest Name: Jane Doe
Rooms:
2 x Deluxe
Double Room
1 x Family
Suite
Phone: +84 900 000 000
"""))
    assert event.room_type == "Deluxe Double Room x2; Family Suite x1"


@pytest.mark.parametrize("quantity", [1, 2, 3])
def test_agoda_bilingual_voucher_reads_full_name_room_and_quantity(quantity):
    html = (Path(__file__).parent / "fixtures" / "agoda_bilingual_confirmation.html").read_text(encoding="utf-8")
    html = html.replace("</td><td>1</td><td>2 Adults", f"</td><td>{quantity}</td><td>2 Adults")
    message = detail_message("Agoda", "Lead Guest Name: Minh", html)
    event = parse_booking_message(message)
    assert event.guest_name == "Minh Trần"
    assert event.room_type == f"Bunk Bed in Mixed Dormitory Room x{quantity}"
    assert event.checkin_date == date(2026, 9, 30)
    assert event.checkout_date == date(2026, 10, 2)
    assert event.total_revenue == "VND 1,200,000.00"


def test_plain_stacked_bilingual_first_last_names_are_combined_before_partial_name():
    event = parse_booking_message(detail_message("Agoda", """Guest Name: Minh
Customer First Name
Tên Khách Hàng
Minh
Customer Last Name
Họ Khách Hàng
Trần
Room Type: Deluxe King
No. of Rooms: 1
"""))
    assert event.guest_name == "Minh Trần"
    assert event.room_type == "Deluxe King x1"


def test_bilingual_matching_never_treats_room_code_as_room_name():
    message = detail_message("Agoda", "Guest Name: Jane Doe", """<table>
    <tr><td>Room Type Code</td><td>DO-NOT-DISPLAY</td></tr>
    <tr><td>Room Type Name<br>Loại Phòng</td><td>Deluxe King</td></tr>
    <tr><td>Number of Rooms<br>Số phòng</td><td>1</td></tr></table>""")
    assert parse_booking_message(message).room_type == "Deluxe King x1"


def test_stacked_bilingual_room_name_and_quantity_in_plain_text():
    event = parse_booking_message(detail_message("Agoda", """Guest Name
Tên khách
Minh Trần
Room Type
Loại Phòng
Bunk Bed in Mixed Dormitory Room
No. of Rooms
Số phòng
1
Occupancy
Số người
2 Adults
"""))
    assert event.guest_name == "Minh Trần"
    assert event.room_type == "Bunk Bed in Mixed Dormitory Room x1"

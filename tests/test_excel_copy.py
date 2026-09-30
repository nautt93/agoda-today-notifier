from __future__ import annotations

from datetime import date
from email.message import EmailMessage

from app import excel_tsv, excel_tsv_rows
from booking_notifier.excel_export import excel_amount_value
from booking_notifier.models import BookingEvent
from booking_notifier.parsing import parse_booking_message


def booking(booking_id: str, guest_name: str) -> BookingEvent:
    return BookingEvent(
        source="Expedia",
        booking_id=booking_id,
        guest_name=guest_name,
        room_type="Deluxe King",
        checkin_date=date(2026, 9, 30),
        checkout_date=date(2026, 10, 2),
        total_revenue="VND 1,800,000",
        subject="New reservation confirmation",
    )


def test_excel_row_keeps_nine_columns_and_blocks_formula_injection():
    columns = excel_tsv(booking("123456789", "=HYPERLINK(\"bad\")")).split("\t")
    assert len(columns) == 9
    assert columns[1].startswith("'=")
    assert columns[0] == ""
    assert columns[2:6] == ["30", "2", "2", "1800000"]
    assert columns[6:] == ["", "", "Expedia Deluxe King"]


def test_agoda_exact_legacy_layout():
    alert = booking("123456789", "NGUYEN VAN AN")
    alert.source = "Agoda"
    alert.total_revenue = "VND 1,031,040.00"
    assert excel_tsv(alert) == "\tNGUYEN VAN AN\t30\t2\t2\t1031040\t\t\tAgoda Deluxe King"
    alert.room_type = "Agoda Deluxe King"
    assert excel_tsv(alert).endswith("\tAgoda Deluxe King")


def test_excel_whitespace_and_missing_fields():
    alert = booking("123", "  \t=SUM(1)\n ")
    alert.checkout_date = None
    alert.room_type = ""
    alert.total_revenue = ""
    assert excel_tsv(alert) == "\t'=SUM(1)\t30\t\t\t\t\t\tExpedia"


def test_legacy_amounts():
    for value, expected in (("VND 1.800.000,00", "1800000"), ("USD 1,234.50", "1234.50"),
                            ("431 568 ₫", "431568"), ("N/A", ""), ("=SUM(A1)", "1")):
        assert excel_amount_value(value) == expected


def test_multiple_bookings_copy_as_multiple_excel_rows():
    text = excel_tsv_rows([
        booking("123456789", "Jane Doe"),
        booking("987654321", "John Doe"),
    ])
    rows = text.split("\r\n")
    assert len(rows) == 2
    assert all(len(row.split("\t")) == 9 for row in rows)


def test_wrapped_booking_fields_reach_exact_legacy_excel_columns():
    message = EmailMessage()
    message["From"] = "booking@agoda.com"
    message["Subject"] = "Booking confirmation"
    message.set_content("""Booking ID: 987654321
Check-in: 30/09/2026
Check-out: 02/10/2026
Guest Name: Nguyễn
Văn An
Room Type: Superior
Double Room
Booked and Payable by Agoda: VND 1,031,040.00
""")
    alert = parse_booking_message(message)
    assert excel_tsv(alert) == "\tNguyễn Văn An\t30\t2\t2\t1031040\t\t\tAgoda Superior Double Room"

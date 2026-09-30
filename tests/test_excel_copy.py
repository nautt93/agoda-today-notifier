from __future__ import annotations

from datetime import date

from app import excel_tsv, excel_tsv_rows
from booking_notifier.models import BookingEvent


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
    assert columns[3:6] == ["30/09/2026", "02/10/2026", "2"]


def test_multiple_bookings_copy_as_multiple_excel_rows():
    text = excel_tsv_rows([
        booking("123456789", "Jane Doe"),
        booking("987654321", "John Doe"),
    ])
    rows = text.split("\r\n")
    assert len(rows) == 2
    assert all(len(row.split("\t")) == 9 for row in rows)

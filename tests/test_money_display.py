from __future__ import annotations

from datetime import date
from unittest.mock import Mock

import pytest

from app import BookingNotifierApp
from booking_notifier.excel_export import excel_tsv
from booking_notifier.models import BookingEvent
from booking_notifier.money import format_revenue

SCREENSHOT_AMOUNTS = [
    ("VND 368,000.00", "VND 368,000.00"),
    ("VND 378675", "VND 378,675.00"),
    ("VND1672800.00", "VND 1,672,800.00"),
    ("VND 1,018,845.00", "VND 1,018,845.00"),
    ("VND255840.00", "VND 255,840.00"),
    ("273,191 VND", "VND 273,191.00"),
]


@pytest.mark.parametrize(("value", "expected"), [
    *SCREENSHOT_AMOUNTS,
    ("VND 378.675,00", "VND 378,675.00"),
    ("378 675 ₫", "VND 378,675.00"),
    ("378.675 đ", "VND 378,675.00"),
    ("VND -20,000", "VND -20,000.00"),
    ("(VND 10,000)", "VND -10,000.00"),
    ("VND (10,000)", "VND -10,000.00"),
    ("USD 1,234.50", "USD 1,234.50"),
    ("EUR 1.234,56", "EUR 1,234.56"),
    ("VND 0", "VND 0.00"),
    ("USD 1.2345", "USD 1.2345"),
    ("", ""),
    ("Chưa rõ", "Chưa rõ"),
    ("378675", "378675"),
    ("VND not available", "VND not available"),
])
def test_revenue_uses_consistent_currency_grouping_without_changing_value(value, expected):
    assert format_revenue(value) == expected


def test_history_formats_legacy_amounts_in_place_without_changing_state_or_excel():
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.history_tree, app.history_date_var, app.state = Mock(), Mock(), Mock()
    app.history_tree.get_children.return_value = []
    sources = ["Agoda", "Traveloka", "Trip", "Expedia", "Trip", "Agoda"]
    bookings = [BookingEvent(source=source, booking_id=f"TEST-{index}", guest_name="Synthetic Guest",
                             checkin_date=date.today(), total_revenue=value)
                for index, (source, (value, _)) in enumerate(zip(sources, SCREENSHOT_AMOUNTS, strict=True))]
    records = [booking.to_dict() for booking in bookings]
    original = [dict(record) for record in records]
    excel = [excel_tsv(booking) for booking in bookings]
    app.state.history.return_value = records
    app.refresh_history()
    assert [call.kwargs["values"][-1] for call in app.history_tree.insert.call_args_list] == [
        expected for _, expected in SCREENSHOT_AMOUNTS]
    assert records == original
    assert list(app.history_rows.values()) == original
    assert [excel_tsv(BookingEvent.from_dict(record)) for record in records] == excel

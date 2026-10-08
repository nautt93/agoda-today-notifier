from __future__ import annotations

import queue
from datetime import date
from unittest.mock import Mock

from app import BookingNotifierApp
from booking_notifier.booking_com_browser import BookingComWorker
from booking_notifier.config import ConfigStore
from booking_notifier.models import BookingEvent
from booking_notifier.popup_state import booking_com_popup_state
from booking_notifier.state import StateStore


def _event() -> BookingEvent:
    return BookingEvent(
        source="Booking.com", booking_id="5550000001", checkin_date=date.today(),
        checkout_date=None, details_url=(
            "https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/booking.html"
            "?res_id=5550000001&hotel_id=12345&lang=vi"
        ),
    )


def test_new_profiles_default_to_manual_booking_mode(tmp_path):
    config = ConfigStore(tmp_path / "config.json").load()
    assert config["booking_com_enrichment_mode"] == "manual"


def test_manual_mode_does_not_start_or_touch_a_browser(tmp_path):
    state = StateStore(tmp_path / "state.json")
    event = _event()
    state.register_today_confirmation(event, ("synthetic-mail",), date.today())
    factory = Mock()
    worker = BookingComWorker(
        queue.Queue(), state, {"booking_com_enrichment": True, "booking_com_enrichment_mode": "manual"},
        tmp_path, factory,
    )
    worker.refresh()
    factory.assert_not_called()


def test_manual_popup_explains_visible_browser_and_form():
    state = booking_com_popup_state(_event(), True, "", "manual")
    assert state.phase == "manual" and not state.ready
    assert "Không chạy web ẩn" in state.message
    assert "Nhập chi tiết" in state.message


def test_manual_checkout_is_strict_and_accepts_common_hotel_formats():
    checkin = date(2026, 10, 8)
    assert BookingNotifierApp._manual_checkout("10/10/2026", checkin) == date(2026, 10, 10)
    assert BookingNotifierApp._manual_checkout("2026-10-10", checkin) == date(2026, 10, 10)
    try:
        BookingNotifierApp._manual_checkout("07/10/2026", checkin)
    except ValueError as exc:
        assert "sau ngày nhận" in str(exc)
    else:
        raise AssertionError("Checkout before check-in must be rejected")

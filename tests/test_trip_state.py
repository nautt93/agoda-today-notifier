from dataclasses import replace
from datetime import date, timedelta

import pytest

from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore


@pytest.mark.parametrize("source", ["Agoda", "Expedia", "Traveloka", "Trip"])
def test_original_reminder_priority_is_trip_only(tmp_path, source):
    today = date(2026, 10, 5)
    original = BookingEvent(source=source, booking_id="9000000000000001", checkin_date=today,
                            guest_name="Example Full Guest", room_type="Superior Breakfast x1",
                            total_revenue="VND 800,000", subject="New booking received")
    reminder = replace(original, guest_name="Example", room_type="Superior x1",
                       total_revenue="VND 1,000,000", subject="[Reminder] New Reservation")
    state = StateStore(tmp_path / "state.json")
    assert state.register_today_confirmation(original, ("original",), today)
    assert state.register_today_confirmation(reminder, ("reminder",), today) is None
    stored = state.pending_for_date(today)[0]
    assert stored.to_dict() == replace(original if source == "Trip" else reminder, status="active").to_dict()


def test_trip_reminder_still_fills_missing_original_details(tmp_path):
    today = date(2026, 10, 5)
    original = BookingEvent(source="Trip", booking_id="9000000000000001", checkin_date=today,
                            subject="Urgent action required - new booking received")
    reminder = replace(original, guest_name="Example Full Guest", room_type="Superior x1",
                       total_revenue="VND 1,000,000", subject="[Reminder] Trip.com New Reservation")
    state = StateStore(tmp_path / "state.json")
    state.register_today_confirmation(original, ("original",), today)
    assert state.register_today_confirmation(reminder, ("reminder",), today) is None
    stored = state.pending_for_date(today)[0]
    assert stored.subject == original.subject
    for field in ("guest_name", "room_type", "total_revenue"):
        assert getattr(stored, field) == getattr(reminder, field)


def test_trip_original_priority_never_crosses_stay_date(tmp_path):
    today = date(2026, 10, 5)
    previous = BookingEvent(source="Trip", booking_id="9000000000000001", checkin_date=today - timedelta(days=1),
                            room_type="Old room plan x1", total_revenue="VND 800,000",
                            subject="Urgent action required - new booking received")
    incoming = replace(previous, checkin_date=today, room_type="New room plan x2", total_revenue="VND 1,000,000",
                       subject="[Reminder] Trip.com New Reservation")
    state = StateStore(tmp_path / "state.json")
    state.apply_event(previous, ("previous",))
    alert = state.register_today_confirmation(incoming, ("incoming",), today)
    assert alert and alert.to_dict() == replace(incoming, status="active").to_dict()


def test_trip_unknown_origin_does_not_claim_original_priority(tmp_path):
    today = date(2026, 10, 5)
    legacy = BookingEvent(source="Trip", booking_id="9000000000000001", checkin_date=today,
                          room_type="Incomplete old room", total_revenue="VND 800,000")
    incoming = replace(legacy, room_type="Confirmed room x1", total_revenue="VND 1,000,000",
                       subject="[Reminder] Trip.com New Reservation")
    state = StateStore(tmp_path / "state.json")
    state.register_today_confirmation(legacy, ("legacy",), today)
    assert state.register_today_confirmation(incoming, ("incoming",), today) is None
    assert state.pending_for_date(today)[0].to_dict() == replace(incoming, status="active").to_dict()

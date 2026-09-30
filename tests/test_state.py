from __future__ import annotations

from datetime import date

from booking_notifier.models import (
    BOOKING_STATUS_CANCELLED,
    BOOKING_STATUS_MODIFIED,
    BookingEvent,
)
from booking_notifier.state import StateStore


def booking(checkin: date, status: str = "new") -> BookingEvent:
    return BookingEvent(
        source="Expedia",
        booking_id="123456789",
        status=status,
        checkin_date=checkin,
        guest_name="Future Guest",
        room_type="Deluxe",
    )


def test_future_booking_is_queued_on_arrival_day(tmp_path):
    state = StateStore(tmp_path / "state.json")
    state.apply_event(booking(date(2026, 10, 1)), ("uid:1", "msg:1"))
    assert state.queue_due_alerts(date(2026, 9, 30)) == []
    due = state.queue_due_alerts(date(2026, 10, 1))
    assert [item.booking_id for item in due] == ["123456789"]
    assert state.queue_due_alerts(date(2026, 10, 1)) == []


def test_cancellation_removes_quiet_hour_pending_alert(tmp_path):
    state = StateStore(tmp_path / "state.json")
    state.apply_event(booking(date(2026, 10, 1)), ("uid:1", "msg:1"))
    assert len(state.queue_due_alerts(date(2026, 10, 1))) == 1
    state.apply_event(booking(date(2026, 10, 1), BOOKING_STATUS_CANCELLED), ("uid:2", "msg:2"))
    assert state.pending_for_date(date(2026, 10, 1)) == []
    assert state.queue_due_alerts(date(2026, 10, 1)) == []


def test_modification_moves_booking_to_new_date(tmp_path):
    state = StateStore(tmp_path / "state.json")
    state.apply_event(booking(date(2026, 10, 1)), ("uid:1", "msg:1"))
    assert len(state.queue_due_alerts(date(2026, 10, 1))) == 1
    affected = state.apply_event(booking(date(2026, 10, 2), BOOKING_STATUS_MODIFIED), ("uid:2", "msg:2"))
    assert affected == {date(2026, 10, 1), date(2026, 10, 2)}
    assert state.pending_for_date(date(2026, 10, 1)) == []
    assert len(state.queue_due_alerts(date(2026, 10, 2))) == 1


def test_cancellation_without_date_reports_original_checkin(tmp_path):
    state = StateStore(tmp_path / "state.json")
    state.apply_event(booking(date(2026, 10, 1)), ("uid:1", "msg:1"))
    cancelled = BookingEvent(
        source="Expedia",
        booking_id="123456789",
        status=BOOKING_STATUS_CANCELLED,
    )
    affected = state.apply_event(cancelled, ("uid:2", "msg:2"))
    assert affected == {date(2026, 10, 1)}
    assert state.pending_for_date(date(2026, 10, 1)) == []


def test_acknowledged_booking_alerts_only_once(tmp_path):
    state = StateStore(tmp_path / "state.json")
    event = booking(date(2026, 10, 1))
    state.apply_event(event, ("uid:1", "msg:1"))
    due = state.queue_due_alerts(date(2026, 10, 1))[0]
    state.acknowledge(due)
    assert state.queue_due_alerts(date(2026, 10, 1)) == []
    assert len(state.history()) == 1


def test_event_and_processed_keys_are_persisted_together(tmp_path):
    path = tmp_path / "state.json"
    state = StateStore(path)
    state.apply_event(booking(date(2026, 10, 1)), ("uid:1", "msg:1"))
    reloaded = StateStore(path)
    assert reloaded.is_processed("uid:1", "missing")
    assert reloaded.is_processed("msg:1")
    assert len(reloaded.active_bookings()) == 1

from __future__ import annotations

import hashlib
from datetime import date

import pytest

from booking_notifier.models import BookingEvent, booking_storage_id


@pytest.mark.parametrize("record", [{}, {"source": "Agoda"}, {"source": None, "booking_id": None}])
def test_legacy_missing_id_and_source_have_safe_defaults(record):
    event = BookingEvent.from_dict(record)
    assert event.source == "Agoda" and event.booking_id == ""
    assert event.storage_id == booking_storage_id(record)


@pytest.mark.parametrize("source", ["Agoda", "Expedia"])
def test_missing_id_identity_is_stable_and_provider_specific(source):
    record = {"source": source, "subject": "Confirmed reservation", "sender": "booking@example.invalid",
              "received_at": "2026-10-04T09:38", "checkin_date": "2026-10-04"}
    event = BookingEvent.from_dict(record)
    raw = "Confirmed reservation|2026-10-04T09:38|2026-10-04|booking@example.invalid"
    expected = f"{source.lower()}:alert:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"
    assert event.storage_id == expected == booking_storage_id(record)
    assert BookingEvent.from_dict(event.to_dict()).storage_id == expected
    assert booking_storage_id({**record, "booking_id": None}) == expected
    assert booking_storage_id({**record, "subject": "Other confirmation"}) != expected


def test_numeric_booking_id_and_legacy_lowercase_provider():
    event = BookingEvent.from_dict({"source": "expedia", "booking_id": 1234567890})
    assert event.source == "Expedia" and event.booking_id == "1234567890"
    assert event.storage_id == "expedia:1234567890"


def test_identity_lookup_does_not_parse_unrelated_old_dates():
    record = {"source": "Agoda", "subject": "Old history", "checkin_date": "30/09/2026"}
    assert booking_storage_id(record).startswith("agoda:alert:")
    with pytest.raises(ValueError):
        BookingEvent.from_dict(record)  # Invalid dates cannot create a notification for today.
    assert BookingEvent(source="Agoda", checkin_date=date(2026, 10, 4)).booking_id == ""

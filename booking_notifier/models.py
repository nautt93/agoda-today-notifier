from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

BOOKING_STATUS_NEW = "new"
BOOKING_STATUS_MODIFIED = "modified"
BOOKING_STATUS_CANCELLED = "cancelled"
BOOKING_SOURCES = ("Agoda", "Expedia", "Traveloka", "Trip", "Booking.com")


def booking_storage_id(record: dict[str, Any]) -> str:
    """Read identity without deserializing dates or requiring optional legacy fields.

    Preserve the existing hash for ID-less Agoda confirmations. An unrelated old
    history row must never prevent a new email from being committed.
    """
    source = str(record.get("source") or "Agoda").lower()
    booking_id = str(record.get("booking_id") or "").upper()
    if booking_id:
        return f"{source}:{booking_id}"
    raw = "|".join(str(record.get(field) or "")
                   for field in ("subject", "received_at", "checkin_date", "sender"))
    return f"{source}:alert:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


@dataclass(slots=True)
class BookingEvent:
    source: str
    booking_id: str = ""
    status: str = BOOKING_STATUS_NEW
    checkin_date: date | None = None
    checkout_date: date | None = None
    guest_name: str = ""
    room_type: str = ""
    total_revenue: str = ""
    subject: str = ""
    sender: str = ""
    received_at: str = ""
    details_url: str = ""
    details_loaded_at: str = ""

    @property
    def nights(self) -> int | None:
        if not self.checkin_date or not self.checkout_date:
            return None
        return max(0, (self.checkout_date - self.checkin_date).days)

    @property
    def storage_id(self) -> str:
        # 1.5.5 also alerts on Agoda confirmations whose booking ID is not readable.
        return booking_storage_id({
            "source": self.source, "booking_id": self.booking_id, "subject": self.subject,
            "received_at": self.received_at, "sender": self.sender,
            "checkin_date": self.checkin_date.isoformat() if self.checkin_date else "",
        })

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["checkin_date"] = self.checkin_date.isoformat() if self.checkin_date else ""
        value["checkout_date"] = self.checkout_date.isoformat() if self.checkout_date else ""
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BookingEvent:
        data = dict(value)
        # 1.5.x records may omit the source/ID; JSON exports may have numeric IDs.
        source = str(data.get("source") or "Agoda")
        data["source"] = {provider.lower(): provider for provider in BOOKING_SOURCES}.get(source.lower(), source)
        data["booking_id"] = str(data.get("booking_id") or "")
        for field in ("checkin_date", "checkout_date"):
            raw = data.get(field)
            data[field] = date.fromisoformat(raw) if raw else None
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: data[key] for key in allowed if key in data})

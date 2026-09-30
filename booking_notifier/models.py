from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

BOOKING_STATUS_NEW = "new"
BOOKING_STATUS_MODIFIED = "modified"
BOOKING_STATUS_CANCELLED = "cancelled"


@dataclass(slots=True)
class BookingEvent:
    source: str
    booking_id: str
    status: str = BOOKING_STATUS_NEW
    checkin_date: date | None = None
    checkout_date: date | None = None
    guest_name: str = ""
    room_type: str = ""
    total_revenue: str = ""
    subject: str = ""
    sender: str = ""
    received_at: str = ""

    @property
    def nights(self) -> int | None:
        if not self.checkin_date or not self.checkout_date:
            return None
        return max(0, (self.checkout_date - self.checkin_date).days)

    @property
    def storage_id(self) -> str:
        if self.booking_id:
            return f"{self.source.lower()}:{self.booking_id.upper()}"
        # 1.5.5 also alerts on Agoda confirmations whose booking ID is not readable.
        raw = "|".join((self.subject, self.received_at,
                        self.checkin_date.isoformat() if self.checkin_date else "", self.sender))
        return f"{self.source.lower()}:alert:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["checkin_date"] = self.checkin_date.isoformat() if self.checkin_date else ""
        value["checkout_date"] = self.checkout_date.isoformat() if self.checkout_date else ""
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BookingEvent:
        data = dict(value)
        for field in ("checkin_date", "checkout_date"):
            raw = data.get(field)
            data[field] = date.fromisoformat(raw) if raw else None
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: data[key] for key in allowed if key in data})

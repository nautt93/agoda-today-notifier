from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import STATE_PATH, atomic_json_write
from .models import BOOKING_STATUS_CANCELLED, BookingEvent

STATE_SCHEMA = 4
PARSER_STATE_VERSION = "p6"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class StateStore:
    """Atomic booking state, scheduling, cancellation, and deduplication."""

    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        value: dict[str, Any] = {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                value = loaded
        except (OSError, ValueError):
            pass
        value.setdefault("schema", STATE_SCHEMA)
        value.setdefault("processed_keys", [])
        value.setdefault("bookings", {})
        value.setdefault("pending_alerts", [])
        value.setdefault("history", [])
        value.setdefault("parse_failures", {})
        # Keep v1.5 history/pending records readable; future scheduling uses bookings.
        if not isinstance(value["bookings"], dict):
            value["bookings"] = {}
        if not isinstance(value["processed_keys"], list):
            value["processed_keys"] = []
        value["schema"] = STATE_SCHEMA
        return value

    def _save_locked(self) -> None:
        atomic_json_write(self.path, self.data)

    def is_processed(self, *keys: str) -> bool:
        with self.lock:
            existing = set(self.data["processed_keys"])
            return any(key and key in existing for key in keys)

    def record_parse_failure(self, message_key: str, reason: str) -> int:
        with self.lock:
            failures = self.data["parse_failures"]
            record = failures.setdefault(message_key, {"attempts": 0, "reason": "", "updated_at": ""})
            record["attempts"] = int(record.get("attempts", 0)) + 1
            record["reason"] = reason[:300]
            record["updated_at"] = _now()
            # Bound diagnostic state without falsely marking the message processed.
            if len(failures) > 500:
                oldest = sorted(failures, key=lambda key: failures[key].get("updated_at", ""))[:100]
                for key in oldest:
                    failures.pop(key, None)
            self._save_locked()
            return int(record["attempts"])

    def apply_event(self, event: BookingEvent, processed_keys: Iterable[str]) -> set[date]:
        """Apply the event atomically and return every check-in date it affects."""
        with self.lock:
            storage_id = event.storage_id
            bookings = self.data["bookings"]
            pending = self.data["pending_alerts"]
            existing = dict(bookings.get(storage_id, {}))
            affected_dates: set[date] = set()
            old_checkin = self._record_checkin_date(existing)
            if old_checkin:
                affected_dates.add(old_checkin)
            if event.status == BOOKING_STATUS_CANCELLED:
                incoming = event.to_dict()
                for field, value in incoming.items():
                    if value not in (None, ""):
                        existing[field] = value
                existing.update({
                    "source": event.source,
                    "booking_id": event.booking_id,
                    "status": BOOKING_STATUS_CANCELLED,
                    "cancelled_at": _now(),
                    "updated_at": _now(),
                    "subject": event.subject,
                })
                bookings[storage_id] = existing
                self.data["pending_alerts"] = [
                    record for record in pending
                    if self._record_storage_id(record) != storage_id
                ]
            else:
                old_checkin_text = str(existing.get("checkin_date", ""))
                incoming = event.to_dict()
                for field, value in incoming.items():
                    if value not in (None, ""):
                        existing[field] = value
                existing["status"] = "active"
                existing["updated_at"] = _now()
                new_checkin = str(existing.get("checkin_date", ""))
                if old_checkin_text and new_checkin and old_checkin_text != new_checkin:
                    existing["alerted_for"] = ""
                    self.data["pending_alerts"] = [
                        record for record in pending
                        if self._record_storage_id(record) != storage_id
                    ]
                else:
                    for record in self.data["pending_alerts"]:
                        if self._record_storage_id(record) != storage_id:
                            continue
                        for field, field_value in existing.items():
                            if field_value not in (None, ""):
                                record[field] = field_value
                bookings[storage_id] = existing
            self._repair_booking_details_locked(existing, storage_id)
            new_checkin_date = self._record_checkin_date(existing)
            if new_checkin_date:
                affected_dates.add(new_checkin_date)
            existing_keys = list(self.data["processed_keys"])
            existing_set = set(existing_keys)
            for key in processed_keys:
                if key and key not in existing_set:
                    existing_keys.append(key)
                    existing_set.add(key)
            self.data["processed_keys"] = existing_keys[-20000:]
            for key in processed_keys:
                self.data["parse_failures"].pop(key, None)
            self._save_locked()
            return affected_dates

    def _repair_booking_details_locked(self, booking: dict[str, Any], storage_id: str) -> None:
        """Enrich previously shown records after a parser upgrade without alerting twice."""
        historical_checkins: set[str] = set()
        for record in self.data["history"]:
            if self._record_storage_id(record) != storage_id:
                continue
            historical_checkins.add(str(record.get("checkin_date", "")))
            for field in ("guest_name", "room_type"):
                incoming = str(booking.get(field, "")).strip()
                current = str(record.get(field, "")).strip()
                if incoming and (not current or len(incoming) > len(current)):
                    record[field] = incoming
            for field in ("total_revenue", "checkout_date", "subject", "sender", "received_at"):
                if not record.get(field) and booking.get(field):
                    record[field] = booking[field]

        checkin = str(booking.get("checkin_date", ""))
        if checkin and checkin in historical_checkins:
            booking["alerted_for"] = checkin

        for record in self.data["pending_alerts"]:
            if self._record_storage_id(record) != storage_id:
                continue
            for field in ("guest_name", "room_type", "total_revenue", "checkout_date", "subject", "sender", "received_at"):
                if booking.get(field):
                    record[field] = booking[field]

    def remember_processed_aliases(self, *keys: str) -> None:
        """Remember extra UID aliases for a Message-ID that was already committed."""
        with self.lock:
            existing = list(self.data["processed_keys"])
            known = set(existing)
            changed = False
            for key in keys:
                if key and key not in known:
                    existing.append(key)
                    known.add(key)
                    changed = True
            if changed:
                self.data["processed_keys"] = existing[-20000:]
                self._save_locked()

    def queue_due_alerts(self, today: date) -> list[BookingEvent]:
        due: list[BookingEvent] = []
        today_text = today.isoformat()
        with self.lock:
            pending_ids = {self._record_storage_id(record) for record in self.data["pending_alerts"]}
            for storage_id, record in self.data["bookings"].items():
                if record.get("status") != "active":
                    continue
                if record.get("checkin_date") != today_text:
                    continue
                if record.get("alerted_for") == today_text or storage_id in pending_ids:
                    continue
                queued = dict(record)
                queued["pending_key"] = f"{storage_id}:{today_text}"
                queued["queued_at"] = _now()
                self.data["pending_alerts"].append(queued)
                pending_ids.add(storage_id)
                try:
                    due.append(BookingEvent.from_dict(queued))
                except (TypeError, ValueError):
                    continue
            if due:
                self._save_locked()
        return due

    def pending_for_date(self, today: date) -> list[BookingEvent]:
        result: list[BookingEvent] = []
        with self.lock:
            for record in self.data["pending_alerts"]:
                if record.get("checkin_date") != today.isoformat():
                    continue
                storage_id = self._record_storage_id(record)
                current = self.data["bookings"].get(storage_id, {})
                if current.get("status") == BOOKING_STATUS_CANCELLED:
                    continue
                try:
                    result.append(BookingEvent.from_dict(record))
                except (TypeError, ValueError):
                    continue
        return result

    def acknowledge(self, event: BookingEvent) -> None:
        with self.lock:
            today_text = event.checkin_date.isoformat() if event.checkin_date else ""
            storage_id = event.storage_id
            self.data["pending_alerts"] = [
                record for record in self.data["pending_alerts"]
                if self._record_storage_id(record) != storage_id
            ]
            booking = self.data["bookings"].get(storage_id)
            if booking is not None:
                booking["alerted_for"] = today_text
                booking["alerted_at"] = _now()
            history_record = event.to_dict()
            history_record["acknowledged_at"] = _now()
            history_record["pending_key"] = f"{storage_id}:{today_text}"
            self.data["history"].append(history_record)
            self.data["history"] = self.data["history"][-2000:]
            self._save_locked()

    def history(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(record) for record in reversed(self.data["history"])]

    def active_bookings(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(record) for record in self.data["bookings"].values() if record.get("status") == "active"]

    @staticmethod
    def _record_storage_id(record: dict[str, Any]) -> str:
        source = str(record.get("source", "Agoda")).lower()
        booking_id = str(record.get("booking_id", "")).upper()
        return f"{source}:{booking_id}"

    @staticmethod
    def _record_checkin_date(record: dict[str, Any]) -> date | None:
        try:
            raw = str(record.get("checkin_date", ""))
            return date.fromisoformat(raw) if raw else None
        except ValueError:
            return None

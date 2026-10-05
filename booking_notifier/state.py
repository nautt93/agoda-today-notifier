from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import STATE_PATH, atomic_json_write
from .models import BOOKING_SOURCES, BOOKING_STATUS_CANCELLED, BOOKING_STATUS_NEW, BookingEvent, booking_storage_id

STATE_SCHEMA = 5
PARSER_STATE_VERSION = "p13"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _trip_original_has_priority(original: dict[str, Any], reminder: dict[str, Any]) -> bool:
    """Do not downgrade a Trip original's payout/details with a reminder summary."""
    original_subject = str(original.get("subject", "")).strip().lower()
    reminder_subject = str(reminder.get("subject", "")).strip().lower()
    return (
        str(original.get("source", "")).lower() == "trip"
        and str(reminder.get("source", "")).lower() == "trip"
        and bool(original.get("booking_id"))
        and original.get("booking_id") == reminder.get("booking_id")
        and bool(original.get("checkin_date"))
        and original.get("checkin_date") == reminder.get("checkin_date")
        and bool(original_subject)
        and not original_subject.startswith("[reminder]")
        and reminder_subject.startswith("[reminder]")
    )


class StateStore:
    """Persist today's pending notifications, legacy history, and deduplication."""

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
        value.setdefault("mailbox_reads", {})
        if not isinstance(value["mailbox_reads"], dict):
            value["mailbox_reads"] = {}
        # Keep v1.5 history/pending and v1.6/1.7 saved records readable.
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

    def mailbox_read_position(self, mailbox_key: str) -> tuple[int, list[int]] | None:
        with self.lock:
            record = self.data["mailbox_reads"].get(mailbox_key)
            if record is None:
                return None
            return int(record["cursor"]), list(record["pending_uids"])

    def mailbox_parser_version(self, mailbox_key: str) -> str:
        with self.lock:
            return str(self.data["mailbox_reads"].get(mailbox_key, {}).get("parser_version", ""))

    def stage_mailbox_reads(
        self, mailbox_key: str, uids: list[int], *, minimum_cursor: int = 0, parser_version: str = "",
    ) -> list[int]:
        """Save the entire batch before advancing the cursor or fetching any body.

        A crash, stopped monitor, or failed parse leaves unfinished UIDs recoverable.
        """
        with self.lock:
            record = self.data["mailbox_reads"].setdefault(mailbox_key, {"cursor": 0, "pending_uids": []})
            pending = set(record["pending_uids"]) | set(uids)
            record["cursor"] = max(int(record["cursor"]), minimum_cursor, max(uids, default=0))
            record["pending_uids"] = sorted(pending)
            if parser_version:
                record["parser_version"] = parser_version
            self._save_locked()
            return sorted(pending, reverse=True)

    def finish_mailbox_read(self, mailbox_key: str, uid: int) -> None:
        with self.lock:
            record = self.data["mailbox_reads"][mailbox_key]
            if uid in record["pending_uids"]:
                record["pending_uids"].remove(uid)
                self._save_locked()

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

    def apply_event(
        self, event: BookingEvent, processed_keys: Iterable[str], queue_for_date: date | None = None,
    ) -> set[date]:
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
                if _trip_original_has_priority(existing, incoming):
                    # The original has an explicit net payout and richer room
                    # plan; the reminder may only contain the guest's total.
                    # Fill missing details but retain the original provenance.
                    for field in ("guest_name", "room_type", "total_revenue", "checkout_date",
                                  "subject", "sender", "received_at"):
                        if existing.get(field):
                            incoming[field] = existing[field]
                for field, value in incoming.items():
                    if value not in (None, ""):
                        existing[field] = value
                # An unreadable Agoda ID is valid. Persist its explicit empty ID
                # instead of relying on a previously omitted dictionary field.
                existing["source"] = event.source
                existing["booking_id"] = event.booking_id
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
            if queue_for_date is not None:
                self._queue_booking_locked(existing, storage_id, queue_for_date)
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

    def register_today_confirmation(
        self, event: BookingEvent, processed_keys: Iterable[str], today: date,
    ) -> BookingEvent | None:
        """Commit a today's confirmation and its pending popup in the same write."""
        if event.status != BOOKING_STATUS_NEW or event.checkin_date != today:
            raise ValueError("Chỉ nhận xác nhận booking check-in hôm nay.")
        with self.lock:
            already_pending = any(self._record_storage_id(record) == event.storage_id
                                  for record in self.data["pending_alerts"])
            self.apply_event(event, processed_keys, queue_for_date=today)
            if not already_pending:
                for record in self.data["pending_alerts"]:
                    if self._record_storage_id(record) == event.storage_id:
                        return BookingEvent.from_dict(record)
            return None

    def _queue_booking_locked(self, record: dict[str, Any], storage_id: str, today: date) -> BookingEvent | None:
        if record.get("status") != "active" or record.get("checkin_date") != today.isoformat():
            return None
        if record.get("alerted_for") == today.isoformat() or any(
            self._record_storage_id(item) == storage_id for item in self.data["pending_alerts"]
        ):
            return None
        queued = dict(record)
        queued["pending_key"] = f"{storage_id}:{today.isoformat()}"
        queued["queued_at"] = _now()
        alert = BookingEvent.from_dict(queued)
        self.data["pending_alerts"].append(queued)
        return alert

    def _repair_booking_details_locked(self, booking: dict[str, Any], storage_id: str) -> None:
        """Enrich previously shown records after a parser upgrade without alerting twice."""
        historical_checkins: set[str] = set()
        for record in self.data["history"]:
            if self._record_storage_id(record) != storage_id:
                continue
            historical_checkins.add(str(record.get("checkin_date", "")))
            prefer_original = _trip_original_has_priority(booking, record)
            for field in ("guest_name", "room_type"):
                incoming = str(booking.get(field, "")).strip()
                current = str(record.get(field, "")).strip()
                if incoming and (prefer_original or not current or len(incoming) > len(current)):
                    record[field] = incoming
            for field in ("total_revenue", "checkout_date", "subject", "sender", "received_at"):
                if (prefer_original or not record.get(field)) and booking.get(field):
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

    def repair_known_confirmation(self, event: BookingEvent) -> bool:
        """Enrich an already saved confirmation, even after its check-in day.

        This never creates a booking/popup or applies lifecycle changes. Unseen
        confirmations for other days remain ignored by the new-mail reader.
        """
        if event.status != BOOKING_STATUS_NEW or event.checkin_date is None:
            return False
        with self.lock:
            records = [self.data["bookings"].get(event.storage_id, {})]
            records.extend(self.data["history"])
            records.extend(self.data["pending_alerts"])
            changed = False
            for record in records:
                if (not record or record.get("checkin_date") != event.checkin_date.isoformat()
                        or self._record_storage_id(record) != event.storage_id):
                    continue
                for field in ("guest_name", "room_type"):
                    incoming = str(getattr(event, field)).strip()
                    current = str(record.get(field, "")).strip()
                    if incoming and (not current or len(incoming) > len(current)):
                        record[field] = incoming
                        changed = True
            if changed:
                self._save_locked()
            return changed

    def incomplete_confirmations(self, today: date, limit: int = 20) -> list[dict[str, Any]]:
        """Return only known arrivals today needing name/room recovery, once per ID."""
        placeholders = {"", "—", "-", "unknown", "xem trong email"}
        with self.lock:
            records = list(self.data["bookings"].values()) + self.data["history"] + self.data["pending_alerts"]
            candidates: dict[str, dict[str, Any]] = {}
            for record in records:
                if (record.get("checkin_date") != today.isoformat()
                        or record.get("status") in {"cancelled", "modified"}
                        or not record.get("booking_id")
                        or str(record.get("source", "Agoda")).lower() not in {source.lower() for source in BOOKING_SOURCES}):
                    continue
                if any(str(record.get(field, "")).strip().lower() in placeholders for field in ("guest_name", "room_type")):
                    candidates[self._record_storage_id(record)] = dict(record)
            return list(candidates.values())[:max(0, limit)]

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

    def pending_for_date(self, today: date) -> list[BookingEvent]:
        result: list[BookingEvent] = []
        seen: set[str] = set()
        with self.lock:
            for record in self.data["pending_alerts"]:
                if record.get("checkin_date") != today.isoformat():
                    continue
                storage_id = self._record_storage_id(record)
                current = self.data["bookings"].get(storage_id, {})
                if (storage_id in seen or current.get("status") == BOOKING_STATUS_CANCELLED
                        or self._is_acknowledged_locked(storage_id, today.isoformat())):
                    continue
                try:
                    result.append(BookingEvent.from_dict(record))
                    seen.add(storage_id)
                except (TypeError, ValueError):
                    continue
        return result

    def is_acknowledged(self, event: BookingEvent) -> bool:
        """Recheck persisted acknowledgement when a delayed UI event arrives."""
        checkin = event.checkin_date.isoformat() if event.checkin_date else ""
        with self.lock:
            return self._is_acknowledged_locked(event.storage_id, checkin)

    def _is_acknowledged_locked(self, storage_id: str, checkin: str) -> bool:
        if not checkin:
            return False
        booking = self.data["bookings"].get(storage_id, {})
        if booking.get("alerted_for") == checkin:
            return True
        # Legacy profiles may retain history without the matching booking cache.
        return any(record.get("checkin_date") == checkin and self._record_storage_id(record) == storage_id
                   for record in self.data["history"])

    def acknowledge(self, event: BookingEvent) -> None:
        with self.lock:
            today_text = event.checkin_date.isoformat() if event.checkin_date else ""
            storage_id = event.storage_id
            already_acknowledged = self._is_acknowledged_locked(storage_id, today_text)
            self.data["pending_alerts"] = [
                record for record in self.data["pending_alerts"]
                if self._record_storage_id(record) != storage_id
            ]
            booking = self.data["bookings"].get(storage_id)
            if booking is not None and booking.get("alerted_for") != today_text:
                booking["alerted_for"] = today_text
                booking["alerted_at"] = _now()
            if not already_acknowledged:
                history_record = event.to_dict()
                history_record["acknowledged_at"] = _now()
                history_record["pending_key"] = f"{storage_id}:{today_text}"
                self.data["history"].append(history_record)
                self.data["history"] = self.data["history"][-2000:]
            if booking is not None:
                # A popup may predate a parser repair. Do not write its stale
                # short name/empty room back over the enriched saved details.
                self._repair_booking_details_locked(booking, storage_id)
            self._save_locked()

    def history(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(record) for record in reversed(self.data["history"])]

    def active_bookings(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(record) for record in self.data["bookings"].values() if record.get("status") == "active"]

    @staticmethod
    def _record_storage_id(record: dict[str, Any]) -> str:
        return booking_storage_id(record)

    @staticmethod
    def _record_checkin_date(record: dict[str, Any]) -> date | None:
        try:
            raw = str(record.get("checkin_date", ""))
            return date.fromisoformat(raw) if raw else None
        except ValueError:
            return None

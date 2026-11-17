"""Read Booking.com confirmations, including the Exely email relay."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from .models import BOOKING_STATUS_CANCELLED, BOOKING_STATUS_MODIFIED, BOOKING_STATUS_NEW, BookingEvent
from .parsing import (
    CHECKIN_LABELS,
    CHECKOUT_LABELS,
    MONEY_PATTERN,
    _date_by_labels,
    _parse_room_count,
    _row_value,
    _text_value,
    clean_guest_name,
    clean_room_type,
    extract_guest_name,
    extract_room_type,
    is_exely_booking_relay,
    message_body_text,
    message_table_rows,
    normalized,
    parse_date,
    sender_source,
)

EXELY_SUBJECT = re.compile(
    r"^(?:xac nhan dat phong|booking confirmation|reservation confirmation)\s+([0-9]{6,20})\s*\.\s*booking\.com\.?$",
)
EXELY_AGENCY_LABELS = ("Đại lý du lịch", "Travel agency", "Travel agent")
ROOM_SUMMARY = re.compile(r"^(?:số|no\.?|room(?:\s+no\.?)?|phòng)\s*(\d{1,3})\s*:\s*(.+)$", re.IGNORECASE)
ROOM_DETAIL = re.compile(r"^[№#]\s*(\d{1,3})\s*$")
TOTAL_LABELS = (
    "Amount payable to property", "Property payout", "Net payout",
    "Tổng chi phí", "Tổng tiền", "Total cost", "Total price", "Booking total", "Total amount", "Grand total",
)


def _field_value(body: str, rows: Sequence[Sequence[str]], labels: Sequence[str]) -> str:
    return _row_value(rows, labels) or _text_value(body, labels)


def _booking_rooms(body: str, rows: Sequence[Sequence[str]]) -> str:
    """Exely lists each numbered room twice, in the summary and room details."""
    slots: dict[int, str] = {}

    def add(index: str, value: str) -> None:
        if room := clean_room_type(value):
            slots.setdefault(int(index), room)

    lines = body.splitlines()
    for value in [*(row[0] for row in rows if row), *lines]:
        if match := ROOM_SUMMARY.fullmatch(value):
            add(*match.groups())
    for row in rows:
        if len(row) >= 2 and (match := ROOM_DETAIL.fullmatch(row[0])):
            add(match[1], row[1])
    if not rows:
        for label, value in zip(lines, lines[1:], strict=False):
            if match := ROOM_DETAIL.fullmatch(label):
                add(match[1], value)
    if slots:
        groups: dict[str, tuple[str, int]] = {}
        for index in sorted(slots):
            room = slots[index]
            key = normalized(room)
            name, count = groups.get(key, (room, 0))
            groups[key] = (name, count + 1)
        return "; ".join(f"{name} x{count}" for name, count in groups.values())
    room = extract_room_type(body, rows, "Booking.com")
    count = _parse_room_count(_field_value(body, rows, ("Phòng", "Số phòng")))
    return f"{room} x{count}" if room and count and not re.search(r"\bx\d+$", room) else room


def _booking_total(body: str, rows: Sequence[Sequence[str]]) -> str:
    # Never substitute agency commission or a per-night price for the total.
    for label in TOTAL_LABELS:
        candidate = _field_value(body, rows, (label,))
        value = re.sub(r"\s+", " ", candidate).strip()
        if MONEY_PATTERN.fullmatch(value):
            return value
    return ""


def _reservation_id_from_url(value: str) -> str:
    """Use an email link only to identify the reservation; never retain/open it."""
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname != "admin.booking.com" or url.username or url.password
                or url.port not in (None, 443)
                or url.path != "/hotel/hoteladmin/extranet_ng/manage/booking.html"):
            return ""
        reservations = parse_qs(url.query).get("res_id", [])
        return reservations[0] if len(reservations) == 1 and re.fullmatch(r"[0-9]{6,20}", reservations[0]) else ""
    except (TypeError, ValueError):
        return ""


class _ReservationLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.identifiers: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.identifiers.update(identifier for key, value in attrs
                                    if key == "href" and value and (identifier := _reservation_id_from_url(value)))


def _booking_com_subject_status(subject: str) -> str:
    title = normalized(subject)
    if re.search(r"\b(?:cancelled|canceled|cancellation|huy dat phong|dat phong.*huy)\b", title):
        return BOOKING_STATUS_CANCELLED
    if re.search(r"\b(?:modified|modification|amended|changed|updated|thay doi|chinh sua)\b", title):
        return BOOKING_STATUS_MODIFIED
    if any(term in title for term in ("dat phong moi", "new booking", "new reservation")):
        return BOOKING_STATUS_NEW
    if re.match(r"^(?:booking\.com\s*[-:–]?\s*)?(?:xac nhan dat phong|booking confirmation|reservation confirmation)\b", title):
        return BOOKING_STATUS_NEW
    return ""


def is_booking_com_nonbooking_notice(message: Message) -> bool:
    """Recognize explicit unrelated subjects, never discard unknown booking mail."""
    if len(message.get_all("From", [])) != 1 or sender_source(message.get("From", "")) != "Booking.com":
        return False
    subject = str(message.get("Subject", ""))
    if is_exely_booking_relay(message.get("From", "")):
        # This relay handles several agencies. Don't retry its other channels.
        return EXELY_SUBJECT.fullmatch(normalized(subject)) is None
    if _booking_com_subject_status(subject):
        return False
    title = normalized(subject)
    return bool(re.match(
        r"^(?:booking\.com\s*[-:–]?\s*)?"
        r"(?:newsletter|payment reminder|review requested|ban tin|nhac nho thanh toan|nhac thanh toan|yeu cau danh gia)\b",
        title,
    ))


def parse_booking_com_email(message: Message) -> BookingEvent | None:
    if len(message.get_all("From", [])) != 1 or sender_source(message.get("From", "")) != "Booking.com":
        return None
    subject = str(message.get("Subject", ""))
    status = _booking_com_subject_status(subject)
    if not status:
        return None
    body = unicodedata.normalize("NFC", message_body_text(message))
    rows = [[unicodedata.normalize("NFC", cell) for cell in row] for row in message_table_rows(message)]
    identifiers = set(re.findall(r"\(\s*([0-9]{6,20})(?:\s*[,)]|\s*$)", subject))
    exely = is_exely_booking_relay(message.get("From", ""))
    if exely:
        match = EXELY_SUBJECT.fullmatch(normalized(subject))
        agency = normalized(_field_value(body, rows, EXELY_AGENCY_LABELS))
        if not match or agency != "booking.com":
            return None
        identifiers.add(match[1])
    elif match := re.search(r"\b(?:xac nhan dat phong|booking confirmation|reservation confirmation)\s*[#: -]*([0-9]{6,20})\b",
                            normalized(subject)):
        identifiers.add(match[1])
    identifiers.update(re.findall(
        r"\b(?:reservation (?:number|id)|booking (?:number|id)|don dat phong so|ma (?:so )?dat phong|so xac nhan)"
        # Exely also puts its own hyphenated internal reservation ID in the
        # preheader. Do not read the date prefix of that ID as the OTA code.
        r"\s*[:#-]?\s*([0-9]{6,20})(?![\w-])",
        normalized(body),
    ))
    if not exely:
        for part in message.walk():
            if part.get_content_type() != "text/html" or part.get_content_disposition() == "attachment":
                continue
            try:
                parser = _ReservationLinks()
                parser.feed(str(part.get_content()))
                identifiers.update(parser.identifiers)
            except (LookupError, ValueError, AttributeError):
                continue
        identifiers.update(identifier for raw in re.findall(r"https://[^\s<>]+", body)
                           if (identifier := _reservation_id_from_url(raw)))
    if len(identifiers) != 1:
        return None
    checkin = None if exely else parse_date(subject, "Booking.com")
    checkin = checkin or _date_by_labels(body, rows, CHECKIN_LABELS, "Booking.com")
    if status == BOOKING_STATUS_NEW and checkin is None:
        return None
    return BookingEvent(
        source="Booking.com", booking_id=identifiers.pop(), status=status, checkin_date=checkin,
        checkout_date=_date_by_labels(body, rows, CHECKOUT_LABELS, "Booking.com"),
        guest_name=(clean_guest_name(_field_value(body, rows, ("Tên đầy đủ", "Full name")))
                    or extract_guest_name(body, rows)),
        room_type=_booking_rooms(body, rows), total_revenue=_booking_total(body, rows),
        subject=subject[:240], sender=str(message.get("From", ""))[:240], received_at=str(message.get("Date", ""))[:100],
    )

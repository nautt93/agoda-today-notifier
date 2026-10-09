"""Read Booking.com reservation codes and arrival dates directly from email."""
from __future__ import annotations

import re
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from .models import BOOKING_STATUS_CANCELLED, BOOKING_STATUS_MODIFIED, BOOKING_STATUS_NEW, BookingEvent
from .parsing import (
    CHECKIN_LABELS,
    _date_by_labels,
    message_body_text,
    message_table_rows,
    normalized,
    parse_date,
    sender_source,
)


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
    return ""


def is_booking_com_nonbooking_notice(message: Message) -> bool:
    """Recognize explicit unrelated subjects, never discard unknown booking mail."""
    if len(message.get_all("From", [])) != 1 or sender_source(message.get("From", "")) != "Booking.com":
        return False
    subject = str(message.get("Subject", ""))
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
    body = message_body_text(message)
    identifiers = set(re.findall(r"\(\s*([0-9]{6,20})(?:\s*[,)]|\s*$)", subject))
    identifiers.update(re.findall(
        r"(?:reservation number|booking number|ma so dat phong|so xac nhan)\s*[:#-]?\s*([0-9]{6,20})",
        normalized(body),
    ))
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
    checkin = parse_date(subject, "Booking.com") or _date_by_labels(
        body, message_table_rows(message), CHECKIN_LABELS, "Booking.com",
    )
    if status == BOOKING_STATUS_NEW and checkin is None:
        return None
    return BookingEvent(
        source="Booking.com", booking_id=identifiers.pop(), status=status, checkin_date=checkin,
        subject=subject, sender=str(message.get("From", "")), received_at=str(message.get("Date", "")),
    )

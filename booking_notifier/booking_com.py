"""Booking.com's short emails and read-only Extranet detail snapshots.

Only a canonical reservation link is persisted. Email tracking/login tokens,
browser sessions, guest contact details and payment data never enter state.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace
from datetime import datetime
from email.message import Message
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .excel_export import excel_amount_value
from .models import BOOKING_STATUS_CANCELLED, BOOKING_STATUS_MODIFIED, BOOKING_STATUS_NEW, BookingEvent
from .parsing import (
    CHECKIN_LABELS,
    CHECKOUT_LABELS,
    _date_by_labels,
    extract_guest_name,
    message_body_text,
    message_table_rows,
    normalized,
    parse_date,
    sender_source,
)

DETAIL_PATH = "/hotel/hoteladmin/extranet_ng/manage/booking.html"
ADMIN_HOME = "https://admin.booking.com/"


def canonical_details_url(value: str, booking_id: str = "") -> str:
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname != "admin.booking.com" or url.username or url.password
                or url.port not in (None, 443) or url.path != DETAIL_PATH):
            return ""
        query = parse_qs(url.query, keep_blank_values=True)
        reservation = query.get("res_id", [])
        hotel = query.get("hotel_id", [])
        if (len(reservation) != 1 or len(hotel) != 1 or not re.fullmatch(r"[0-9]{6,20}", reservation[0])
                or not re.fullmatch(r"[0-9]{1,15}", hotel[0]) or (booking_id and reservation[0] != booking_id)):
            return ""
        return urlunsplit(("https", "admin.booking.com", DETAIL_PATH,
                           urlencode({"res_id": reservation[0], "hotel_id": hotel[0], "lang": "vi"}), ""))
    except (TypeError, ValueError):
        return ""


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.links.extend(value for key, value in attrs if key == "href" and value)


def parse_booking_com_email(message: Message) -> BookingEvent | None:
    if len(message.get_all("From", [])) != 1 or sender_source(message.get("From", "")) != "Booking.com":
        return None
    subject = str(message.get("Subject", ""))
    title = normalized(subject)
    if re.search(r"\b(?:cancelled|canceled|cancellation|huy dat phong|dat phong.*huy)\b", title):
        status = BOOKING_STATUS_CANCELLED
    elif re.search(r"\b(?:modified|modification|amended|changed|updated|thay doi|chinh sua)\b", title):
        status = BOOKING_STATUS_MODIFIED
    elif any(term in title for term in ("dat phong moi", "new booking", "new reservation")):
        status = BOOKING_STATUS_NEW
    else:
        return None
    body = message_body_text(message)
    ids = set(re.findall(r"\(\s*([0-9]{6,20})(?:\s*[,)]|\s*$)", subject))
    ids.update(re.findall(r"(?:reservation number|booking number|ma so dat phong|so xac nhan)\s*[:#-]?\s*([0-9]{6,20})", normalized(body)))
    links: set[str] = set()
    for part in message.walk():
        if part.get_content_type() != "text/html" or part.get_content_disposition() == "attachment":
            continue
        try:
            parser = _Links()
            parser.feed(str(part.get_content()))
            links.update(safe for raw in parser.links if (safe := canonical_details_url(raw)))
        except (LookupError, ValueError, AttributeError):
            continue
    links.update(safe for raw in re.findall(r"https://[^\s<>]+", body) if (safe := canonical_details_url(raw)))
    ids.update(parse_qs(urlsplit(link).query)["res_id"][0] for link in links)
    if len(ids) != 1:
        return None
    booking_id = ids.pop()
    rows = message_table_rows(message)
    checkin = parse_date(subject, "Booking.com") or _date_by_labels(body, rows, CHECKIN_LABELS, "Booking.com")
    if status == BOOKING_STATUS_NEW and checkin is None:
        return None
    # Conflicting property links must not open an arbitrary hotel reservation.
    details_url = next(iter(links)) if len(links) == 1 else ""
    return BookingEvent(source="Booking.com", booking_id=booking_id, status=status, checkin_date=checkin,
                        checkout_date=_date_by_labels(body, rows, CHECKOUT_LABELS, "Booking.com"),
                        guest_name=extract_guest_name(body, rows), subject=subject,
                        sender=str(message.get("From", "")), received_at=str(message.get("Date", "")),
                        details_url=details_url)


class BookingComDetailError(ValueError):
    pass


# Selectors observed on the authenticated Extranet. Hidden print copies are
# intentionally excluded, as are messages, contact information and card fields.
DETAIL_SNAPSHOT_JS = """() => {
    const visible = e => e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
    const text = e => (e?.innerText || '').replace(/\\s+/g, ' ').trim();
    const label = e => text(e).normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').replace(/đ/g, 'd').toLowerCase().replace(/[:\\s]+$/, '');
    const allowed = new Set(['nhan phong', 'tra phong', 'tong so can', 'tong tien phong', 'ma so dat phong',
        'check-in', 'check-out', 'total units', 'total rooms', 'number of rooms', 'total room price',
        'total price', 'reservation number', 'booking number']);
    const main = document.querySelector('#main-content');
    const fields = [...(main?.querySelectorAll('.res-content__label') || [])]
        .filter(e => visible(e) && allowed.has(label(e))).map(e => [text(e), visible(e.nextElementSibling) ? text(e.nextElementSibling) : '']);
    const names = [...(main?.querySelectorAll('[data-test-id="reservation-overview-name"]') || [])].filter(visible).map(text);
    const rooms = [...(main?.querySelectorAll('.res-room-title__name') || [])].filter(visible).map(text);
    const u = new URL(location.href);
    return {fields, names, rooms, url: u.origin + u.pathname + '?res_id=' +
        encodeURIComponent(u.searchParams.get('res_id') || '') + '&hotel_id=' +
        encodeURIComponent(u.searchParams.get('hotel_id') || '')};
}"""


def parse_booking_com_details(snapshot: dict[str, Any], event: BookingEvent) -> BookingEvent:
    expected = canonical_details_url(event.details_url, event.booking_id)
    actual = canonical_details_url(str(snapshot.get("url", "")), event.booking_id)
    if event.source != "Booking.com" or event.status not in {BOOKING_STATUS_NEW, "active"} or not expected or actual != expected:
        raise BookingComDetailError("Trang chi tiết không khớp mã booking/chỗ nghỉ.")
    fields: dict[str, str] = {}
    for pair in snapshot.get("fields", []):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        key, value = normalized(str(pair[0])).strip(" :"), str(pair[1]).strip()
        if key in fields and fields[key] != value:
            raise BookingComDetailError("Trang có các trường chi tiết mâu thuẫn.")
        fields[key] = value

    def field(*labels: str) -> str:
        return next((fields[normalized(label)] for label in labels if fields.get(normalized(label))), "")

    identity = field("Mã số đặt phòng", "Reservation number", "Booking number")
    if identity != event.booking_id:
        raise BookingComDetailError("Chưa thấy đúng mã booking trên trang chi tiết.")
    checkin = parse_date(field("Nhận phòng", "Check-in"), "Booking.com")
    checkout = parse_date(field("Trả phòng", "Check-out"), "Booking.com")
    if checkin != event.checkin_date or not checkout or not checkin or checkout <= checkin:
        raise BookingComDetailError("Ngày lưu trú không khớp email booking.")
    names = {re.sub(r"\s+", " ", str(name)).strip() for name in snapshot.get("names", []) if str(name).strip()}
    unit_text = field("Tổng số căn", "Total units", "Total rooms", "Number of rooms")
    count_match = re.fullmatch(r"\s*([0-9]{1,3})\s*", unit_text)
    rooms = [re.sub(r"\s+", " ", str(room)).strip() for room in snapshot.get("rooms", []) if str(room).strip()]
    amount = field("Tổng tiền phòng", "Total room price", "Total price")
    if len(names) != 1 or not count_match or not rooms or not excel_amount_value(amount):
        raise BookingComDetailError("Chưa lấy đủ họ tên/hạng phòng/số phòng/tổng tiền; hãy mở chi tiết Booking.com.")
    units = int(count_match[1])
    if not 1 <= units <= 100:
        raise BookingComDetailError("Số phòng không hợp lệ.")
    counts = Counter(rooms)
    if len(rooms) == units:
        room_type = "; ".join(f"{room} x{count}" for room, count in counts.items())
    else:
        raise BookingComDetailError("Số hạng phòng không khớp tổng số căn; không tự đoán số lượng từng hạng.")
    return replace(event, status=BOOKING_STATUS_NEW, guest_name=names.pop(), room_type=room_type, checkout_date=checkout,
                   total_revenue=amount, details_url=expected, details_loaded_at=datetime.now().isoformat(timespec="seconds"))

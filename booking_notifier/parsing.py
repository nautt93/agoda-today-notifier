from __future__ import annotations

import html
import re
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from datetime import date
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from .models import (
    BOOKING_STATUS_CANCELLED,
    BOOKING_STATUS_MODIFIED,
    BOOKING_STATUS_NEW,
    BookingEvent,
)

TRUSTED_DOMAINS = {
    "Agoda": ("agoda.com", "agoda.net"),
    "Expedia": ("expedia.com", "expediagroup.com", "expediapartnercentral.com"),
}

CHECKIN_LABELS = (
    "check-in date", "check in date", "check-in", "check in", "arrival date",
    "arrival", "ngày nhận phòng", "ngày đến", "nhận phòng",
)
CHECKOUT_LABELS = (
    "check-out date", "check out date", "check-out", "check out", "departure date",
    "departure", "ngày trả phòng", "ngày đi", "trả phòng",
)
GUEST_LABELS = (
    "lead guest name", "primary traveler name", "lead traveler name",
    "traveler full name", "traveler name", "guest full name", "guest name",
    "customer full name", "customer name", "booking holder", "reservation holder",
    "tên khách hàng", "tên người nhận phòng",
)
ROOM_LABELS = (
    "room type name", "room category", "booked room", "unit type", "unit name",
    "accommodation type", "room type", "room(s)", "rooms", "hạng phòng", "loại phòng",
)
ROOM_COUNT_LABELS = (
    "number of rooms", "rooms booked", "room quantity", "quantity", "qty",
    "no of rooms", "no. of rooms", "số lượng phòng", "số phòng đặt",
)
CONFIRMATION_LABELS = (
    "expedia confirmation id", "expedia confirmation number", "room confirmation id",
    "confirmation id", "confirmation number",
)
PAYMENT_MODEL_LABELS = ("payment model", "payment type", "business model", "collect type")

EXPEDIA_COLLECT_REVENUE_LABELS = (
    "Amount to Charge Expedia Group",
    "Amount Expedia Group Will Pay You",
    "Amount Payable to Property",
    "Expedia Collect Payout",
    "Property Payout",
    "Payout Amount",
    "Remittance Amount",
    "Net Amount",
    "Net Rate",
)
PROPERTY_COLLECT_REVENUE_LABELS = (
    "Amount to Collect from Guest",
    "Amount to Collect",
    "Guest Pays Property",
    "Amount Due at Property",
    "Hotel Collect Amount",
    "Property Collect Amount",
    "Total Booking Amount",
    "Total Reservation Amount",
    "Total Reservation Value",
    "Grand Total",
    "Booking Total",
    "Total Amount",
)
AGODA_REVENUE_LABELS = (
    "Booked and Payable by Agoda",
    "Amount Payable to Property",
    "Base Rate to Property",
    "Net Rate (incl. taxes & fees)",
    "Reference Sell Rate (incl. taxes & fees)",
    "Reference Sell Rate",
    "Total Room Charges",
    "Total Room Charge",
    "Grand Total",
    "Total Price",
    "Total Amount",
    "Booking Total",
    "Tổng tiền phòng",
    "Tổng tiền",
)

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7,
    "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12,
    "december": 12,
}


def strip_accents(value: object) -> str:
    text = str(value or "").replace("đ", "d").replace("Đ", "D")
    return "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if unicodedata.category(char) != "Mn"
    )


def normalized(value: object) -> str:
    text = html.unescape(str(value or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", strip_accents(text)).strip().lower().rstrip(":")


class _TextParser(HTMLParser):
    BREAKS = {"br", "p", "div", "tr", "td", "th", "li", "table", "h1", "h2", "h3"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", html.unescape("".join(self.cell))).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if any(self.row):
                self.rows.append(self.row)
            self.row = None


def html_to_text(value: str) -> str:
    parser = _TextParser()
    try:
        parser.feed(value)
        parser.close()
        return parser.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", value)


def message_body_text(message: Message) -> str:
    parts: list[str] = []
    iterable = message.walk() if message.is_multipart() else [message]
    for part in iterable:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            value = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            value = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        text = str(value)
        parts.append(html_to_text(text) if content_type == "text/html" else text)
    return "\n\n".join(parts)


def message_table_rows(message: Message) -> list[list[str]]:
    rows: list[list[str]] = []
    iterable = message.walk() if message.is_multipart() else [message]
    for part in iterable:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() != "text/html":
            continue
        try:
            value = str(part.get_content())
        except Exception:
            raw = part.get_payload(decode=True) or b""
            value = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        parser = _TableParser()
        try:
            parser.feed(value)
            parser.close()
            rows.extend(parser.rows)
        except Exception:
            continue
    return rows


def sender_source(sender: str) -> str:
    address = parseaddr(sender)[1].lower().strip()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    for source, roots in TRUSTED_DOMAINS.items():
        if any(domain == root or domain.endswith("." + root) for root in roots):
            return source
    return ""


def _event_status(subject: str, body: str) -> str:
    subject_n = normalized(subject)
    searchable = normalized(subject + "\n" + body[:50000])
    cancellation_patterns = (
        r"\b(?:booking|reservation)(?: status)?\s*[:\-]?\s*(?:has been |was |is )?(?:cancelled|canceled)\b",
        r"\bnotification type\s*[:\-]?\s*(?:booking )?(?:cancelled|canceled|cancellation)\b",
        r"\b(?:cancelled|canceled)\s+(?:booking|reservation)\b",
        r"\b(?:booking|reservation)\s+(?:cancellation|cancelled|canceled)\b",
        r"\bstatus\s*[:\-]\s*(?:cancelled|canceled)\b",
        r"\bda huy\b",
    )
    if any(re.search(pattern, searchable) for pattern in cancellation_patterns):
        return BOOKING_STATUS_CANCELLED
    if re.search(r"\b(?:cancelled|canceled|cancellation)\b", subject_n) and "policy" not in subject_n:
        return BOOKING_STATUS_CANCELLED
    modification_patterns = (
        r"\b(?:booking|reservation)(?: status)?\s*[:\-]?\s*(?:has been |was |is )?(?:modified|updated|changed|amended)\b",
        r"\bnotification type\s*[:\-]?\s*(?:booking )?(?:modified|modification|amended)\b",
        r"\b(?:modified|updated|changed|amended)\s+(?:booking|reservation)\b",
        r"\b(?:booking|reservation)\s+(?:modified|updated|changed|amended)\b",
        r"\bstatus\s*[:\-]\s*(?:modified|updated|changed|amended)\b",
    )
    if any(re.search(pattern, searchable) for pattern in modification_patterns):
        return BOOKING_STATUS_MODIFIED
    return BOOKING_STATUS_NEW


def extract_booking_id(text: str, subject: str = "") -> str:
    combined = subject + "\n" + text
    patterns = (
        r"(?:Expedia\s*)?Itinerary\s*(?:ID|number|no\.?|#|confirmation)\s*[:#\-]?\s*((?=[A-Z0-9\-]*\d)[A-Z0-9][A-Z0-9\-]{4,24})\b",
        r"Expedia\s*(?:booking|reservation)\s*(?:ID|number|no\.?|reference|#)\s*[:#\-]?\s*((?=[A-Z0-9\-]*\d)[A-Z0-9][A-Z0-9\-]{4,24})\b",
        r"Agoda\s*(?:booking|reservation)\s*(?:ID|number|no\.?|#)\s*[:#\-]?\s*((?=[A-Z0-9\-]*\d)[A-Z0-9][A-Z0-9\-]{4,24})\b",
        r"(?:Booking|Reservation|Confirmation)\s*(?:ID|number|no\.?|reference|#)\s*[:#\-]?\s*((?=[A-Z0-9\-]*\d)[A-Z0-9][A-Z0-9\-]{4,24})\b",
        r"M[aã]\s*(?:đặt|dat)\s*(?:ph[oò]ng|ch[oỗ])\s*[:#\-]?\s*((?=[A-Z0-9\-]*\d)[A-Z0-9][A-Z0-9\-]{4,24})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, combined, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return ""


def parse_date(value: str, source: str = "") -> date | None:
    raw = normalized(value)
    match = re.search(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b", raw)
    if match:
        return _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    match = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_names})\.?\s*,?\s*(20\d{{2}})\b", raw)
    if match:
        return _safe_date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1)))
    match = re.search(rf"\b({month_names})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(20\d{{2}})\b", raw)
    if match:
        return _safe_date(int(match.group(3)), MONTHS[match.group(1)], int(match.group(2)))
    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b", raw)
    if match:
        first, second, year = map(int, match.groups())
        if first > 12:
            day, month = first, second
        elif second > 12:
            month, day = first, second
        elif source == "Expedia":
            month, day = first, second
        else:
            day, month = first, second
        return _safe_date(year, month, day)
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _row_value(rows: Sequence[Sequence[str]], labels: Iterable[str]) -> str:
    normalized_labels = {normalized(label) for label in labels}
    for row_index, row in enumerate(rows):
        for column, cell in enumerate(row):
            key = normalized(cell)
            if key not in normalized_labels:
                continue
            for candidate in row[column + 1:]:
                if candidate.strip():
                    return candidate.strip()
            if row_index + 1 < len(rows) and column < len(rows[row_index + 1]):
                candidate = rows[row_index + 1][column].strip()
                if candidate:
                    return candidate
    return ""


def _text_value(text: str, labels: Iterable[str], max_length: int = 180) -> str:
    for label in labels:
        match = re.search(
            rf"(?:^|\n)\s*{re.escape(label)}\s*[:\-]?\s*(?:\n\s*)?([^\r\n]{{1,{max_length}}})",
            text,
            re.IGNORECASE,
        )
        if match:
            return match.group(1).strip()
    return ""


def _date_by_labels(text: str, rows: Sequence[Sequence[str]], labels: Sequence[str], source: str) -> date | None:
    table_value = _row_value(rows, labels)
    parsed = parse_date(table_value, source)
    if parsed:
        return parsed
    for label in labels:
        match = re.search(
            rf"(?:^|\n)\s*{re.escape(label)}\s*[:\-]?\s*(?:\n\s*)?([^\r\n]{{1,100}})",
            text,
            re.IGNORECASE,
        )
        if match:
            parsed = parse_date(match.group(1), source)
            if parsed:
                return parsed
    return None


def clean_guest_name(value: str) -> str:
    value = re.sub(r"\s+", " ", html.unescape(value or "")).strip(" \t:;,-")
    rejected = (
        "country of residence", "country/region of residence", "guest country",
        "xem trong email", "http://", "https://", "@",
    )
    if not value or any(term in normalized(value) for term in rejected):
        return ""
    return value[:120]


def extract_guest_name(text: str, rows: Sequence[Sequence[str]]) -> str:
    value = clean_guest_name(_row_value(rows, GUEST_LABELS))
    if value:
        return value
    value = clean_guest_name(_text_value(text, GUEST_LABELS, 120))
    if value:
        return value
    first = _row_value(rows, ("first name", "given name", "guest first name", "traveler first name")) or _text_value(
        text, ("First Name", "Given Name", "Guest First Name", "Traveler First Name"), 60
    )
    last = _row_value(rows, ("last name", "surname", "family name", "guest last name", "traveler last name")) or _text_value(
        text, ("Last Name", "Surname", "Family Name", "Guest Last Name", "Traveler Last Name"), 60
    )
    return clean_guest_name(" ".join(part for part in (first, last) if part))


def clean_room_type(value: str) -> str:
    value = re.sub(r"\s+", " ", html.unescape(value or "")).strip(" \t:;,-")
    if not value or normalized(value) in {normalized(item) for item in ROOM_LABELS + CONFIRMATION_LABELS}:
        return ""
    return value[:220]


def _header_index(row: Sequence[str], labels: Sequence[str]) -> int | None:
    wanted = {normalized(label) for label in labels}
    for index, value in enumerate(row):
        if normalized(value) in wanted:
            return index
    return None


def extract_room_type(text: str, rows: Sequence[Sequence[str]], source: str) -> str:
    if source == "Expedia":
        for header_index, header in enumerate(rows):
            room_column = _header_index(header, ROOM_LABELS)
            confirmation_column = _header_index(header, CONFIRMATION_LABELS)
            if room_column is None or confirmation_column is None:
                continue
            order: list[str] = []
            names: dict[str, str] = {}
            confirmations: set[str] = set()
            counts: dict[str, int] = {}
            for row in rows[header_index + 1:header_index + 51]:
                if max(room_column, confirmation_column) >= len(row):
                    break
                confirmation = normalized(row[confirmation_column])
                room = clean_room_type(row[room_column])
                confirmation_is_id = bool(re.fullmatch(r"(?=.*\d)[a-z0-9-]{4,40}", confirmation))
                if not confirmation_is_id or not room:
                    if order:
                        break
                    continue
                if confirmation in confirmations:
                    continue
                confirmations.add(confirmation)
                key = normalized(room)
                if key not in counts:
                    order.append(key)
                    names[key] = room
                    counts[key] = 0
                counts[key] += 1
            if order:
                return "; ".join(f"{names[key]} x{counts[key]}" if counts[key] > 1 else names[key] for key in order)

    for header_index, header in enumerate(rows):
        room_column = _header_index(header, ROOM_LABELS)
        if room_column is None:
            continue
        count_column = _header_index(header, ROOM_COUNT_LABELS)
        allocations: OrderedDict[str, tuple[str, int]] = OrderedDict()
        for row in rows[header_index + 1:header_index + 30]:
            if room_column >= len(row):
                break
            room = clean_room_type(row[room_column])
            if not room:
                if allocations:
                    break
                continue
            count = 1
            if count_column is not None and count_column < len(row):
                match = re.search(r"\d{1,3}", row[count_column])
                count = max(1, int(match.group(0))) if match else 1
            key = normalized(room)
            old = allocations.get(key, (room, 0))
            allocations[key] = (old[0], old[1] + count)
        if allocations:
            return "; ".join(f"{name} x{count}" if count > 1 else name for name, count in allocations.values())

    # Prefer the explicit Name field and never let "Room Type Code" win.
    labels = ("Room Type Name", "Room(s)", "Room Category", "Booked Room", "Room Type", "Hạng phòng", "Loại phòng")
    value = _row_value(rows, labels) or _text_value(text, labels, 220)
    value = re.sub(r"^code\s*:\s*", "", value, flags=re.IGNORECASE)
    match = re.match(r"\s*(\d{1,3})\s*[x×]\s*(.+)", value)
    if match:
        return f"{clean_room_type(match.group(2))} x{int(match.group(1))}"
    return clean_room_type(value)


MONEY_PATTERN = re.compile(
    r"(?:(?:VND|VNĐ|USD|EUR|THB|CNY|JPY|MYR|SGD|AUD|GBP|KRW|HKD|IDR|PHP|INR|TWD|₫|đ|\$|€|¥)\s*"
    r"(?:\d[\d.,\s]*\d|\d)|(?:\d[\d.,\s]*\d|\d)\s*"
    r"(?:VND|VNĐ|USD|EUR|THB|CNY|JPY|MYR|SGD|AUD|GBP|KRW|HKD|IDR|PHP|INR|TWD|₫|đ|\$|€|¥))",
    re.IGNORECASE,
)


def clean_money(value: str) -> str:
    match = MONEY_PATTERN.search(re.sub(r"\s+", " ", html.unescape(value or "")))
    return match.group(0).strip()[:80] if match else ""


def _labeled_money(text: str, rows: Sequence[Sequence[str]], labels: Sequence[str]) -> str:
    # Label priority is deliberate. Do not return whichever row happens to be first.
    for label in labels:
        wanted = normalized(label)
        for row_index, row in enumerate(rows):
            for column, cell in enumerate(row):
                key = normalized(cell)
                if key != wanted and not key.startswith(wanted + " "):
                    continue
                candidates = list(row[column + 1:])
                if row_index + 1 < len(rows) and column < len(rows[row_index + 1]):
                    candidates.append(rows[row_index + 1][column])
                candidates.append(cell[len(label):])
                for candidate in candidates:
                    amount = clean_money(candidate)
                    if amount:
                        return amount
        match = re.search(
            rf"(?:^|\n)\s*{re.escape(label)}\s*[:\-]?\s*(?:\n\s*)?([^\r\n]{{1,120}})",
            text,
            re.IGNORECASE,
        )
        if match:
            amount = clean_money(match.group(1))
            if amount:
                return amount
    return ""


def extract_payment_model(text: str, rows: Sequence[Sequence[str]]) -> str:
    value = normalized(_row_value(rows, PAYMENT_MODEL_LABELS) or _text_value(text, PAYMENT_MODEL_LABELS, 100))
    if "expedia collect" in value or "merchant" in value:
        return "expedia_collect"
    if any(term in value for term in ("hotel collect", "property collect", "agency")):
        return "property_collect"
    searchable = normalized(text[:12000])
    has_expedia = "expedia collect" in searchable
    has_property = "hotel collect" in searchable or "property collect" in searchable or "hotel collects payment" in searchable
    if has_expedia and not has_property:
        return "expedia_collect"
    if has_property and not has_expedia:
        return "property_collect"
    return ""


def extract_revenue(text: str, rows: Sequence[Sequence[str]], source: str) -> str:
    if source != "Expedia":
        return _labeled_money(text, rows, AGODA_REVENUE_LABELS)
    model = extract_payment_model(text, rows)
    if model == "expedia_collect":
        return _labeled_money(text, rows, EXPEDIA_COLLECT_REVENUE_LABELS)
    if model == "property_collect":
        return _labeled_money(text, rows, PROPERTY_COLLECT_REVENUE_LABELS)
    # Unknown model: accept only labels whose meaning is unambiguous, with payout first.
    return _labeled_money(
        text,
        rows,
        EXPEDIA_COLLECT_REVENUE_LABELS[:-2] + PROPERTY_COLLECT_REVENUE_LABELS[:6],
    )


def parse_booking_message(message: Message) -> BookingEvent | None:
    subject = str(message.get("Subject", ""))
    sender = str(message.get("From", ""))
    source = sender_source(sender)
    if not source:
        return None
    body = message_body_text(message)
    rows = message_table_rows(message)
    status = _event_status(subject, body)
    booking_id = extract_booking_id(body, subject)
    if not booking_id:
        return None
    checkin = _date_by_labels(body, rows, CHECKIN_LABELS, source)
    checkout = _date_by_labels(body, rows, CHECKOUT_LABELS, source)
    if status == BOOKING_STATUS_NEW:
        searchable = normalized(subject + "\n" + body[:12000])
        booking_terms = (
            "new reservation", "new booking", "reservation confirmation", "booking confirmation",
            "reservation notification", "booking notification", "confirmed reservation",
            "reservation confirmed", "booking confirmed", "status confirmed", "status booked",
        )
        if not checkin or not any(term in searchable for term in booking_terms):
            return None
    received_at = str(message.get("Date", ""))
    try:
        received_at = parsedate_to_datetime(received_at).astimezone().isoformat(timespec="minutes")
    except Exception:
        pass
    return BookingEvent(
        source=source,
        booking_id=booking_id,
        status=status,
        checkin_date=checkin,
        checkout_date=checkout,
        guest_name=extract_guest_name(body, rows),
        room_type=extract_room_type(body, rows, source),
        total_revenue=extract_revenue(body, rows, source),
        subject=subject[:240],
        sender=sender[:240],
        received_at=received_at,
    )


def is_trusted_booking_sender(sender: str) -> bool:
    return bool(sender_source(sender))

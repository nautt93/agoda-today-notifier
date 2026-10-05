from __future__ import annotations

import html
import re
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from datetime import date
from email.headerregistry import HeaderRegistry
from email.message import Message
from email.utils import parsedate_to_datetime
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
    "Traveloka": ("traveloka.com",),
    "Trip": ("trip.com",),
}

CHECKIN_LABELS = (
    "check-in date", "check in date", "check-in", "check in", "arrival date",
    "arrival", "ngày nhận phòng", "ngày đến", "nhận phòng",
)
CHECKOUT_LABELS = (
    "check-out date", "check out date", "check-out", "check out", "departure date",
    "departure", "ngày trả phòng", "ngày đi", "ngày rời đi", "trả phòng",
)
GUEST_LABELS = (
    "lead traveler name", "lead guest name", "primary traveler name", "primary guest name",
    "traveler full name", "traveler name", "guest full name", "guest name",
    "customer full name", "customer name", "booking holder", "reservation holder",
    "booked by", "lead traveler", "primary traveler", "danh sách khách", "khách chính",
    "tên khách", "tên khách hàng", "tên người nhận phòng",
    "guest", "lead guest", "primary guest", "main guest", "booker", "tên người đặt",
)
ROOM_LABELS = (
    "room type name", "room name", "room category", "booked room", "unit type", "unit name",
    "accommodation type", "room type", "hạng phòng", "loại phòng", "tên phòng",
    "room description", "room type booked", "booked room type",
)
ROOM_COUNT_LABELS = (
    "number of rooms", "rooms booked", "room quantity", "quantity", "qty",
    "no of rooms", "no. of rooms", "no of room", "no. of room", "no rooms",
    "no of rms", "of rooms", "of rms", "rooms", "room(s)", "số lượng phòng", "số phòng đặt", "số phòng",
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
TRAVELOKA_REVENUE_LABELS = (
    # The rate grid/subtotal may precede a promotion or rounding adjustment.
    # Only the explicit hotel payout is the amount that the property receives.
    "Total you will receive",
    "Amount Payable to Property",
    "Amount Traveloka Will Pay You",
)
TRIP_REVENUE_LABELS = (
    "Your payout",
    "Amount Payable to Property",
    "Amount Payable to Hotel",
    "Net Amount",
    # Trip's reminder has only this reported amount, not a rate/commission grid.
    "Total amount",
)

FIRST_NAME_LABELS = (
    "customer first name", "guest first name", "traveler first name", "first name", "given name", "tên khách hàng",
)
LAST_NAME_LABELS = (
    "customer last name", "guest last name", "traveler last name", "last name", "surname", "family name", "họ khách hàng",
)
CARD_FIELD_LABELS = (
    "card holder name", "cardholder name", "card number", "credit card number",
    "virtual card number", "virtual credit card", "virtual credit card number",
    "vcc number", "valid until", "expiration date", "expiry date", "activation date",
    "validation code", "cvv", "cvc", "cvv/cvc", "security code", "card security code",
    "billing details", "billing address", "vcc amount", "card amount", "vcc status", "card status",
    "payment id", "payment status",
)
FIELD_LABELS = (
    GUEST_LABELS + FIRST_NAME_LABELS + LAST_NAME_LABELS + ROOM_LABELS + ROOM_COUNT_LABELS
    + CHECKIN_LABELS + CHECKOUT_LABELS + CONFIRMATION_LABELS + PAYMENT_MODEL_LABELS
    + AGODA_REVENUE_LABELS + EXPEDIA_COLLECT_REVENUE_LABELS + PROPERTY_COLLECT_REVENUE_LABELS
    + TRAVELOKA_REVENUE_LABELS
    + TRIP_REVENUE_LABELS
    + CARD_FIELD_LABELS
    + ("booking id", "agoda booking id", "itinerary id", "customer info", "phone", "telephone", "tel",
       "email", "address", "country", "country of residence", "country region of residence", "nationality",
       "special requests", "remarks", "meal plan", "rate plan", "cancellation policy", "payment instructions",
       "room type code", "room code", "guest details", "reservation details", "booking details", "room details",
       "adults", "children", "number of guests", "occupancy", "thank you", "important information",
       "số người", "no. of extra bed", "no of extra bed", "số giường thêm", "tên chính sách giá",
       "other guests", "khách khác", "yêu cầu đặc biệt", "quốc gia cư trú")
    + ("room information", "guest information", "extra bed information", "guest email",
       "subtotal rates", "promotion and rounding adjustment", "booked and payable by")
    + ("reservation type", "reservation", "staying period", "bed type", "arrival time", "meals",
       "guests (estimated)", "payment information", "property confirmation no.")
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


FIELD_HEADERS = {normalized(label) for label in FIELD_LABELS}
INLINE_FIELD_BOUNDARY = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(normalized(label)).replace(r"\ ", r"\s+")
                             for label in sorted(FIELD_LABELS, key=len, reverse=True)) + r")\s*:",
    re.IGNORECASE,
)


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
        self.parents: list[tuple[list[str] | None, list[str] | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self.parents.append((self.row, self.cell))
            self.row = []
            self.cell = None
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
        elif tag in {"br", "p", "div", "li"} and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"p", "div", "li"} and self.cell is not None:
            self.cell.append(" ")
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", html.unescape("".join(self.cell))).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if any(self.row):
                self.rows.append(self.row)
            self.row, self.cell = self.parents.pop() if self.parents else (None, None)


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
    html_parts: list[str] = []
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
            try:
                value = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            except LookupError:
                value = payload.decode("utf-8", errors="replace")
        text = str(value)
        if content_type == "text/html":
            html_parts.append(html_to_text(text))
        else:
            parts.append(text)
    # OTA plain alternatives can contain only a shortened name/omit the room.
    # Prefer the richer HTML, retaining plain text as a fallback for absent fields.
    return "\n\n".join(html_parts + parts)


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
            try:
                value = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
            except LookupError:
                value = raw.decode("utf-8", errors="replace")
        parser = _TableParser()
        try:
            parser.feed(value)
            parser.close()
            rows.extend(parser.rows)
        except Exception:
            continue
    return rows


def sender_source(sender: str) -> str:
    # parseaddr() can accept the first address in a malformed/ambiguous header on
    # older Python versions. The From header must identify one actual mailbox;
    # an address appearing in a display name never grants provider trust.
    if getattr(sender, "defects", ()):
        return ""
    raw = re.sub(r"\r?\n[ \t]+", " ", str(sender)).strip()
    if "\r" in raw or "\n" in raw:
        return ""
    try:
        header = HeaderRegistry()("From", raw)
        if (header.defects or len(header.addresses) != 1
                or any(group.display_name is not None for group in header.groups)):
            return ""
        mailbox = header.addresses[0]
        if not mailbox.username or not mailbox.domain:
            return ""
        domain = mailbox.domain.lower()
    except (ValueError, IndexError, TypeError):
        return ""
    for source, roots in TRUSTED_DOMAINS.items():
        if any(domain == root or domain.endswith("." + root) for root in roots):
            return source
    return ""


def is_traveloka_payment_notice(message: Message) -> bool:
    return (
        len(message.get_all("From", [])) == 1
        and sender_source(message.get("From", "")) == "Traveloka"
        and bool(re.search(
            r"\bpayment\s+(?:completed|id|received|processed|successful)\b",
            normalized(message.get("Subject", "")),
        ))
    )


def _event_status(subject: str, body: str, source: str = "") -> str:
    subject_n = normalized(subject)
    if source == "Trip":
        kind = normalized(_text_value(body, ("Reservation type", "Reservation status", "Booking status", "Notification type")))
        if re.search(r"\b(?:cancel|cancelled|canceled|cancellation)\b", kind + "\n" + subject_n):
            return BOOKING_STATUS_CANCELLED
        if re.search(r"\b(?:modify|modified|modification|amend|amended|amendment|change|changed|update|updated)\b",
                     kind + "\n" + subject_n):
            return BOOKING_STATUS_MODIFIED
    if source == "Agoda":
        # Original 1.5.5 rules: ignore cancellation/amendment notices, but do not
        # mistake cancellation policy wording elsewhere in a confirmation for its status.
        if re.search(r"\b(?:cancelled|canceled|cancellation|booking\s+cancel)\b|\bhuy\s+dat\s+phong\b|\bda\s+huy\b", subject_n):
            return BOOKING_STATUS_CANCELLED
        if re.search(r"\b(?:amended|modified|updated|changed)\b|\b(?:sua|thay\s+doi)\s+(?:booking|dat\s+phong)\b", subject_n):
            return BOOKING_STATUS_MODIFIED
        if re.search(
            r"\b(?:booking|reservation)(?:\s+status)?\s*[:\-]?\s*(?:has\s+been\s+|was\s+|is\s+)?(?:cancelled|canceled)\b"
            r"|\bnotification\s*type(?:test)?\s*[:\-]?\s*(?:booking\s*)?cancel(?:led|ed|lation)?\b"
            r"|\b(?:booking|reservation)\s+(?:has\s+been\s+|was\s+|is\s+)?(?:da\s+)?huy\b",
            normalized(body[:8000]),
        ):
            return BOOKING_STATUS_CANCELLED
        return BOOKING_STATUS_NEW
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
    if re.search(r"\b(?:huy (?:dat phong|booking)|(?:dat phong|booking).*da huy)\b", subject_n):
        return BOOKING_STATUS_CANCELLED
    # Agoda 1.5.5 rejects amendment subjects even without an adjacent 'booking' word.
    if re.search(r"\b(?:amended|modified|updated|changed)\b|\b(?:sua|thay doi)\s+(?:booking|dat phong)\b", subject_n):
        return BOOKING_STATUS_MODIFIED
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


def _trip_booking_id(text: str, subject: str) -> str:
    identifier = extract_booking_id(text, subject)
    if identifier:
        return identifier
    # The reminder omits ID/No: `Reservation:\n123...`, and its subject is
    # `Trip.com New Reservation: 123...`. Require an explicit numeric identifier.
    patterns = (
        r"\bTrip\.com\s+New\s+Reservation\s*[:#]\s*([0-9]{5,25})\b",
        r"(?:^|\n)\s*Reservation\s*:\s*([0-9]{5,25})\b",
    )
    for pattern in patterns:
        if match := re.search(pattern, subject + "\n" + text, re.IGNORECASE):
            return match.group(1)
    return ""


def _trip_stay_dates(text: str, rows: Sequence[Sequence[str]]) -> tuple[date | None, date | None]:
    value = _row_value(rows, ("Staying period", "Stay dates", "Stay period")) or _text_value(
        text, ("Staying period", "Stay dates", "Stay period"), 220,
    )
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    tokens = re.findall(
        rf"\b(?:20\d{{2}}[./-]\d{{1,2}}[./-]\d{{1,2}}"
        rf"|(?:{month_names})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?[ ,/-]+20\d{{2}}"
        rf"|\d{{1,2}}(?:st|nd|rd|th)?[ /-]+(?:{month_names})\.?[ ,/-]+20\d{{2}}"
        rf"|\d{{1,2}}[./-]\d{{1,2}}[./-]20\d{{2}})\b",
        value, re.IGNORECASE,
    )
    if len(tokens) != 2:
        return None, None
    return parse_date(tokens[0], "Trip"), parse_date(tokens[1], "Trip")


def _trip_inline_room(text: str, rows: Sequence[Sequence[str]]) -> str:
    value = _row_value(rows, ROOM_LABELS) or _text_label_value(text, ROOM_LABELS, 220)
    # Keep the complete room/rate-plan name. There is no reliable delimiter
    # between those two names, only between their name and the room quantity.
    match = re.search(r"\s*[|·•]\s*(\d{1,3})\s*room(?:\(s\)|s)?(?:\s*Allotment)?\s*$", value, re.IGNORECASE)
    if not match:
        return ""
    room = clean_room_type(value[:match.start()])
    count = _parse_room_count(match.group(1))
    return f"{room} x{count}" if room and count else ""


def parse_date(value: str, source: str = "") -> date | None:
    raw = normalized(value).translate(str.maketrans({"–": "-", "‐": "-", "‑": "-", "−": "-"}))
    match = re.search(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b", raw)
    if match:
        return _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    match = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s/-]+({month_names})\.?[\s,/-]+(20\d{{2}})\b", raw)
    if match:
        return _safe_date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1)))
    match = re.search(rf"\b({month_names})\.?[\s/-]+(\d{{1,2}})(?:st|nd|rd|th)?[\s,/-]+(20\d{{2}})\b", raw)
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
    match = re.search(r"\b(\d{1,2})\s+thang\s+(\d{1,2})(?:\s+nam)?\s+(20\d{2})\b", raw)
    if match:
        return _safe_date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    # This numeric, space-separated date was supported by the original Agoda reader.
    match = re.search(r"\b(\d{1,2})\s+(?:thang\s+)?(\d{1,2})\s*,?\s*(20\d{2})\b", raw)
    if match:
        return _safe_date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _row_value(rows: Sequence[Sequence[str]], labels: Iterable[str]) -> str:
    labels = tuple(labels)
    for row_index, row in enumerate(rows):
        for column, cell in enumerate(row):
            inline_value = ""
            if ":" in cell:
                inline_label, inline_value = cell.split(":", 1)
            else:
                inline_label = ""
            if not _matches_label(cell, labels) and not _matches_label(inline_label, labels):
                continue
            if inline_value.strip() and not _is_field_header(inline_value):
                return inline_value.strip()
            for candidate in row[column + 1:]:
                if _is_field_header(candidate):
                    break  # Horizontal headings have their values below, not to the right.
                if candidate.strip():
                    return candidate.strip()
            if row_index + 1 < len(rows) and column < len(rows[row_index + 1]):
                candidate = rows[row_index + 1][column].strip()
                if candidate and not _is_field_header(candidate):
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
    def labelled_date(candidate: str) -> date | None:
        # A flattened body may put checkout on the same line. Never let its date
        # be parsed as checkin when the arrival uses a different numeric format.
        candidate = re.split(
            r"\b(?:check\s*[-]?\s*(?:in|out)|arrival|departure|ngay\s*(?:nhan\s*phong|tra\s*phong|den|di))\b",
            strip_accents(candidate), maxsplit=1, flags=re.IGNORECASE,
        )[0]
        return parse_date(candidate, source)

    # A horizontal header row (Check-in | Check-out) has values BELOW each label.
    wanted = {normalized(label) for label in labels}
    date_headers = {normalized(label) for label in CHECKIN_LABELS + CHECKOUT_LABELS}
    for row_index, row in enumerate(rows):
        for column, cell in enumerate(row):
            label, _, inline = cell.partition(":")
            if not _matches_label(label, wanted):
                continue
            candidates = [inline] if inline.strip() else []
            if column + 1 < len(row) and normalized(row[column + 1]) not in date_headers:
                candidates.append(row[column + 1])
            if row_index + 1 < len(rows) and column < len(rows[row_index + 1]):
                candidates.append(rows[row_index + 1][column])
            for candidate in candidates:
                parsed = labelled_date(candidate)
                if parsed:
                    return parsed
    text = text.translate(str.maketrans({"–": "-", "‑": "-", "−": "-"}))
    for label in labels:
        for match in re.finditer(
            rf"(?:^|\n)\s*{re.escape(label)}\s*[:\-]?\s*(?:\n\s*)?([^\r\n]{{1,100}})",
            text,
            re.IGNORECASE,
        ):
            parsed = labelled_date(match.group(1))
            if parsed:
                return parsed
    # 1.5.5 searches labels anywhere in the flattened body, not only at line starts.
    # Keep table/line parsing first so horizontal date headings still resolve correctly.
    flat = re.sub(r"\s+", " ", strip_accents(text)).strip()
    patterns = "|".join(re.escape(normalized(label)).replace(r"\ ", r"\s+") for label in labels)
    legacy_label = (r"check\s*[-]?\s*in(?:\s*date)?" if labels == CHECKIN_LABELS
                    else r"check\s*[-]?\s*out(?:\s*date)?")
    patterns = legacy_label + "|" + patterns
    for match in re.finditer(rf"(?<!\w)(?:{patterns})\s*[:\-]?\s*(.{{0,140}})", flat, re.IGNORECASE):
        parsed = labelled_date(match.group(1))
        if parsed:
            return parsed
    return None


def clean_guest_name(value: str) -> str:
    value = re.sub(r"\s+", " ", html.unescape(value or "")).strip(" \t:;,-")
    lowered = normalized(value)
    rejected_fragments = (
        "country of residence", "country region of residence", "guest country", "quoc gia cu tru",
        "customer first name", "customer last name", "room type", "no of rooms",
        "check in", "check out", "phone", "telephone", "email", "xem trong email",
    )
    if (
        not 2 <= len(value) <= 120
        or not any(character.isalpha() for character in value)
        or lowered in {"name", "full name"}
        or any(term in lowered for term in rejected_fragments)
        or any(token in value.lower() for token in ("http://", "https://", "@"))
    ):
        return ""
    return value


def _matches_label(value: str, labels: Iterable[str]) -> bool:
    """Accept exact labels and EN + VI translations, not arbitrary prefix matches.

    Agoda puts e.g. `Customer First Name<br>Tên Khách Hàng` in one cell.
    `Room Type Code` must still never be mistaken for `Room Type`.
    """
    key = normalized(value).strip(" :-")
    wanted = {normalized(label).strip(" :-") for label in labels}
    if key in wanted:
        return True
    return any(key.startswith(label + " ") and key[len(label):].strip(" :-/") in wanted
               for label in wanted)


def _is_field_header(value: str) -> bool:
    return _matches_label(value.split(":", 1)[0], FIELD_HEADERS)


def _text_label_value(text: str, labels: Sequence[str], max_length: int = 120) -> str:
    """Read stacked bilingual label/value pairs in the flattened HTML/plain text."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        label, separator, inline = line.partition(":")
        if not _matches_label(label, labels):
            continue
        payload = ([inline] if separator and inline.strip() else []) + lines[index + 1:]
        while payload and _matches_label(payload[0], labels):
            payload.pop(0)  # The translation repeats this field; it is not its value.
        value = _field_value("\n".join(payload), max_length, multiline=True)
        if value:
            return value
    return ""


def _field_value(value: str, max_length: int, multiline: bool = False) -> str:
    # Retain wrapped names/room descriptions, but stop at another labelled field.
    parts: list[str] = []
    for line in value.splitlines()[:8 if multiline else 1]:
        if not line.strip() or _is_field_header(line):
            break
        line = unicodedata.normalize("NFC", line)
        match = INLINE_FIELD_BOUNDARY.search(strip_accents(line))
        if match:
            line = line[:match.start()]
        if line.strip():
            parts.append(line.strip(" ,;"))
        if match or len(" ".join(parts)) > max_length:
            break
    value = re.sub(r"\s+", " ", " ".join(parts)).strip(" :-,;")
    return value if len(value) <= max_length else ""


def _extract_field(text: str, labels: Iterable[str], max_length: int = 180, *, multiline: bool = False) -> str:
    """Read a label/value pair even when an OTA inserts a line break after the label."""
    for label in labels:
        match = re.search(
            rf"(?<!\w)(?:{label})(?!\w)[ \t]*[:\-]?[ \t]*(?:\r?\n[ \t]*)?([^\r\n]+)",
            text,
            re.IGNORECASE,
        )
        if match:
            payload = text[match.start(1):] if multiline else match.group(1)
            value = _field_value(payload, max_length, multiline)
            if value:
                return value[:max_length]
    return ""


def _customer_info_name(text: str, rows: Sequence[Sequence[str]]) -> str:
    """Handle Agoda's `Customer Info | Name: ... , Phone: ...` layout."""
    payloads: list[str] = []
    for row_index, row in enumerate(rows):
        for cell_index, cell in enumerate(row):
            if "customer info" not in normalized(cell):
                continue
            if ":" in cell:
                payloads.append(cell.split(":", 1)[1])
            payloads.extend(row[cell_index + 1:])
            if row_index + 1 < len(rows) and cell_index < len(rows[row_index + 1]):
                payloads.append(rows[row_index + 1][cell_index])
    for payload in payloads:
        match = re.search(
            r"(?:^|\b)Name[ \t]*:[ \t]*(.+?)(?=[ \t]*,[ \t]*(?:Phone|Telephone|Tel\.?)"
            r"[ \t]*:|$)",
            payload,
            re.IGNORECASE,
        )
        if match:
            candidate = clean_guest_name(match.group(1))
            if candidate:
                return candidate
    return clean_guest_name(_extract_field(
        text, (r"Customer\s+Info\s*(?:-|–)?\s*Name",), 120, multiline=True,
    ))


def extract_guest_name(text: str, rows: Sequence[Sequence[str]]) -> str:
    table_first = clean_guest_name(_row_value(rows, FIRST_NAME_LABELS) or _text_label_value(text, FIRST_NAME_LABELS, 60))
    table_last = clean_guest_name(_row_value(rows, LAST_NAME_LABELS) or _text_label_value(text, LAST_NAME_LABELS, 60))
    combined_table_name = clean_guest_name(" ".join(part for part in (table_first, table_last) if part))
    if table_first and table_last and combined_table_name:
        return combined_table_name

    customer_info = _customer_info_name(text, rows)
    if customer_info:
        return customer_info

    table_full_name = clean_guest_name(_row_value(rows, GUEST_LABELS) or _text_label_value(text, GUEST_LABELS))
    if table_full_name:
        return table_full_name

    full_name = clean_guest_name(_extract_field(text, (
        r"Guest\s*(?:full\s*)?name", r"Lead\s*guest\s*(?:full\s*)?name",
        r"Primary\s*guest\s*(?:full\s*)?name", r"Customer\s*(?:full\s*)?name",
        r"Booking\s*holder", r"Traveler\s*(?:full\s*)?name",
        r"Primary\s*traveler(?:\s*name)?", r"Lead\s*traveler(?:\s*name)?",
        r"Reservation\s*holder", r"Booked\s*by", r"T[eê]n\s*kh[aá]ch(?:\s*h[aà]ng)?",
        r"T[eê]n\s*người\s*đặt",
    ), 120, multiline=True))
    if full_name:
        return full_name

    first_name = clean_guest_name(_extract_field(text, (
        r"Customer\s*First\s*Name", r"Guest\s*First\s*Name", r"Traveler\s*First\s*Name",
        r"First\s*Name", r"Given\s*Name", r"(?<!\w)T[eê]n(?:\s*đ[eệ]m)?(?!\w)",
    ), 60, multiline=True))
    last_name = clean_guest_name(_extract_field(text, (
        r"Customer\s*Last\s*Name", r"Guest\s*Last\s*Name", r"Traveler\s*Last\s*Name",
        r"Last\s*Name", r"Surname", r"Family\s*Name", r"(?<!\w)H[oọ](?!\w)",
    ), 60, multiline=True))
    combined = clean_guest_name(" ".join(part for part in (first_name, last_name) if part))
    if combined:
        return combined
    return clean_guest_name(_extract_field(
        text, (r"Lead\s*guest", r"Primary\s*guest", r"Main\s*guest", r"Booker", r"Guest(?=\s*:)"),
        120, multiline=True,
    ))


def clean_room_type(value: str) -> str:
    value = re.sub(r"\s+", " ", html.unescape(value or "")).strip(" \t:;,-")
    rejected = {normalized(item) for item in ROOM_LABELS + ROOM_COUNT_LABELS + CONFIRMATION_LABELS}
    if (
        not 2 <= len(value) <= 220
        or not any(character.isalpha() for character in value)
        or _is_field_header(value)
        or normalized(value) in rejected | {"xem trong email"}
    ):
        return ""
    return value


def _parse_room_count(value: str) -> int | None:
    match = re.fullmatch(r"(\d{1,3})(?:\s+(?:rooms?|room s|rms|ph[oò]ng))?", normalized(value))
    if not match:
        return None
    count = int(match.group(1))
    return count if 1 <= count <= 100 else None


def _format_room_allocations(allocations: OrderedDict[str, tuple[str, int]], *, include_single: bool = False) -> str:
    return "; ".join(
        f"{name} x{count}" if count > 1 or include_single else name
        for name, count in allocations.values()
    )


def _room_summary(text: str) -> str:
    summary = re.search(
        r"^[ \t]*Room(?:s|\(s\))?[ \t]*:?[ \t]*(?:\r?\n[ \t]*)?(?=\d{1,3}[ \t]*[x×])",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if not summary:
        return ""
    allocations: OrderedDict[str, tuple[str, int]] = OrderedDict()
    payload = re.sub(r"(\d{1,3}[ \t]*[x×])[ \t]*\r?\n[ \t]*", r"\1 ", text[summary.end():])
    lines = payload.splitlines()
    index = 0
    allocation_pattern = re.compile(r"\s*(\d{1,3})\s*[x×]\s*(.+?)\s*")
    while index < len(lines):
        match = allocation_pattern.fullmatch(lines[index])
        if not match:
            break
        count = _parse_room_count(match.group(1))
        parts = [match.group(2)]
        index += 1
        while index < len(lines) and len(parts) < 8:
            line = lines[index]
            if not line.strip() or _is_field_header(line) or allocation_pattern.fullmatch(line):
                break
            parts.append(line)
            index += 1
        room = clean_room_type(_field_value("\n".join(parts), 220, multiline=True))
        if count is None or not room:
            continue
        key = normalized(room)
        old = allocations.get(key, (room, 0))
        allocations[key] = (old[0], old[1] + count)
    return _format_room_allocations(allocations, include_single=True)


def _header_index(row: Sequence[str], labels: Sequence[str]) -> int | None:
    for index, value in enumerate(row):
        if _matches_label(value, labels):
            return index
    return None


def _expedia_rooms_from_stay_grid(rows: Sequence[Sequence[str]]) -> int | None:
    """Room Nights is rooms * nights, not a room quantity by itself.

    Expedia's legacy notification has one room type followed by this stay grid.
    Infer quantity only from complete, consistent dates and divisible totals.
    """
    for index, header in enumerate(rows):
        arrival = _header_index(header, CHECKIN_LABELS)
        departure = _header_index(header, CHECKOUT_LABELS)
        room_nights = _header_index(header, ("room nights", "total room nights"))
        if arrival is None or departure is None or room_nights is None:
            continue
        total = 0
        stay = None
        for row in rows[index + 1:]:
            if max(arrival, departure, room_nights) >= len(row):
                break
            checkin = parse_date(row[arrival], "Expedia")
            checkout = parse_date(row[departure], "Expedia")
            if not checkin or not checkout:
                break
            nights = (checkout - checkin).days
            value = row[room_nights].strip()
            if nights <= 0 or not re.fullmatch(r"\d{1,5}", value):
                return None
            count, remainder = divmod(int(value), nights)
            if remainder or not 1 <= count <= 100 or (stay and stay != (checkin, checkout)):
                return None
            stay = (checkin, checkout)
            total += count
        if 1 <= total <= 100:
            return total
    return None


def _traveloka_room_information(text: str, rows: Sequence[Sequence[str]]) -> str:
    """Read Traveloka's `(2 × ) Room Name` grid, not its guest/extra-bed counts."""
    allocation_pattern = re.compile(r"\s*\(?\s*(\d{1,3})\s*[x×]\s*\)?\s*(.+?)\s*", re.IGNORECASE)
    allocations: OrderedDict[str, tuple[str, int]] = OrderedDict()

    def add_allocation(value: str) -> bool:
        match = allocation_pattern.fullmatch(value)
        if not match:
            return False
        count = _parse_room_count(match.group(1))
        room = clean_room_type(match.group(2))
        if count is None or not room:
            return False
        key = normalized(room)
        old = allocations.get(key, (room, 0))
        allocations[key] = (old[0], old[1] + count)
        return True

    for header_index, header in enumerate(rows):
        room_column = _header_index(header, ("room information",))
        if room_column is None:
            continue
        for row in rows[header_index + 1:header_index + 101]:
            if room_column >= len(row) or not add_allocation(row[room_column]):
                break
    if allocations:
        return _format_room_allocations(allocations, include_single=True)

    # The original plain alternative also stacks these three grid headings.
    # Scan only the immediate grid section, never a rate grid further below.
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not _matches_label(line, ("room information",)):
            continue
        for candidate in lines[index + 1:index + 13]:
            if not candidate.strip() or _matches_label(candidate, ("guest information", "extra bed information")):
                continue
            if add_allocation(candidate):
                continue
            break
    return _format_room_allocations(allocations, include_single=True)


def extract_room_type(text: str, rows: Sequence[Sequence[str]], source: str) -> str:
    if source == "Trip":
        trip_room = _trip_inline_room(text, rows)
        if trip_room:
            return trip_room
    if source == "Traveloka":
        traveloka_rooms = _traveloka_room_information(text, rows)
        if traveloka_rooms:
            return traveloka_rooms
    summary = _room_summary(text)
    if summary:
        return summary

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
                return "; ".join(f"{names[key]} x{counts[key]}" for key in order)

    for header_index, header in enumerate(rows):
        if source == "Trip" and len(header) == 1:
            # The reminder stacks one-cell label/value rows. Its following
            # Room(s) quantity must not be skipped as an unrelated grid heading.
            continue
        room_column = _header_index(header, ROOM_LABELS)
        if room_column is None or any(cell.strip() and not _is_field_header(cell) for cell in header):
            continue
        count_column = _header_index(header, ROOM_COUNT_LABELS)
        allocations: OrderedDict[str, tuple[str, int]] = OrderedDict()
        for row in rows[header_index + 1:header_index + 30]:
            if room_column >= len(row):
                break
            room = clean_room_type(row[room_column])
            if not room:
                break
            count = 1
            if count_column is not None:
                if count_column >= len(row):
                    break
                parsed_count = _parse_room_count(row[count_column])
                if parsed_count is None and row[count_column].strip():
                    break  # Do not treat unrelated detail tables as extra booked rooms.
                count = parsed_count or 1
            key = normalized(room)
            old = allocations.get(key, (room, 0))
            allocations[key] = (old[0], old[1] + count)
        if allocations:
            return _format_room_allocations(allocations, include_single=count_column is not None)

    # Prefer the explicit Name field and never let "Room Type Code" win.
    labels = (
        "Room Type Name", "Room Name", "Room Category", "Booked Room", "Room Type",
        "Unit Type", "Unit Name", "Accommodation Type", "Hạng phòng", "Loại phòng", "Tên phòng",
        "Room Description", "Room Type Booked", "Booked Room Type",
    )
    value = _row_value(rows, labels) or _text_label_value(text, labels, 220) or _extract_field(text, (
        r"Room\s*type\s*(?:name|booked)", r"Room\s*type(?!\s*code)", r"Room\s*category",
        r"Room\s*(?:name|description)", r"Booked\s*room(?:\s*type)?", r"Unit\s*(?:type|name)",
        r"Accommodation\s*type", r"H[aạ]ng\s*ph[oò]ng", r"Lo[aạ]i\s*ph[oò]ng", r"T[eê]n\s*ph[oò]ng",
    ), 220, multiline=True)
    match = re.match(r"\s*(\d{1,3})\s*[x×]\s*(.+)", value)
    if match:
        return f"{clean_room_type(match.group(2))} x{int(match.group(1))}"
    room = clean_room_type(value)
    if not room:
        return ""
    count_value = _row_value(rows, ROOM_COUNT_LABELS) or _text_label_value(text, ROOM_COUNT_LABELS, 40) or _extract_field(text, (
        r"No\.?\s*of\s*Rooms?", r"Number\s*of\s*Rooms?", r"Quantity",
        r"S[oố]\s*lượng\s*ph[oò]ng", r"S[oố]\s*ph[oò]ng\s*đặt",
    ), 40)
    count = _parse_room_count(count_value)
    if count is None and not count_value and source == "Expedia":
        count = _expedia_rooms_from_stay_grid(rows)
    return f"{room} x{count}" if count is not None else room


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
    if source == "Trip":
        return _labeled_money(text, rows, TRIP_REVENUE_LABELS)
    if source == "Traveloka":
        return _labeled_money(text, rows, TRAVELOKA_REVENUE_LABELS)
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
    if len(message.get_all("From", [])) != 1:
        return None
    sender_header = message.get("From", "")
    source = sender_source(sender_header)
    if not source:
        return None
    if is_traveloka_payment_notice(message):
        # Payment receipts may quote confirmation text and a stay/guest/VCC.
        # They enrich an explicitly requested printout, never create an alert.
        return None
    sender = str(sender_header)
    body = message_body_text(message)
    rows = message_table_rows(message)
    if source == "Trip":
        # Only Trip's stacked reminder uses this colon convention; preserve the
        # existing Agoda/Expedia/Traveloka parsing semantics unchanged.
        body = body.replace("：", ":")
        rows = [[cell.replace("：", ":") for cell in row] for row in rows]
        if re.search(r"\b(?:payment|remittance|invoice|refund)\b", normalized(subject)) and not re.search(
                r"\bnew\s+(?:booking|reservation)\b", normalized(subject)):
            return None
    status = _event_status(subject, body, source)
    booking_id = _trip_booking_id(body, subject) if source == "Trip" else extract_booking_id(body, subject)
    if not booking_id and source != "Agoda":
        return None
    checkin = _date_by_labels(body, rows, CHECKIN_LABELS, source)
    checkout = _date_by_labels(body, rows, CHECKOUT_LABELS, source)
    if source == "Trip" and (not checkin or not checkout):
        stay_checkin, stay_checkout = _trip_stay_dates(body, rows)
        checkin = checkin or stay_checkin
        checkout = checkout or stay_checkout
    if status == BOOKING_STATUS_NEW:
        if source == "Trip":
            kind = _text_value(body, ("Reservation type", "Reservation status", "Booking status", "Notification type"))
            if kind and not re.match(r"^(?:new|confirmed)\b", normalized(kind)):
                return None
        searchable = normalized(subject + "\n" + body[:12000])
        booking_terms = (
            "new reservation", "new booking", "reservation confirmation", "booking confirmation",
            "reservation notification", "booking notification", "reservation details", "confirmed reservation",
            "reservation confirmed", "booking confirmed", "status confirmed", "status booked",
            "xac nhan dat phong", "dat phong moi", "thong bao dat phong",
        )
        agoda_terms = ("booking", "reservation", "confirmation", "dat phong", "xac nhan")
        if source == "Agoda":
            terms = agoda_terms
        elif source == "Traveloka":
            terms = booking_terms + ("confirmed - traveloka itinerary id",)
        else:
            terms = booking_terms
        if not checkin or not any(term in searchable for term in terms):
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

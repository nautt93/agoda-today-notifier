"""Traveloka print-only data, including optional exact-linked payment receipts.

PAN/CVC and receipt details stay in memory. They never belong in BookingEvent,
the notifier state, clipboard/Excel or diagnostic logs.
"""
from __future__ import annotations

import imaplib
import re
import ssl
from collections.abc import Sequence
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import Message
from typing import Any

from .expedia_print import CARD_KEYS, ExpediaPrintData, ExpediaPrintError, PrintCard, PrintRoom, is_explicitly_charged
from .mail_monitor import _response_bytes
from .models import BOOKING_STATUS_NEW, BookingEvent
from .parsing import message_body_text, message_table_rows, normalized, parse_booking_message, parse_date, sender_source


class TravelokaPrintError(ExpediaPrintError):
    """Use only fixed, non-sensitive user-facing messages."""


class _UnmatchedEmail(TravelokaPrintError):
    """An unrelated email may be skipped; a malformed matching card may not."""


LABELS = {
    "Itinerary ID": "booking_id", "Traveloka Itinerary ID": "booking_id", "Reservation ID": "booking_id",
    "Guest Name": "guest", "Guest Email": "email", "Customer Email": "email",
    "Guest Phone": "phone", "Guest Telephone": "phone", "Customer Phone": "phone",
    "Check-in": "checkin", "Check-out": "checkout", "Booking Time": "booked", "Booked on": "booked",
    "Hotel Name": "hotel", "Property Name": "hotel",
    "Room Type Name": "room", "Room Type": "room", "Room Name": "room",
    "Number of Rooms": "quantity", "No. of Rooms": "quantity", "Rooms": "quantity",
    "Adults": "adults", "Number of Adults": "adults", "Children": "children", "Number of Children": "children",
    "Extra Beds": "extra_beds", "Extra Bed Information": "extra_beds", "Guest Information": "guest_info",
    "Special Request": "requests", "Special Requests": "requests", "Meal Plan": "meal",
    "Meal Plan Details": "meal", "Rate Channel": "rate_channel", "Hotel-sponsored Coupons": "coupons",
    "Cancellation policy": "cancellation_policy", "Booked and Payable by": "payment",
    "Payment Instructions": "payment", "Subtotal Rates": "subtotal",
    "Promotion and Rounding Adjustment": "adjustment", "Total you will receive": "payable",
    "Amount Payable to Property": "payable", "Notes & Instructions": "notes",
    "Card Holder Name": "holder", "Cardholder Name": "holder", "Card Number": "pan",
    "Virtual Card Number": "pan", "Virtual Credit Card": "pan", "Virtual Credit Card Number": "pan",
    "Valid Until": "expiry", "Expiration Date": "expiry", "Expiry Date": "expiry",
    "CVV": "cvv", "CVC": "cvv", "Validation Code": "cvv", "Security Code": "cvv",
    "Activation Date": "activation", "Billing Address": "address", "Card Status": "card_status",
    "VCC Status": "card_status", "VCC Amount": "vcc_amount", "Card Amount": "vcc_amount",
    "Payment ID": "payment_id", "Payment Status": "payment_status", "Invoice Amount": "invoice_amount",
    "Deposit": "deposit", "Refund": "refund", "Total": "receipt_total",
    "Card for Rooms": "card_scope", "Card for Room": "card_scope",
    "Payment Card for Rooms": "card_scope", "Payment Card for Room": "card_scope",
}
LOOKUP = {normalized(label): key for label, key in LABELS.items()}
PRIVATE_KEYS = CARD_KEYS | {"card_status", "card_scope"}
RECEIPT_KEYS = {"payment_id", "payment_status", "invoice_amount", "deposit", "refund", "receipt_total", "vcc_amount"}
ROOM_ALLOCATION = re.compile(r"\s*\(?\s*(\d{1,3})\s*[x×]\s*\)?\s*(.+?)\s*", re.IGNORECASE)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _cell_field(cell: str) -> tuple[str, str] | None:
    label, separator, value = cell.partition(":")
    key = LOOKUP.get(normalized(label))
    if key:
        return key, _clean(value) if separator else ""
    key_n = normalized(cell)
    if key_n.startswith("booking time (utc"):
        return "booked", _clean(value) if separator and ")" in label else ""
    if key_n.startswith("cancellation policy ("):
        return "cancellation_policy", _clean(value) if separator else ""
    for label in sorted(LABELS, key=len, reverse=True):
        if key_n.startswith(normalized(label) + " "):
            return LABELS[label], _clean(cell[len(label):]).lstrip(": ")
    return None


def _rows(message: Message) -> list[list[str]]:
    return message_table_rows(message) or [[line.strip()] for line in message_body_text(message).splitlines() if line.strip()]


def _row_fields(rows: Sequence[Sequence[str]]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for index, row in enumerate(rows):
        for col, cell in enumerate(row):
            item = _cell_field(cell)
            if not item:
                continue
            key, value = item
            if not value:
                for candidate in row[col + 1:]:
                    if _cell_field(candidate):
                        break
                    if candidate.strip():
                        value = _clean(candidate)
                        break
            if not value and index + 1 < len(rows):
                following = rows[index + 1]
                if col < len(following) and not _cell_field(following[col]):
                    value = _clean(following[col])
            if value:
                # Booking-wide summary precedes reservation-specific repeated totals.
                fields.setdefault(key, value)
    return fields


def _validate_expected(booking: BookingEvent, expected: BookingEvent | None) -> None:
    if expected and (expected.source != "Traveloka" or expected.storage_id != booking.storage_id
                     or expected.checkin_date != booking.checkin_date):
        raise _UnmatchedEmail("Email không khớp nguồn, mã booking hoặc ngày đến của dòng đã chọn.")


def _trusted(message: Message) -> bool:
    return len(message.get_all("From", [])) == 1 and sender_source(message.get("From", "")) == "Traveloka"


def _is_receipt(message: Message) -> bool:
    return bool(re.search(r"\bpayment\s+completed\b", normalized(message.get("Subject", ""))))


def _room_fields(rows: list[list[str]], booking: BookingEvent) -> list[PrintRoom]:
    allocations: list[tuple[int, dict[str, str]]] = []
    for index, row in enumerate(rows):
        if any(normalized(cell) == "room information" for cell in row):
            for candidate_index in range(index + 1, min(len(rows), index + 101)):
                candidate = rows[candidate_index]
                if len(candidate) == 1 and normalized(candidate[0]) in {"guest information", "extra bed information"}:
                    continue
                if len(candidate) < 1 or not (match := ROOM_ALLOCATION.fullmatch(candidate[0])):
                    break
                quantity = int(match.group(1))
                if not 1 <= quantity <= 100:
                    raise TravelokaPrintError("Số lượng phòng trong email không hợp lệ. Hãy kiểm tra email gốc.")
                fields = {"room": _clean(match.group(2)), "quantity": str(quantity)}
                occupancy = candidate[1] if len(candidate) > 1 else " ".join(
                    value for following in rows[candidate_index + 1:candidate_index + 4] for value in following
                    if not _cell_field(value))
                if occupancy:
                    info = _clean(occupancy)
                    adult = re.search(r"(\d+)\s*Adult(?:\(s\)|s)?", info, re.IGNORECASE)
                    child = re.search(r"(\d+\s*Child.*)", info, re.IGNORECASE)
                    if adult:
                        fields["adults"] = adult.group(1)
                    if child:
                        fields["children"] = child.group(1)
                if len(candidate) > 2 and candidate[2].strip():
                    fields["extra_beds"] = _clean(candidate[2])
                elif len(candidate) == 1:
                    extra = re.search(r"\b\d+\s+per room\b", occupancy, re.IGNORECASE)
                    if extra:
                        fields["extra_beds"] = extra.group()
                allocations.append((candidate_index, fields))
                if len(candidate) == 1:
                    break
    if not allocations:
        generic = _row_fields(rows)
        for index, row in enumerate(rows):
            if any((item := _cell_field(cell)) and item[0] == "room" for cell in row):
                local = _row_fields(rows[index:])
                room = local.get("room", "")
                if room:
                    fields = {"room": room}
                    if local.get("quantity"):
                        fields["quantity"] = local["quantity"]
                    allocations.append((index, fields))
        if not allocations:
            allocations = [(0, {"room": booking.room_type or "Email chưa ghi rõ hạng phòng"})]
        if len(allocations) == 1 and generic.get("quantity"):
            allocations[0][1]["quantity"] = generic["quantity"]
    rooms: list[PrintRoom] = []
    for number, (start, fields) in enumerate(allocations):
        end = allocations[number + 1][0] if number + 1 < len(allocations) else len(rows)
        local = _row_fields(rows[start:end])
        for key in ("guest", "quantity", "adults", "children", "extra_beds", "requests", "meal", "cancellation_policy"):
            if local.get(key):
                fields[key] = local[key]
        fields.setdefault("guest", booking.guest_name)
        if booking.checkin_date:
            fields["checkin"] = booking.checkin_date.strftime("%d/%m/%Y")
        if booking.checkout_date:
            fields["checkout"] = booking.checkout_date.strftime("%d/%m/%Y")
        rates: list[str] = []
        for index in range(start, end):
            header = rows[index]
            if not any(normalized(cell) == "date" for cell in header) or not any(
                    normalized(cell) == "room rates" for cell in header):
                continue
            for rate_row in rows[index + 1:end]:
                if not rate_row or not parse_date(rate_row[0], "Traveloka"):
                    break
                rates.append(" | ".join(f"{label}: {_clean(value)}" for label, value in zip(header, rate_row, strict=False)
                                        if value.strip()))
        if rates:
            fields["daily_rate"] = "; ".join(rates)
        rooms.append(PrintRoom(fields))
    return rooms


def _charged(status: str) -> bool:
    return is_explicitly_charged(status)


def _unlabelled_card_status(body: str) -> str:
    """Preserve a complete statement; a charged substring is not authorization."""
    statements: list[str] = []
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if not (re.search(r"\b(?:VCC|Card)\b", line, re.IGNORECASE)
                and re.search(r"\b(?:charg(?:e|ed|ing)|uncharged|pending|failed|declined|partial(?:ly)?|unpaid)\b",
                              line, re.IGNORECASE)):
            continue
        parts = [_clean(line)]
        for following in lines[index + 1:index + 5]:
            if not following.strip() or _cell_field(following):
                break
            # Continue wrapped status qualifiers, not another unrelated section.
            if re.search(r"\b(?:partially|partial|no|not|fully|only|but|back|pending|failed|declined|if|unless|when|once)\b",
                         following, re.IGNORECASE):
                parts.append(_clean(following))
            else:
                break
        statement = " ".join(parts).rstrip(".!").strip()
        statements.append(statement)
    # Contrary, conditional or qualified statements override a general positive
    # summary. The exact classifier deliberately rejects all of those statements.
    return next((value for value in statements if not _charged(value)), next(iter(statements), ""))


def _card_blocks(rows: list[list[str]], room_count: int, status: str = "") -> list[PrintCard]:
    raw_cards: list[PrintCard] = []
    current: dict[str, str] = {}
    scope: list[int] = [1] if room_count == 1 else []

    def finish() -> None:
        nonlocal current
        if current.get("pan"):
            pan = current["pan"]
            digits = re.sub(r"\D", "", pan)
            if (not re.fullmatch(r"[\dXx* -]+", pan) or not digits
                    or (not re.search(r"[Xx*]", pan) and not 13 <= len(digits) <= 19)):
                raise TravelokaPrintError("Số thẻ trong email không đọc được đầy đủ. Không in sai thẻ; hãy kiểm tra email gốc.")
            current.setdefault("status", status)
            if _charged(current.get("status", "")):
                current["completed"] = "true"
            raw_cards.append(PrintCard(dict(current), list(scope)))
        current = {}

    for row_index, row in enumerate(rows):
        row_fields = _row_fields(rows[row_index:row_index + 2])
        explicit = [(key, value) for cell in row if (item := _cell_field(cell)) for key, value in [item]]
        pan_fields = [item for item in explicit if item[0] == "pan"]
        if len(pan_fields) > 1:
            raise TravelokaPrintError("Email có nhiều thẻ chưa tách được. Không in thiếu thẻ; hãy kiểm tra email gốc.")
        if pan_fields:
            # Only this PAN label's own value or a genuine scalar continuation
            # belongs to this card. A following PAN header is another card block.
            pan_value = pan_fields[0][1]
            if not pan_value:
                column = next(col for col, cell in enumerate(row)
                              if (item := _cell_field(cell)) and item[0] == "pan")
                candidates: list[str] = []
                for cell in row[column + 1:]:
                    if _cell_field(cell):
                        break
                    if cell.strip():
                        candidates.append(_clean(cell))
                if len(candidates) == 1:
                    pan_value = candidates[0]
                elif not candidates and row_index + 1 < len(rows):
                    following = rows[row_index + 1]
                    values = [cell for cell in following if cell.strip()]
                    if len(values) == 1 and not _cell_field(values[0]):
                        pan_value = _clean(values[0])
            if not pan_value:
                raise TravelokaPrintError("Email có trường thẻ nhưng thiếu số thẻ. Không in thiếu thẻ; hãy kiểm tra email gốc.")
            row_fields["pan"] = pan_value
        if any(key == "card_scope" for key, _ in explicit):
            if current.get("pan"):
                finish()
            value = row_fields.get("card_scope", "")
            if not re.fullmatch(r"\d+(?:\s*[,/&]\s*\d+)*", value):
                raise TravelokaPrintError("Không xác định được phòng áp dụng thẻ. Hãy kiểm tra email gốc.")
            scope = list(dict.fromkeys(map(int, re.findall(r"\d+", value))))
            if any(number < 1 or number > room_count for number in scope):
                raise TravelokaPrintError("Phòng áp dụng thẻ không khớp booking. Hãy kiểm tra email gốc.")
        if current.get("pan") and any(key == "pan" for key, _ in explicit):
            finish()
        if (current.get("pan") and current.get("holder") and any(key == "holder" for key, _ in explicit)
                and row_fields.get("holder") != current["holder"]):
            raise TravelokaPrintError("Chủ thẻ trong email không khớp khối thẻ. Không in sai thẻ; hãy kiểm tra email gốc.")
        for key, _ in explicit:
            if key in CARD_KEYS and row_fields.get(key):
                current[key] = row_fields[key]
            elif key == "vcc_amount" and row_fields.get(key):
                current["amount"] = row_fields[key]
            elif key == "card_status" and row_fields.get(key):
                current["status"] = row_fields[key]
    finish()
    cards: list[PrintCard] = []
    for card in raw_cards:
        existing = next((item for item in cards if item.fields == card.fields), None)
        if existing is None:
            cards.append(card)
        elif existing.rooms and card.rooms:
            existing.rooms = list(dict.fromkeys(existing.rooms + card.rooms))
        else:
            existing.rooms = []
    return cards


@dataclass(repr=False)
class _Receipt:
    booking: BookingEvent
    fields: dict[str, str]
    cards: list[PrintCard]
    completed: bool
    rows: list[list[str]]


def _payment_receipt(message: Message, expected: BookingEvent | None, room_count: int) -> _Receipt:
    if not _trusted(message) or not _is_receipt(message):
        raise _UnmatchedEmail("Email không phải thông báo thanh toán hoàn tất của Traveloka.")
    rows = _rows(message)
    reservation_starts = [index for index, row in enumerate(rows) if any(
        (item := _cell_field(cell)) and item[0] == "booking_id" for cell in row)]
    reservations: list[tuple[dict[str, str], list[list[str]]]] = []
    for index, start in enumerate(reservation_starts):
        end = reservation_starts[index + 1] if index + 1 < len(reservation_starts) else len(rows)
        reservations.append((_row_fields(rows[start:end]), rows[start:end]))
    wanted = expected.booking_id if expected else ""
    matches = [(fields, local_rows) for fields, local_rows in reservations
               if not wanted or fields.get("booking_id", "").upper() == wanted.upper()]
    if not matches:
        raise _UnmatchedEmail("Email thanh toán không khớp duy nhất mã đặt phòng của dòng đã chọn.")
    if len(matches) > 1:
        raise TravelokaPrintError("Email thanh toán lặp mã đặt phòng trong nhiều khối. Không ghép nhầm hoặc in thiếu thẻ; hãy kiểm tra email gốc.")
    selected, selected_rows = matches[0]
    if len(reservations) > 1:
        raise TravelokaPrintError("Email thanh toán gộp nhiều mã đặt phòng. Không ghép nhầm thẻ; hãy kiểm tra email gốc.")
    identifier = selected.get("booking_id", "").upper()
    checkin = parse_date(selected.get("checkin", ""), "Traveloka")
    checkout = parse_date(selected.get("checkout", ""), "Traveloka")
    if not re.fullmatch(r"[A-Z0-9-]{4,40}", identifier) or not checkin:
        raise _UnmatchedEmail("Email thanh toán thiếu mã đặt phòng hoặc ngày đến hợp lệ.")
    booking = BookingEvent(source="Traveloka", booking_id=identifier, checkin_date=checkin, checkout_date=checkout,
                           guest_name=selected.get("guest", ""), room_type=selected.get("room", ""))
    _validate_expected(booking, expected)
    if expected:
        booking = BookingEvent.from_dict(expected.to_dict())
        booking.guest_name = selected.get("guest", "") or booking.guest_name
        booking.room_type = selected.get("room", "") or booking.room_type
        booking.checkout_date = checkout or booking.checkout_date
    all_fields = _row_fields(rows)
    fields = {key: value for key, value in all_fields.items() if key in RECEIPT_KEYS}
    fields["invoice_amount"] = selected.get("invoice_amount", fields.get("invoice_amount", ""))
    body = message_body_text(message)
    payment_id = re.search(r"\bPayment\s+ID\s*:\s*([A-Z0-9-]{4,40})\b", body, re.IGNORECASE)
    if payment_id:
        fields["payment_id"] = payment_id.group(1)
    status = fields.get("payment_status", "")
    observed_status = _unlabelled_card_status(body)
    if observed_status and (not status or not _charged(observed_status)):
        status = observed_status
    if not status:
        # The subject is a receipt category, not proof that a particular VCC was charged.
        status = "Trạng thái VCC chưa được xác nhận trong email"
    fields["payment_status"] = status
    cards = _card_blocks(rows, room_count, status)
    completed = _charged(status) and all(_charged(card.fields.get("status", "")) for card in cards)
    return _Receipt(booking, fields, cards, completed, selected_rows)


def _merge_receipts(data: ExpediaPrintData, messages: Sequence[Message]) -> None:
    receipts: list[_Receipt] = []
    for message in messages:
        try:
            receipt = _payment_receipt(message, data.booking, len(data.rooms))
        except _UnmatchedEmail:
            continue
        receipts.append(receipt)
        for key, value in receipt.fields.items():
            if not value:
                continue
            existing = data.fields.get(key, "")
            data.fields[key] = " | ".join(dict.fromkeys(part for part in (existing, value) if part))
        for card in receipt.cards:
            if not any(existing.fields == card.fields and existing.rooms == card.rooms for existing in data.cards):
                data.cards.append(card)
    if receipts and all(receipt.completed for receipt in receipts) and all(
            _charged(card.fields.get("status", "")) for card in data.cards):
        data.fields["payment_completed"] = "true"
    charged_pans = {re.sub(r"[ -]", "", card.fields.get("pan", "")) for receipt in receipts for card in receipt.cards
                    if card.fields.get("completed") == "true"}
    charged_pans = {pan for pan in charged_pans if re.fullmatch(r"\d{13,19}", pan)}
    for card in data.cards:
        if re.sub(r"[ -]", "", card.fields.get("pan", "")) in charged_pans:
            # Do not merge or overwrite expiry/CVC/amount/scope/raw status. An
            # identical full PAN elsewhere still needs an explicit no-recharge warning.
            card.fields["charged_related"] = "true"


def parse_traveloka_print(message: Message, expected: BookingEvent | None = None,
                         payment_messages: Sequence[Message] = ()) -> ExpediaPrintData:
    if _is_receipt(message):
        if expected is None or expected.source != "Traveloka":
            raise TravelokaPrintError("Cần chọn booking Traveloka có mã và ngày đến để in email thanh toán.")
        receipt = _payment_receipt(message, expected, 1)
        if not receipt.cards:
            raise TravelokaPrintError("Email thanh toán không có thẻ VCC. Cần email xác nhận đặt phòng để in đầy đủ.")
        rooms = _room_fields(receipt.rows, receipt.booking)
        fields = dict(receipt.fields)
        fields["guest"] = receipt.booking.guest_name
        if receipt.completed:
            fields["payment_completed"] = "true"
        # A receipt alone does not establish which of several room groups a card funds.
        for card in receipt.cards:
            if len(rooms) > 1:
                card.rooms = []
        return ExpediaPrintData(receipt.booking, fields, rooms, receipt.cards)
    booking = parse_booking_message(message)
    if not booking or booking.source != "Traveloka" or booking.status != BOOKING_STATUS_NEW:
        raise _UnmatchedEmail("Email không phải xác nhận booking mới của Traveloka.")
    _validate_expected(booking, expected)
    rows = _rows(message)
    fields = {key: value for key, value in _row_fields(rows).items() if key not in PRIVATE_KEYS}
    fields["guest"] = booking.guest_name
    fields["payable"] = booking.total_revenue
    if not fields.get("hotel"):
        fields["hotel"] = next((_clean(cell.split("City:", 1)[0]) for row in rows[:4] for cell in row
                                if "hotel" in normalized(cell) and not _cell_field(cell)), "Booking Desk")
    rooms = _room_fields(rows, booking)
    cards = _card_blocks(rows, len(rooms))
    data = ExpediaPrintData(booking, fields, rooms, cards)
    _merge_receipts(data, payment_messages)
    return data


def fetch_traveloka_print(config: dict[str, Any], password: str, expected: BookingEvent) -> ExpediaPrintData:
    if expected.source != "Traveloka" or not re.fullmatch(r"[A-Za-z0-9-]{4,40}", expected.booking_id):
        raise TravelokaPrintError("Không có mã booking Traveloka hợp lệ để tìm email gốc.")
    try:
        with imaplib.IMAP4_SSL(str(config["imap_host"]), int(config.get("imap_port", 993)),
                              ssl_context=ssl.create_default_context(), timeout=30) as client:
            client.login(str(config["email_address"]), password)
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise TravelokaPrintError("Không mở được Inbox để đọc email gốc.")
            status, found = client.uid("search", None, "TEXT", f'"{expected.booking_id}"')
            if status != "OK" or not found or found[0] is None:
                raise TravelokaPrintError("Không tìm được email Traveloka gốc.")
            confirmations: list[Message] = []
            payments: list[Message] = []
            # One exact-known-ID server search; at most six downloads total.
            for uid in sorted(found[0].split(), key=int, reverse=True)[:6]:
                status, payload = client.uid("fetch", uid, "(BODY.PEEK[])")
                raw = _response_bytes(payload) if status == "OK" else None
                if not raw:
                    continue
                message = message_from_bytes(raw, policy=policy.default)
                if not _trusted(message):
                    continue
                if _is_receipt(message):
                    if len(payments) < 3:
                        payments.append(message)
                elif len(confirmations) < 3:
                    confirmations.append(message)
            for message in confirmations:
                try:
                    return parse_traveloka_print(message, expected, payments)
                except _UnmatchedEmail:
                    continue
            for message in payments:
                try:
                    return parse_traveloka_print(message, expected)
                except _UnmatchedEmail:
                    continue
    except TravelokaPrintError:
        raise
    except Exception:
        raise TravelokaPrintError("Không đọc được email gốc. Kiểm tra kết nối và cấu hình IMAP.") from None
    raise TravelokaPrintError("Chưa tìm thấy email Traveloka khớp booking. Email gốc cần còn trong Inbox.")

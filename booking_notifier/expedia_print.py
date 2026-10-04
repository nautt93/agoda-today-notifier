"""Transient Expedia print data. Never put these objects in state or logs."""
from __future__ import annotations

import imaplib
import os
import re
import ssl
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import Message
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .mail_monitor import _response_bytes
from .models import BOOKING_STATUS_NEW, BookingEvent
from .parsing import message_body_text, message_table_rows, normalized, parse_booking_message, parse_date


class ExpediaPrintError(ValueError):
    """Only fixed, non-sensitive user-facing messages belong in this exception."""


# Keys are intentionally separate from BookingEvent: PAN/CVV must not be persisted.
LABELS = {
    "Reservation ID": "booking_id", "Guest": "guest", "Guest Name": "guest",
    "Booked on": "booked", "Guest Email": "email", "Email": "email",
    "Guest Phone": "phone", "Phone": "phone", "Telephone": "phone",
    "Room Type Name": "room", "Room Type": "room", "Room Type Code": "code",
    "Number of Rooms": "quantity", "No. of Rooms": "quantity", "Rooms": "quantity",
    "Special Request": "requests", "Special Requests": "requests",
    "Payment Instructions": "payment", "Total Booking Amount": "total",
    "Amount to Charge Expedia Group": "payable", "Amount to Charge Expedia": "payable",
    "Amount Paid Includes": "includes", "Extra Person": "extra_person",
    "Taxes": "taxes", "Extra Charges": "extra_charges", "Rate Code": "rate",
    "Discount": "discount", "Card Holder Name": "holder", "Cardholder Name": "holder",
    "Card Number": "pan", "Virtual Card Number": "pan", "Activation Date": "activation",
    "Expiration Date": "expiry", "Expiry Date": "expiry", "Validation Code": "cvv",
    "CVV": "cvv", "Security Code": "cvv", "Billing Address": "address",
    "Notes & Instructions": "notes",
}
LOOKUP = {normalized(label): key for label, key in LABELS.items()}
CARD_KEYS = {"holder", "pan", "activation", "expiry", "cvv", "address"}


@dataclass(repr=False)
class PrintRoom:
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(repr=False)
class PrintCard:
    fields: dict[str, str] = field(default_factory=dict)
    rooms: list[int] = field(default_factory=list)


@dataclass(repr=False)
class ExpediaPrintData:
    booking: BookingEvent
    fields: dict[str, str]
    rooms: list[PrintRoom]
    cards: list[PrintCard]


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _cell_field(cell: str) -> tuple[str, str] | None:
    label, separator, value = cell.partition(":")
    key = LOOKUP.get(normalized(label))
    if key:
        return key, _clean(value) if separator else ""
    return None


def _row_fields(rows: list[list[str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, row in enumerate(rows):
        for col, cell in enumerate(row):
            match = _cell_field(cell)
            if not match:
                continue
            key, value = match
            if not value:
                following: list[str] = []
                for other in row[col + 1:]:
                    if _cell_field(other):
                        break
                    if other.strip():
                        following.append(_clean(other))
                value = " | ".join(following) if key in {"requests", "notes"} else next(iter(following), "")
            # Expedia places the request/notes in the NEXT row, not the same cell.
            if not value and key in {"requests", "notes"} and index + 1 < len(rows):
                next_row = rows[index + 1]
                if not any(_cell_field(cell) for cell in next_row):
                    value = _clean(" ".join(next_row))
            if value:
                result[key] = value
        if any(normalized(cell) == "check-in" for cell in row) and index + 1 < len(rows):
            values = rows[index + 1]
            columns = {"check-in": "checkin", "check-out": "checkout", "adults": "adults",
                       "kids/ages": "children", "room nights": "room_nights", "hotel conf": "confirmation"}
            for col, header in enumerate(row):
                key = columns.get(normalized(header))
                if key and col < len(values) and values[col].strip():
                    result[key] = _clean(values[col])
    return result


def parse_expedia_print(message: Message, expected: BookingEvent | None = None) -> ExpediaPrintData:
    booking = parse_booking_message(message)
    if not booking or booking.source != "Expedia" or booking.status != BOOKING_STATUS_NEW:
        raise ExpediaPrintError("Email không phải xác nhận booking mới của Expedia.")
    if expected and (expected.source != "Expedia" or booking.storage_id != expected.storage_id
                     or booking.checkin_date != expected.checkin_date):
        raise ExpediaPrintError("Email không khớp nguồn, mã booking hoặc ngày đến của dòng đã chọn.")
    rows = message_table_rows(message)
    if not rows:
        # Plain alternatives retain labels, but never render raw unstructured text.
        rows = [[line.strip()] for line in message_body_text(message).splitlines() if line.strip()]
        expanded: list[list[str]] = []
        for index, row in enumerate(rows):
            match = _cell_field(row[0])
            if match and not match[1] and index + 1 < len(rows) and not _cell_field(rows[index + 1][0]):
                expanded.append([row[0], rows[index + 1][0]])
            else:
                expanded.append(row)
        rows = expanded
    fields = _row_fields(rows)
    # Do not allow the last room's guest to overwrite the lead guest.
    fields["guest"] = booking.guest_name
    for row in rows[:12]:
        for cell in row:
            if "@" in cell and not fields.get("email"):
                fields["email"] = _clean(cell)
            if re.fullmatch(r"[+()\d\s.-]{7,25}", cell.strip()) and not fields.get("phone"):
                fields["phone"] = cell.strip()
    # Code starts a room section; Name follows it. Some templates omit Code.
    starts = [i for i, row in enumerate(rows) if any(
        (match := _cell_field(cell)) and match[0] == "code" for cell in row)]
    if not starts:
        starts = [i for i, row in enumerate(rows) if any(
            (match := _cell_field(cell)) and match[0] == "room" for cell in row)]
    # Keep each repeated reservation/guest header with its OWN following room.
    resolved_starts = []
    for index, start in enumerate(starts):
        lower = max(-1, start - 7, starts[index - 1] if index else -1)
        resolved_starts.append(next((i for i in range(start - 1, lower, -1)
                                    if any((match := _cell_field(cell)) and match[0] == "guest" for cell in rows[i])), start))
    starts = resolved_starts
    rooms: list[PrintRoom] = []
    cards: list[PrintCard] = []
    for number, start in enumerate(starts, 1):
        end = starts[number] if number < len(starts) else len(rows)
        room_fields = _row_fields(rows[start:end])
        for row in rows[start:end]:
            for cell in row:
                if normalized(cell).startswith("daily base rate"):
                    room_fields["daily_rate"] = _clean(cell)
        room_fields["room"] = room_fields.get("room") or booking.room_type
        if not room_fields.get("quantity"):
            arrival = parse_date(room_fields.get("checkin", ""), "Expedia") or booking.checkin_date
            departure = parse_date(room_fields.get("checkout", ""), "Expedia") or booking.checkout_date
            nights = (departure - arrival).days if arrival and departure else 0
            raw_nights = room_fields.get("room_nights", "")
            if nights > 0 and raw_nights.isdigit() and int(raw_nights) % nights == 0:
                count = int(raw_nights) // nights
                if 1 <= count <= 100:
                    room_fields["quantity"] = str(count)
        rooms.append(PrintRoom({key: value for key, value in room_fields.items() if key not in CARD_KEYS}))
        card = {key: room_fields[key] for key in CARD_KEYS if room_fields.get(key)}
        address_parts = [card.get("address", "")]
        billing = False
        for row in rows[start:end]:
            if any(normalized(cell) == "billing details" for cell in row):
                billing = True
            if billing and any(normalized(cell) == "notes & instructions" for cell in row):
                billing = False
            if billing and len(row) >= 4 and not _cell_field(row[2]) and row[3].strip():
                address_parts.append(_clean(row[3]))
        if any(address_parts):
            card["address"] = " | ".join(dict.fromkeys(part for part in address_parts if part))
        if card.get("pan"):
            # Only exact duplicate card blocks may be grouped; never merge different CVVs/dates.
            existing = next((item for item in cards if item.fields == card), None)
            if existing:
                existing.rooms.append(number)
            else:
                cards.append(PrintCard(card, [number]))
    if not rooms:
        rooms = [PrintRoom({"room": booking.room_type or "Email chưa ghi rõ hạng phòng"})]
    if not cards and fields.get("pan"):
        cards.append(PrintCard({key: fields[key] for key in CARD_KEYS if fields.get(key)}, list(range(1, len(rooms) + 1))))
    elif len(rooms) > 1 and len(cards) == 1 and cards[0].rooms == [len(rooms)]:
        # A tail-only card could be booking-wide OR belong only to the last room.
        # Do not invent an allocation that the email does not explicitly identify.
        cards[0].rooms = []
    raw_card_rows = [row for row in rows if any(
        (match := _cell_field(cell)) and match[0] == "pan" for cell in row)]
    raw_pans = {_row_fields([row]).get("pan", "") for row in raw_card_rows} - {""}
    if (raw_pans - {card.fields.get("pan", "") for card in cards}
            or len(raw_card_rows) > sum(max(1, len(card.rooms)) for card in cards)):
        raise ExpediaPrintError("Email có nhiều thẻ chưa ghép được với phòng. Không in thiếu thẻ; hãy kiểm tra email gốc.")
    # Hotel name is a plain row near the beginning, not contact/legal boilerplate.
    fields["hotel"] = next((cell.strip() for row in rows[:4] for cell in row
                            if "hotel" in normalized(cell) and not _cell_field(cell)), "Booking Desk")
    # Fields are ephemeral too, but keep card data in exactly one explicit area.
    fields = {key: value for key, value in fields.items() if key not in CARD_KEYS}
    return ExpediaPrintData(booking, fields, rooms, cards)


def fetch_expedia_print(config: dict[str, Any], password: str, expected: BookingEvent) -> ExpediaPrintData:
    if expected.source != "Expedia" or not re.fullmatch(r"[A-Za-z0-9-]{4,40}", expected.booking_id):
        raise ExpediaPrintError("Không có mã booking Expedia hợp lệ để tìm email gốc.")
    try:
        with imaplib.IMAP4_SSL(str(config["imap_host"]), int(config.get("imap_port", 993)),
                              ssl_context=ssl.create_default_context(), timeout=30) as client:
            client.login(str(config["email_address"]), password)
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise ExpediaPrintError("Không mở được Inbox để đọc email gốc.")
            # Server-side lookup for ONE known booking, not another mailbox scan.
            status, found = client.uid("search", None, "TEXT", f'"{expected.booking_id}"')
            if status != "OK" or not found or found[0] is None:
                raise ExpediaPrintError("Không tìm được email Expedia gốc.")
            for uid in sorted(found[0].split(), key=int, reverse=True)[:3]:
                status, payload = client.uid("fetch", uid, "(BODY.PEEK[])")
                raw = _response_bytes(payload) if status == "OK" else None
                if not raw:
                    continue
                message = message_from_bytes(raw, policy=policy.default)
                try:
                    return parse_expedia_print(message, expected)
                except ExpediaPrintError:
                    continue
    except ExpediaPrintError:
        raise
    except Exception:
        # Server/library exceptions can include sensitive message content. Never forward them.
        raise ExpediaPrintError("Không đọc được email gốc. Kiểm tra kết nối và cấu hình IMAP.") from None
    raise ExpediaPrintError("Chưa tìm thấy email xác nhận khớp booking. Email gốc cần còn trong Inbox.")


def _font(points: float, bold: bool = False, font_path: str | None = None) -> ImageFont.FreeTypeFont:
    size = round(points * 300 / 72)
    candidates = ([font_path] if font_path else []) + [
        str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / ("arialbd.ttf" if bold else "arial.ttf")),
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(str(name), size)
        except OSError:
            pass
    raise ExpediaPrintError("Không có font Unicode để in đầy đủ tên khách.")


def is_explicitly_charged(status: str) -> bool:
    """Interpret only exact positive payment/card statements, never negations."""
    value = normalized(status)
    return value in {"charged", "payment completed", "paid"} or bool(re.fullmatch(
        r"(?:vcc|card|virtual credit card) (?:has been|was|is) charged", value,
    ))


def render_booking_a4(data: ExpediaPrintData, font_path: str | None = None) -> Image.Image:
    """Exactly one 300-dpi A4 bitmap, in memory only; never silently clip fields."""
    source = data.booking.source
    payment_completed = data.fields.get("payment_completed") == "true"
    payable_label = f"Đã thanh toán {source}" if payment_completed else f"Thu {source}"
    charged_cards = [is_explicitly_charged(card.fields.get("status", "")) for card in data.cards]
    if source not in {"Expedia", "Traveloka"}:
        raise ExpediaPrintError("Nguồn booking chưa hỗ trợ phiếu in A4.")
    if (len(data.booking.guest_name or "") + len(data.booking.booking_id or "") + len(data.booking.total_revenue or "")
            + len(str(data.fields.values())) + sum(len(str(room.fields.values())) for room in data.rooms)
            + sum(len(str(card.fields.values())) + len(str(card.rooms)) for card in data.cards) > 18000):
        raise ExpediaPrintError("Thông tin quá dài để vừa một trang A4. Không in thiếu phòng/thẻ; hãy kiểm tra email gốc.")
    scale = 300 / 72
    left, right = 36 * scale, 559 * scale
    for body_size in (10, 9.5, 9, 8.5, 8):
        image = Image.new("RGB", (2480, 3508), "white")
        draw = ImageDraw.Draw(image)
        y = 32 * scale
        navy, muted = "#172A42", "#536174"

        def write(text: str, size: float = body_size, bold: bool = False, color: str = navy,
                  gap: float = 3, draw: Any = draw) -> None:
            nonlocal y
            font = _font(size, bold, font_path)
            line = ""
            # Character wrapping also handles long unbroken references/email addresses.
            lines: list[str] = []
            for word in _clean(text).split(" "):
                candidate = f"{line} {word}".strip()
                if draw.textlength(candidate, font=font) <= right - left:
                    line = candidate
                    continue
                if line:
                    lines.append(line)
                line = ""
                for char in word:
                    if line and draw.textlength(line + char, font=font) > right - left:
                        lines.append(line)
                        line = ""
                    line += char
            if line:
                lines.append(line)
            for line in lines:
                draw.text((left, y), line, font=font, fill=color)
                y += (size * 1.45) * scale
            y += gap * scale

        def section(title: str, draw: Any = draw, size: float = body_size) -> None:
            nonlocal y
            y += 6 * scale
            draw.rectangle((left, y, right, y + 22 * scale), fill="#EEF1F5")
            y += 3 * scale
            write(title, size, True, gap=6)

        def details(fields: dict[str, str], labels: list[tuple[str, str]]) -> None:
            parts = [f"{label}: {fields[key]}" for key, label in labels if fields.get(key)]
            if parts:
                write("   |   ".join(parts))

        booking = data.booking
        title = "PHIẾU THANH TOÁN" if payment_completed else "PHIẾU ĐẶT PHÒNG"
        write(f"{source.upper()} / {title}", 19, True, gap=1)
        write(data.fields.get("hotel", "Booking Desk"), 10, gap=10)
        write(booking.guest_name or "Chưa đọc được tên khách", 15, True)
        write(f"Mã đặt phòng: {booking.booking_id}", body_size + 1, True)
        if payment_completed:
            write("ĐÃ THANH TOÁN - KHÔNG THU LẠI THẺ NÀY", body_size + 1, True, color="#A03226")
        details(data.fields, [("phone", "Điện thoại"), ("email", "Email")])
        if data.fields.get("booked"):
            write(f"Ngày đặt: {data.fields['booked']}", color=muted)
        arrival = booking.checkin_date.strftime("%d/%m/%Y") if booking.checkin_date else "Chưa rõ"
        departure = booking.checkout_date.strftime("%d/%m/%Y") if booking.checkout_date else "Chưa rõ"
        write(f"Đến: {arrival}   |   Đi: {departure}   |   Số đêm: {booking.nights or 'Chưa rõ'}", bold=True)
        details(data.fields, [("rate_channel", "Kênh giá"), ("coupons", "Ưu đãi / mã giảm giá")])
        section(f"PHÒNG ĐÃ ĐẶT / {len(data.rooms)} NHÓM PHÒNG")
        if source == "Traveloka" and data.fields.get("requests"):
            write(f"Yêu cầu booking: {data.fields['requests']}")
        for index, room in enumerate(data.rooms, 1):
            fields = room.fields
            quantity = f" x{fields['quantity']}" if fields.get("quantity") else " (số lượng chưa rõ)"
            write(f"{index}. {fields.get('room', 'Chưa rõ hạng phòng')}{quantity}", bold=True)
            details(fields, [("guest", "Khách"), ("confirmation", "Mã KS"),
                             ("adults", "Người lớn"), ("children", "Trẻ em"), ("room_nights", "Room nights")])
            details(fields, [("extra_beds", "Giường phụ"), ("meal", "Bữa ăn")])
            details(fields, [("checkin", "Đến"), ("checkout", "Đi")])
            room_details = dict(fields)
            if source == "Traveloka" and room_details.get("requests") == data.fields.get("requests"):
                room_details.pop("requests", None)
            details(room_details, [("requests", "Yêu cầu"), ("rate", "Rate"), ("discount", "Ưu đãi")])
            if fields.get("daily_rate"):
                write(fields["daily_rate"])
            details(fields, [("total", "Giá trị booking"), ("payable", payable_label)])
            details(fields, [("taxes", "Thuế"), ("extra_person", "Khách thêm"), ("extra_charges", "Phụ thu")])
            if fields.get("payment") and fields["payment"] != data.fields.get("payment"):
                write(f"Thanh toán nhóm {index}: {fields['payment']}")
            if fields.get("notes") and fields["notes"] != data.fields.get("notes"):
                write(f"Chỉ dẫn nhóm {index}: {fields['notes']}")
            if fields.get("cancellation_policy") and fields["cancellation_policy"] != data.fields.get("cancellation_policy"):
                write(f"Chính sách hủy nhóm {index}: {fields['cancellation_policy']}")
        section("THANH TOÁN ĐÃ HOÀN TẤT / THẺ GHI TRONG EMAIL" if payment_completed else "THANH TOÁN / THẺ THU TIỀN")
        # Room-specific amounts above are authoritative. Do not sum ambiguous booking-wide totals.
        if len(data.rooms) == 1 or source == "Traveloka":
            amount_label = "Khoản thanh toán ghi trong email" if payment_completed else "Khoản khách sạn thu"
            write(f"{amount_label}: {booking.total_revenue or 'Chưa rõ - kiểm tra email gốc'}", body_size + 1, True)
        if source == "Traveloka":
            details(data.fields, [("subtotal", "Trước điều chỉnh"), ("adjustment", "Điều chỉnh"), ("payable", payable_label)])
        if data.fields.get("payment"):
            write(f"Hình thức: {data.fields['payment']}")
        if data.fields.get("payment_status"):
            write(f"Trạng thái thanh toán: {data.fields['payment_status']}", bold=True,
                  color="#A03226" if payment_completed else navy)
        details(data.fields, [("payment_id", "Mã thanh toán"), ("invoice_amount", "Giá trị hóa đơn"), ("deposit", "Tạm ứng")])
        details(data.fields, [("refund", "Hoàn tiền"), ("receipt_total", "Tổng thanh toán"), ("vcc_amount", "Giá trị VCC")])
        if not data.cards:
            write("Email không có số thẻ. Không tự tạo số thẻ hoặc CVV.", bold=True)
        for card, already_charged in zip(data.cards, charged_cards, strict=True):
            fields = card.fields
            write("Thẻ cho nhóm phòng " + ", ".join(map(str, card.rooms)) if card.rooms
                  else f"Thẻ {source} ghi trong email - kiểm tra phân bổ khoản thu", bold=True)
            if already_charged:
                write("THẺ ĐÃ ĐƯỢC THU - KHÔNG THU LẠI THẺ NÀY", body_size + 1, True, color="#A03226")
            write(f"Số thẻ: {fields.get('pan', 'Không có trong email')}", body_size + 2, True)
            details(fields, [("expiry", "Hết hạn"), ("cvv", "CVV"), ("activation", "Kích hoạt")])
            details(fields, [("holder", "Chủ thẻ"), ("address", "Địa chỉ thanh toán")])
            details(fields, [("amount", "Giá trị thẻ"), ("status", "Trạng thái thẻ")])
        if data.fields.get("includes"):
            write(f"Khoản thu bao gồm: {data.fields['includes']}")
        if data.fields.get("notes"):
            write(f"Chỉ dẫn: {data.fields['notes']}")
        if data.fields.get("cancellation_policy"):
            write(f"Chính sách hủy: {data.fields['cancellation_policy']}")
        if payment_completed:
            instruction = "Phiếu xác nhận thanh toán đã hoàn tất. Không dùng thông tin thẻ này để thu tiền lần nữa."
        elif any(charged_cards):
            instruction = f"Chỉ thu khoản chưa thanh toán mà {source} cho phép; không thu lại thẻ được ghi là đã thu tiền."
        else:
            instruction = f"Chỉ thu đúng khoản {source} cho phép; khách tự thanh toán chi phí phát sinh."
        write(instruction, bold=True)
        if y <= 790 * scale:
            draw.line((left, 803 * scale, right, 803 * scale), fill="#CCD2DB", width=2)
            draw.text((left, 809 * scale), "NỘI BỘ - CÓ THÔNG TIN THẺ / KHÔNG GIAO KHÁCH / BẢO QUẢN AN TOÀN",
                      font=_font(8, False, font_path), fill=muted)
            image.info["dpi"] = (300, 300)
            image.info["body_font_points"] = body_size
            return image
        image.close()
    raise ExpediaPrintError("Thông tin quá dài để vừa một trang A4 ở cỡ chữ dễ đọc. Không in thiếu phòng/thẻ; hãy kiểm tra email gốc.")


def render_expedia_a4(data: ExpediaPrintData, font_path: str | None = None) -> Image.Image:
    """Backward-compatible entry point; shared layout preserves Expedia output."""
    return render_booking_a4(data, font_path)


def render_traveloka_a4(data: ExpediaPrintData, font_path: str | None = None) -> Image.Image:
    """Render Traveloka booking/card details with the same one-page A4 safeguards."""
    return render_booking_a4(data, font_path)

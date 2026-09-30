"""The nine-column clipboard layout used by the original 1.5.5 app."""

import re

from booking_notifier.models import BookingEvent
from booking_notifier.parsing import normalized


def sanitize_excel_cell(value: object, protect_formula: bool = True) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if protect_formula and text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def excel_amount_value(value: str) -> str:
    text = sanitize_excel_cell(value, protect_formula=False)
    match = re.search(r"\d[\d.,\s]*", text)
    if not match:
        return ""
    number = re.sub(r"\s+", "", match.group()).strip(".,")
    is_vnd = any(token in normalized(text) for token in ("vnd", "vnđ", "₫")) or bool(
        re.search(r"(?:^|\s)đ(?:\s|$)", text.lower())
    )
    if "," in number and "." in number:
        decimal_mark = "," if number.rfind(",") > number.rfind(".") else "."
        thousands_mark = "." if decimal_mark == "," else ","
        number = number.replace(thousands_mark, "").replace(decimal_mark, ".")
    elif "," in number or "." in number:
        mark = "," if "," in number else "."
        tail = number.rsplit(mark, 1)[1]
        number = number.replace(mark, "" if len(tail) == 3 else ".")
    if is_vnd and re.fullmatch(r"\d+\.0+", number):
        number = number.split(".", 1)[0]
    return number if re.fullmatch(r"\d+(?:\.\d+)?", number) else ""


def excel_tsv(alert: BookingEvent) -> str:
    source = "Expedia" if alert.source.strip().lower() == "expedia" else "Agoda"
    room_note = sanitize_excel_cell(alert.room_type, protect_formula=False)
    room_normalized = normalized(room_note)
    if room_normalized != source.lower() and not room_normalized.startswith(source.lower() + " "):
        room_note = f"{source} {room_note}".strip()
    return "\t".join((
        "",  # STT: the existing spreadsheet owns the sequence number.
        sanitize_excel_cell(alert.guest_name),
        str(alert.checkin_date.day) if alert.checkin_date else "",
        str(alert.checkout_date.day) if alert.checkout_date else "",
        str(alert.nights) if alert.nights is not None else "",
        excel_amount_value(alert.total_revenue),
        "",
        "",
        sanitize_excel_cell(room_note),
    ))


def excel_tsv_rows(alerts: list[BookingEvent]) -> str:
    return "\r\n".join(excel_tsv(alert) for alert in alerts)

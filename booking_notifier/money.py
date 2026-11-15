"""Consistent monetary display without changing stored or exported amounts."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .excel_export import excel_amount_value


def format_revenue(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    currency = re.search(r"(?<![A-Za-z])([A-Za-z]{3}|VNĐ|vnđ)(?![A-Za-z])|[₫đĐ]", text)
    if not currency:
        return text
    code = currency.group().upper()
    if code in {"VNĐ", "₫", "Đ"}:
        code = "VND"
    number_text = (text[:currency.start()] + text[currency.end():]).strip()
    if not re.fullmatch(r"[+-]?\s*\d[\d., ]*|\(\s*\d[\d., ]*\s*\)", number_text):
        return text
    number = excel_amount_value(text)
    if not number:
        return text
    try:
        amount = Decimal(number)
    except InvalidOperation:
        return text
    if number_text.startswith(("-", "(")):
        amount = -amount
    # Keep any meaningful fractional precision rather than rounding a payout.
    places = max(2, -amount.normalize().as_tuple().exponent)
    return f"{code} {amount:,.{places}f}"

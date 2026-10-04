from __future__ import annotations

from datetime import date

import pytest
from PIL import ImageDraw

from booking_notifier.expedia_print import (
    ExpediaPrintData,
    ExpediaPrintError,
    PrintCard,
    PrintRoom,
    is_explicitly_charged,
    render_booking_a4,
    render_expedia_a4,
    render_traveloka_a4,
)
from booking_notifier.models import BookingEvent


def synthetic_traveloka_print() -> ExpediaPrintData:
    """Only synthetic guest/contact/card values may be used in layout artifacts."""
    booking = BookingEvent(source="Traveloka", booking_id="TEST-TRAVEL-001", guest_name="NGUYỄN TEST KHÁCH",
                           room_type="Superior Twin x2", checkin_date=date(2026, 10, 4),
                           checkout_date=date(2026, 10, 6), total_revenue="VND 2,400,000")
    fields = {
        "hotel": "Moonlight Hotel Da Nang", "phone": "+84 000 000 000", "email": "guest@example.invalid",
        "booked": "03 Oct 2026", "payment": "Virtual card payment after activation",
        "requests": "High floor; non-smoking", "cancellation_policy": "No refund after 03 Oct 2026",
        "rate_channel": "Traveloka Hotel Collect", "coupons": "TESTCOUPON",
        "subtotal": "VND 2,500,000", "adjustment": "VND -100,000", "payable": "VND 2,400,000",
    }
    rooms = [
        PrintRoom({"room": "Superior Twin", "quantity": "1", "guest": "NGUYỄN TEST KHÁCH",
                   "adults": "2", "children": "0", "extra_beds": "0", "meal": "Breakfast included",
                   "checkin": "04 Oct 2026", "checkout": "06 Oct 2026", "daily_rate": "04-05 Oct: VND 600,000 / night",
                   "requests": fields["requests"], "cancellation_policy": fields["cancellation_policy"]}),
        PrintRoom({"room": "Deluxe Double", "quantity": "1", "guest": "SECOND TEST GUEST",
                   "adults": "1", "children": "1 (age 8)", "extra_beds": "1", "meal": "Breakfast excluded",
                   "checkin": "04 Oct 2026", "checkout": "06 Oct 2026", "daily_rate": "04-05 Oct: VND 600,000 / night",
                   "requests": "Baby cot requested", "cancellation_policy": "Second group policy: non-refundable"}),
    ]
    cards = [PrintCard({"holder": "TRAVELOKA TEST PAYMENT", "pan": "4111-1111-1111-1111", "expiry": "12/2028",
                        "cvv": "000", "activation": "04 Oct 2026", "address": "1 TEST STREET, TEST CITY"}, [1, 2])]
    return ExpediaPrintData(booking, fields, rooms, cards)


def record_final_page_text(monkeypatch):
    captured: dict[int, list[tuple[str, tuple[int, int, int, int]]]] = {}
    original = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kwargs):
        bounds = self.textbbox(xy, text, font=kwargs.get("font"))
        captured.setdefault(id(self._image), []).append((text, bounds))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    return captured


def test_traveloka_one_page_print_includes_all_room_payment_contact_and_card_details(monkeypatch):
    data = synthetic_traveloka_print()
    captured = record_final_page_text(monkeypatch)
    page = render_booking_a4(data)
    try:
        assert page.size == (2480, 3508) and page.info["dpi"] == (300, 300)
        assert page.info["body_font_points"] >= 8
        text = " ".join(line for line, _ in captured[id(page)])
        assert "TRAVELOKA / PHIẾU ĐẶT PHÒNG" in text and "EXPEDIA" not in text
        for value in (data.booking.guest_name, data.booking.booking_id, *data.fields.values(),
                      *data.rooms[0].fields.values(), *data.rooms[1].fields.values(), *data.cards[0].fields.values()):
            assert value in text
        assert "Thẻ cho nhóm phòng 1, 2" in text
        assert "CVV: 000" in text and "Thu Traveloka: VND 2,400,000" in text
        assert text.count("High floor; non-smoking") == 1
        assert text.count("No refund after 03 Oct 2026") == 1
        # The page must contain every line, including long policy/contact/card text.
        for _, (left, top, right, bottom) in captured[id(page)]:
            assert 140 <= left < right <= 2335
            assert 120 <= top < bottom <= 3460
    finally:
        page.close()


def test_traveloka_without_card_explicitly_reports_missing_source_data(monkeypatch):
    data = synthetic_traveloka_print()
    data.cards = []
    data.fields["payment"] = "Prepaid to Traveloka; payout by provider"
    captured = record_final_page_text(monkeypatch)
    page = render_traveloka_a4(data)
    try:
        text = " ".join(line for line, _ in captured[id(page)])
        assert "Email không có số thẻ. Không tự tạo số thẻ hoặc CVV." in text
        assert "Số thẻ:" not in text and "CVV:" not in text
        assert "Khoản khách sạn thu: VND 2,400,000" in text  # Explicit booking-level payout, not a sum of rooms.
        assert "Thu Expedia" not in text
    finally:
        page.close()


def test_unallocated_traveloka_card_keeps_all_fields_without_invented_room_mapping(monkeypatch):
    data = synthetic_traveloka_print()
    data.cards[0].rooms = []
    captured = record_final_page_text(monkeypatch)
    page = render_booking_a4(data)
    try:
        text = " ".join(line for line, _ in captured[id(page)])
        assert "Thẻ Traveloka ghi trong email - kiểm tra phân bổ khoản thu" in text
        assert "Thẻ cho nhóm phòng" not in text
        assert data.cards[0].fields["pan"] in text
    finally:
        page.close()


def test_existing_expedia_entry_point_keeps_same_output_as_shared_renderer():
    data = synthetic_traveloka_print()
    data.booking.source = "Expedia"
    data.rooms = data.rooms[:1]
    shared = render_booking_a4(data)
    legacy = render_expedia_a4(data)
    try:
        assert shared.size == legacy.size and shared.tobytes() == legacy.tobytes()
    finally:
        shared.close()
        legacy.close()


def test_payment_completed_receipt_preserves_card_amount_status_and_warns_not_to_charge_again(monkeypatch):
    data = synthetic_traveloka_print()
    data.rooms = data.rooms[:1]
    data.cards[0].rooms = [1]
    data.fields.update({"payment_completed": "true", "payment_id": "RECEIPT-TEST-001",
                        "payment_status": "VCC has been charged", "invoice_amount": "VND 2,400,000",
                        "deposit": "VND 0", "refund": "VND 0", "receipt_total": "VND 2,400,000",
                        "vcc_amount": "VND 2,400,000"})
    data.cards[0].fields.update(amount="VND 2,400,000", status="VCC has been charged")
    captured = record_final_page_text(monkeypatch)
    page = render_booking_a4(data)
    try:
        text = " ".join(line for line, _ in captured[id(page)])
        assert "TRAVELOKA / PHIẾU THANH TOÁN" in text
        assert "ĐÃ THANH TOÁN - KHÔNG THU LẠI THẺ NÀY" in text
        assert "Không dùng thông tin thẻ này để thu tiền lần nữa." in text
        assert "Chỉ thu đúng khoản" not in text and "THẺ THU TIỀN" not in text
        assert "Khoản khách sạn thu:" not in text and "Thu Traveloka:" not in text
        assert "Khoản thanh toán ghi trong email: VND 2,400,000" in text
        assert "Giá trị thẻ: VND 2,400,000" in text
        for key in ("payment_id", "payment_status", "invoice_amount", "deposit", "refund", "receipt_total", "vcc_amount"):
            assert data.fields[key] in text
        assert data.cards[0].fields["pan"] in text and "CVV: 000" in text
        for _, (left, top, right, bottom) in captured[id(page)]:
            assert 140 <= left < right <= 2335 and 120 <= top < bottom <= 3460
    finally:
        page.close()


def test_unknown_or_negated_payment_status_is_not_inferred_as_completed(monkeypatch):
    data = synthetic_traveloka_print()
    data.fields["payment_status"] = "VCC has not been charged"
    captured = record_final_page_text(monkeypatch)
    page = render_booking_a4(data)
    try:
        text = " ".join(line for line, _ in captured[id(page)])
        assert "TRAVELOKA / PHIẾU ĐẶT PHÒNG" in text
        assert "VCC has not been charged" in text
        assert "ĐÃ THANH TOÁN - KHÔNG THU LẠI THẺ NÀY" not in text
    finally:
        page.close()


@pytest.mark.parametrize("unknown_status", ["", "VCC has not been charged"])
def test_mixed_card_states_warn_only_for_positive_exact_charged_card(monkeypatch, unknown_status):
    data = synthetic_traveloka_print()
    data.cards[0].rooms = [1]
    data.cards[0].fields["status"] = " VCC has been charged "
    data.cards.append(PrintCard({"pan": "5555-5555-5555-4444", "expiry": "12/2028", "cvv": "000",
                                "status": unknown_status}, [2]))
    captured = record_final_page_text(monkeypatch)
    original = ImageDraw.ImageDraw.text
    colors = []

    def record_color(self, xy, text, *args, **kwargs):
        colors.append((id(self._image), text, kwargs.get("fill")))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_color)
    page = render_booking_a4(data)
    try:
        text = " ".join(line for line, _ in captured[id(page)])
        assert "TRAVELOKA / PHIẾU ĐẶT PHÒNG" in text
        assert "ĐÃ THANH TOÁN - KHÔNG THU LẠI THẺ NÀY" not in text
        assert text.count("THẺ ĐÃ ĐƯỢC THU - KHÔNG THU LẠI THẺ NÀY") == 1
        assert "không thu lại thẻ được ghi là đã thu tiền." in text
        assert "Chỉ thu đúng khoản" not in text
        for pan in (data.cards[0].fields["pan"], data.cards[1].fields["pan"]):
            assert pan in text
        assert (id(page), "THẺ ĐÃ ĐƯỢC THU - KHÔNG THU LẠI THẺ NÀY", "#A03226") in colors
        assert "payment_completed" not in data.fields
    finally:
        page.close()


def test_full_pan_linked_to_paid_receipt_warns_every_metadata_block_without_claiming_all_paid(monkeypatch):
    data = synthetic_traveloka_print()
    data.rooms = data.rooms[:1]
    data.cards = [
        PrintCard({"pan": "4111-1111-1111-1111", "expiry": "07/2028", "cvv": "000", "amount": "VND 800,000",
                   "status": "Unknown", "charged_related": "true"}, [1]),
        PrintCard({"pan": "4111111111111111", "expiry": "08/2028", "cvv": "123", "amount": "VND 700,000",
                   "status": "VCC has been charged", "charged_related": "true"}, []),
        PrintCard({"pan": "5555-5555-5555-4444", "expiry": "09/2028", "cvv": "456", "amount": "VND 300,000",
                   "status": "VCC has not been charged"}, [1]),
    ]
    captured = record_final_page_text(monkeypatch)
    original = ImageDraw.ImageDraw.text
    colors = []

    def record_color(self, xy, text, *args, **kwargs):
        colors.append((id(self._image), text, kwargs.get("fill")))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_color)
    page = render_booking_a4(data)
    try:
        text = " ".join(line for line, _ in captured[id(page)])
        assert "TRAVELOKA / PHIẾU ĐẶT PHÒNG" in text
        assert "ĐÃ THANH TOÁN - KHÔNG THU LẠI THẺ NÀY" not in text
        assert text.count("THẺ ĐÃ ĐƯỢC THU - KHÔNG THU LẠI THẺ NÀY") == 1
        assert text.count("TRÙNG SỐ THẺ CÓ GIAO DỊCH ĐÃ THU - KIỂM TRA THANH TOÁN TRƯỚC KHI THU") == 1
        assert "Thông tin thanh toán chưa thống nhất." in text
        assert "không thu trùng giao dịch đã thanh toán." in text
        assert "Chỉ thu đúng khoản" not in text and "Khoản khách sạn thu:" not in text
        assert "THANH TOÁN / KIỂM TRA TRƯỚC KHI THU" in text
        assert "payment_completed" not in data.fields
        for card in data.cards:
            for key in ("pan", "expiry", "cvv", "amount", "status"):
                assert card.fields[key] in text
        warning_colors = [color for image_id, line, color in colors if image_id == id(page)
                          and (line.startswith("THẺ ĐÃ ĐƯỢC THU") or line.startswith("TRÙNG SỐ THẺ"))]
        assert warning_colors == ["#A03226", "#A03226"]
    finally:
        page.close()


@pytest.mark.parametrize("area", ["booking", "room", "card"])
def test_one_page_overflow_fails_without_omitting_or_leaking_fields(area):
    data = synthetic_traveloka_print()
    fields = {"booking": data.fields, "room": data.rooms[0].fields, "card": data.cards[0].fields}[area]
    fields["notes" if area != "card" else "pan"] = "PRIVATE TEST MUST NOT LEAK " * 1000
    with pytest.raises(ExpediaPrintError, match="quá dài") as caught:
        render_booking_a4(data)
    assert "PRIVATE TEST" not in str(caught.value)


def test_unsupported_source_fails_without_invented_print_format():
    data = synthetic_traveloka_print()
    data.booking.source = "Unknown"
    with pytest.raises(ExpediaPrintError, match="chưa hỗ trợ"):
        render_booking_a4(data)


def test_many_groups_that_exceed_page_height_are_rejected_not_clipped():
    data = synthetic_traveloka_print()
    data.rooms = [PrintRoom(dict(data.rooms[0].fields, guest=f"SYNTHETIC GROUP {number}")) for number in range(1, 11)]
    data.cards = [PrintCard(dict(data.cards[0].fields), [number]) for number in range(1, 11)]
    with pytest.raises(ExpediaPrintError, match="cỡ chữ dễ đọc"):
        render_booking_a4(data)


@pytest.mark.parametrize("status", ["paid", "Charged", " Payment Completed "] + [
    f"{subject} {verb} charged" for subject in ("VCC", "Card", "Virtual Credit Card")
    for verb in ("has been", "was", "is")
])
def test_explicit_charged_classifier_accepts_only_positive_completed_statements(status):
    assert is_explicitly_charged(status)


@pytest.mark.parametrize("status", ["", "unknown", "unpaid", "uncharged", "VCC has not been charged",
                                    "VCC has been not charged", "Card was not charged", "Card is not charged",
                                    "Virtual Credit Card was not charged", "Payment Completed but awaiting charge",
                                    "VCC may be charged", "Is this card charged?"])
def test_explicit_charged_classifier_rejects_negative_ambiguous_and_unknown_statements(status):
    assert not is_explicitly_charged(status)

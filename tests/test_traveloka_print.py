from __future__ import annotations

from datetime import date
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock

import pytest

from booking_notifier.expedia_print import CARD_KEYS
from booking_notifier.models import BookingEvent
from booking_notifier.parsing import html_to_text
from booking_notifier.traveloka_print import TravelokaPrintError, fetch_traveloka_print, parse_traveloka_print

FIXTURES = Path(__file__).parent / "fixtures"
IDENTIFIER = "20261234000001"


def confirmation(html: str | None = None, *, plain: bool = False) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Traveloka <booking@traveloka.com>"
    message["Subject"] = f"CONFIRMED - Traveloka Itinerary ID {IDENTIFIER} (Test Hotel, VIETNAM)"
    source = html if html is not None else (FIXTURES / "traveloka_print_test.html").read_text(encoding="utf-8")
    message.set_content(html_to_text(source) if plain else source, subtype="plain" if plain else "html")
    return message


def receipt(*, identifier: str = IDENTIFIER, checkin: str = "Oct 4, 2026", sender: str = "Traveloka <booking@traveloka.com>",
            html: str | None = None, status: str = "VCC has been charged", plain: bool = False) -> EmailMessage:
    # Entirely synthetic example matching the real receipt's nested-cell fields.
    source = html or f"""<html><body>
<p>Automated Payment Completed</p><p>Payment ID : PAYMENT-00001</p>
<p>{status}</p>
<table><tr><td>Virtual Credit Card<br>4111-1111-1111-1111</td>
<td>Valid Until<br>07/2028</td><td>CVC<br>000</td><td>VCC Amount<br>VND 800,000</td></tr></table>
<table><tr><td>Invoice Amount</td><td>VND 820,000</td></tr>
<tr><td>Deposit</td><td>(VND 10,000)</td></tr><tr><td>Refund</td><td>(VND 10,000)</td></tr>
<tr><td>Total</td><td>VND 800,000</td></tr></table>
<table><tr><td>Reservation ID<br>{identifier}</td><td>Guest Name<br>Linh Trần</td></tr>
<tr><td>Check-Out<br>Oct 6, 2026</td><td>Invoice Amount<br>VND 800,000</td></tr>
<tr><td>Check-In<br>{checkin}</td><td></td></tr>
<tr><td>Room Name<br>Deluxe Double - ROOM ONLY</td></tr></table>
</body></html>"""
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = "PAYMENT COMPLETED - Payment ID PAYMENT-00001 (Test Hotel, VIETNAM)"
    message.set_content(html_to_text(source) if plain else source, subtype="plain" if plain else "html")
    return message


def expected(*, identifier: str = IDENTIFIER, checkin: date = date(2026, 10, 4)) -> BookingEvent:
    return BookingEvent(source="Traveloka", booking_id=identifier, checkin_date=checkin,
                        checkout_date=date(2026, 10, 6), guest_name="Linh Trần",
                        room_type="Deluxe Double - ROOM ONLY x2", total_revenue="VND 800,000")


def test_traveloka_print_has_full_guest_room_rate_contact_payment_and_transient_card_fields():
    data = parse_traveloka_print(confirmation(), expected())
    assert data.booking.guest_name == "Linh Trần"
    assert data.fields["hotel"] == "Test Hotel (VIETNAM)"
    assert data.fields["email"] == "guest@example.invalid"
    assert data.fields["phone"] == "+84 000 000 000"
    assert data.fields["booked"] == "Oct 4, 2026 08:00:00"
    assert data.fields["payment"] == "Traveloka"
    assert data.fields["subtotal"] == "VND 820,000"
    assert data.fields["adjustment"] == "VND -20,000"
    assert data.fields["payable"] == "VND 800,000"
    assert data.fields["rate_channel"] == "Member-only Discount"
    assert data.fields["coupons"] == "TEST COUPON"
    room = data.rooms[0].fields
    assert room["room"] == "Deluxe Double - ROOM ONLY" and room["quantity"] == "2"
    assert room["adults"] == "2" and room["children"] == "0 Child (0 - 11) y.o."
    assert room["extra_beds"] == "0 per room" and room["meal"] == "Breakfast Not Included"
    assert room["requests"] == "Non-smoking" and "100% charge" in room["cancellation_policy"]
    assert "Room Rates: VND 410,000" in room["daily_rate"] and "Oct 5, 2026" in room["daily_rate"]
    card = data.cards[0].fields
    assert card["pan"] == "4111-1111-1111-1111" and card["cvv"] == "000"
    assert card["expiry"] == "07/2028" and card["holder"] == "TEST TRAVELOKA"
    assert card["amount"] == "VND 800,000"
    for fields in (data.fields, *(room.fields for room in data.rooms), data.booking.to_dict()):
        assert not CARD_KEYS & fields.keys()
        assert not any(card["pan"] in str(value) for value in fields.values())
    for value in (data, *data.cards, *data.rooms):
        assert "4111" not in repr(value) and "000" not in repr(value)


def test_traveloka_original_confirmation_shape_without_card_never_invents_card_or_support_phone():
    source = (FIXTURES / "traveloka_confirmation.html").read_text(encoding="utf-8")
    source += "<footer>For hotel support, email support@example.invalid Phone +84 111 111 111</footer>"
    data = parse_traveloka_print(confirmation(source))
    assert data.cards == []
    assert data.fields.get("phone", "") == ""
    assert data.fields["email"] == "synthetic-guest@example.invalid"


def test_traveloka_plain_room_grid_keeps_full_name_room_quantity_and_private_card():
    data = parse_traveloka_print(confirmation(plain=True))
    assert data.booking.guest_name == "Linh Trần"
    assert data.rooms[0].fields["room"] == "Deluxe Double - ROOM ONLY"
    assert data.rooms[0].fields["quantity"] == "2"
    assert data.rooms[0].fields["meal"] == "Breakfast Not Included"
    assert data.cards[0].fields["pan"] == "4111-1111-1111-1111"
    assert data.cards[0].fields["holder"] == "TEST TRAVELOKA"


def test_traveloka_pan_first_holder_last_preserves_holder():
    source = (FIXTURES / "traveloka_print_test.html").read_text(encoding="utf-8")
    source = source.replace("<tr><td>Card Holder Name</td><td>TEST TRAVELOKA</td></tr>", "")
    source = source.replace("<tr><td>Valid Until", "<tr><td>Card Holder Name</td><td>TEST LAST HOLDER</td></tr><tr><td>Valid Until")
    card = parse_traveloka_print(confirmation(source)).cards[0]
    assert card.fields["holder"] == "TEST LAST HOLDER"


def test_traveloka_conflicting_holder_refuses_instead_of_silently_dropping_fields():
    source = (FIXTURES / "traveloka_print_test.html").read_text(encoding="utf-8")
    source = source.replace("<tr><td>Valid Until", "<tr><td>Card Holder Name</td><td>SECOND HOLDER</td></tr><tr><td>Valid Until")
    with pytest.raises(TravelokaPrintError, match="Chủ thẻ"):
        parse_traveloka_print(confirmation(source))


@pytest.mark.parametrize("same_cvv", [True, False])
def test_traveloka_multiroom_exact_card_groups_keep_all_cards_and_do_not_merge_different_cvv(same_cvv):
    source = (FIXTURES / "traveloka_print_test.html").read_text(encoding="utf-8")
    first = source.index("<!-- Entirely synthetic")
    base = source[:first]
    base += """<table><tr><th>Room Information</th><th>Guest Information</th><th>Extra Bed Information</th></tr>
<tr><td>(1 × ) Family Suite</td><td>4 Adult(s)</td><td>1 per room</td></tr></table>"""
    card = """<table><tr><td>Card for Room</td><td>{room}</td></tr>
<tr><td>Virtual Credit Card</td><td>4111-1111-1111-1111</td></tr>
<tr><td>Valid Until</td><td>07/2028</td></tr><tr><td>CVC</td><td>{cvv}</td></tr></table>"""
    data = parse_traveloka_print(confirmation(base + card.format(room=1, cvv="000") + card.format(room=2, cvv="000" if same_cvv else "123")))
    assert len(data.rooms) == 2
    if same_cvv:
        assert len(data.cards) == 1 and data.cards[0].rooms == [1, 2]
    else:
        assert len(data.cards) == 2
        assert data.cards[0].rooms == [1] and data.cards[1].rooms == [2]


def test_traveloka_multiroom_unallocated_card_is_never_assigned_to_last_room():
    source = (FIXTURES / "traveloka_print_test.html").read_text(encoding="utf-8")
    source = source.replace("<!-- Entirely synthetic", """<table><tr><th>Room Information</th><th>Guest Information</th><th>Extra Bed Information</th></tr>
<tr><td>(1 × ) Family Suite</td><td>4 Adult(s)</td><td>1 per room</td></tr></table><!-- Entirely synthetic""")
    data = parse_traveloka_print(confirmation(source))
    assert len(data.rooms) == 2 and data.cards[0].rooms == []


def test_traveloka_same_row_with_two_pans_refuses_instead_of_dropping_one():
    source = (FIXTURES / "traveloka_print_test.html").read_text(encoding="utf-8")
    source = source.replace("<td>4111-1111-1111-1111</td>", "<td>4111-1111-1111-1111</td><td>Card Number: 5555-5555-5555-4444</td>")
    with pytest.raises(TravelokaPrintError, match="Không in thiếu thẻ"):
        parse_traveloka_print(confirmation(source))


@pytest.mark.parametrize("value", ["", "Click to view card"])
def test_traveloka_present_but_blank_or_unreadable_pan_refuses_instead_of_claiming_no_card(value):
    source = (FIXTURES / "traveloka_print_test.html").read_text(encoding="utf-8")
    source = source.replace("4111-1111-1111-1111", value)
    with pytest.raises(TravelokaPrintError, match="thiếu số thẻ|Không in sai thẻ"):
        parse_traveloka_print(confirmation(source))


def test_traveloka_flat_multiroom_sections_keep_their_own_quantity():
    source = (FIXTURES / "traveloka_confirmation.html").read_text(encoding="utf-8")
    start = source.index("<table>\n  <tr><th>Room Information")
    end = source.index("</table>", start) + len("</table>")
    source = source[:start] + """<table><tr><td>Room Type</td><td>Deluxe Double</td></tr>
<tr><td>Number of Rooms</td><td>2</td></tr><tr><td>Guest Name</td><td>First Synthetic Guest</td></tr>
<tr><td>Room Type</td><td>Family Suite</td></tr><tr><td>Number of Rooms</td><td>3</td></tr>
<tr><td>Guest Name</td><td>Second Synthetic Guest</td></tr></table>""" + source[end:]
    data = parse_traveloka_print(confirmation(source))
    assert [(room.fields["room"], room.fields["quantity"]) for room in data.rooms] == [("Deluxe Double", "2"), ("Family Suite", "3")]
    assert data.rooms[0].fields["guest"] == "First Synthetic Guest"
    assert data.rooms[1].fields["guest"] == "Second Synthetic Guest"


@pytest.mark.parametrize("selected", [
    BookingEvent(source="Expedia", booking_id=IDENTIFIER, checkin_date=date(2026, 10, 4)),
    expected(identifier="20261234000002"),
    expected(checkin=date(2026, 10, 5)),
])
def test_traveloka_print_validates_expected_source_booking_id_and_date(selected):
    with pytest.raises(TravelokaPrintError, match="không khớp"):
        parse_traveloka_print(confirmation(), selected)


def test_traveloka_exact_matching_late_receipt_adds_card_without_turning_payment_id_into_booking_id():
    no_card = (FIXTURES / "traveloka_confirmation.html").read_text(encoding="utf-8")
    data = parse_traveloka_print(confirmation(no_card), expected(), [receipt()])
    assert data.booking.booking_id == IDENTIFIER and data.booking.guest_name == "Linh Trần"
    assert data.fields["payment_id"] == "PAYMENT-00001"
    assert data.fields["payment_status"] == "VCC has been charged" and data.fields["payment_completed"] == "true"
    assert data.fields["invoice_amount"] == "VND 800,000"
    assert data.fields["deposit"] == "(VND 10,000)" and data.fields["refund"] == "(VND 10,000)"
    assert len(data.cards) == 1 and data.cards[0].fields["status"] == "VCC has been charged"


@pytest.mark.parametrize("message", [
    receipt(identifier="20261234000002"),
    receipt(checkin="Oct 5, 2026"),
    receipt(sender="Traveloka <attacker@example.invalid>"),
])
def test_traveloka_unrelated_receipt_is_never_merged(message):
    no_card = (FIXTURES / "traveloka_confirmation.html").read_text(encoding="utf-8")
    data = parse_traveloka_print(confirmation(no_card), expected(), [message])
    assert not data.cards and not data.fields.get("payment_id") and not data.fields.get("payment_completed")


def test_traveloka_matching_malformed_card_is_not_silently_ignored():
    broken = receipt()
    broken.set_content(broken.get_content().replace("4111-1111-1111-1111", "Unreadable card payload"), subtype="html")
    with pytest.raises(TravelokaPrintError, match="Không in sai thẻ"):
        parse_traveloka_print(confirmation(), expected(), [broken])


def test_traveloka_batch_receipt_never_attaches_card_of_another_reservation():
    source = receipt().get_content().replace("</body>", """<table>
<tr><td>Reservation ID</td><td>20261234000099</td><td>Guest Name</td><td>Another Test Guest</td></tr>
<tr><td>Check-In</td><td>Oct 4, 2026</td></tr><tr><td>Check-Out</td><td>Oct 6, 2026</td></tr>
<tr><td>Virtual Credit Card</td><td>5555-5555-5555-4444</td></tr><tr><td>CVC</td><td>123</td></tr>
</table></body>""")
    with pytest.raises(TravelokaPrintError, match="gộp nhiều mã"):
        parse_traveloka_print(confirmation(), expected(), [receipt(html=source)])


def test_traveloka_duplicate_matching_reservation_blocks_are_not_silently_treated_as_unrelated():
    source = receipt().get_content().replace("</body>", f"""<table>
<tr><td>Reservation ID</td><td>{IDENTIFIER}</td></tr><tr><td>Check-In</td><td>Oct 4, 2026</td></tr>
<tr><td>Virtual Credit Card</td><td>5555-5555-5555-4444</td></tr><tr><td>CVC</td><td>123</td></tr>
</table></body>""")
    with pytest.raises(TravelokaPrintError, match="Không ghép nhầm hoặc in thiếu thẻ"):
        parse_traveloka_print(confirmation(), expected(), [receipt(html=source)])


@pytest.mark.parametrize("plain", [True, False])
def test_traveloka_receipt_only_print_uses_full_actual_guest_not_cached_short_name(plain):
    selected = expected()
    selected.guest_name = "Linh"
    data = parse_traveloka_print(receipt(plain=plain), selected)
    assert data.booking.guest_name == "Linh Trần" and selected.guest_name == "Linh"
    assert data.fields["payment_completed"] == "true"
    assert data.rooms[0].fields["room"] == "Deluxe Double - ROOM ONLY"
    assert len(data.cards) == 1 and data.cards[0].fields["cvv"] == "000"


def test_traveloka_receipt_only_requires_known_booking_and_never_matches_payment_id_alone():
    with pytest.raises(TravelokaPrintError, match="Cần chọn booking"):
        parse_traveloka_print(receipt())
    with pytest.raises(TravelokaPrintError, match="không khớp"):
        parse_traveloka_print(receipt(), expected(identifier="PAYMENT-00001"))


def test_traveloka_pending_or_mixed_card_status_never_sets_global_paid_marker():
    no_card = (FIXTURES / "traveloka_confirmation.html").read_text(encoding="utf-8")
    data = parse_traveloka_print(confirmation(no_card), expected(), [receipt(status="VCC has been not charged")])
    assert "payment_completed" not in data.fields
    assert data.cards[0].fields["status"] == "VCC has been not charged"
    mixed = receipt().get_content().replace("</body>", """<table>
<tr><td>Virtual Credit Card</td><td>5555-5555-5555-4444</td></tr><tr><td>CVC</td><td>123</td></tr>
<tr><td>Card Status</td><td>Not charged</td></tr></table></body>""")
    data = parse_traveloka_print(confirmation(no_card), expected(), [receipt(html=mixed)])
    assert len(data.cards) == 2 and "payment_completed" not in data.fields
    assert data.cards[0].fields["completed"] == "true" and "completed" not in data.cards[1].fields
    assert data.cards[1].fields["status"] == "Not charged"


@pytest.mark.parametrize("status", [
    "VCC has not been charged", "VCC has been not charged", "Card was not charged", "Card is not charged",
])
def test_traveloka_negative_unlabelled_card_status_cannot_fall_back_to_completed_subject(status):
    data = parse_traveloka_print(receipt(status=status), expected())
    assert data.fields["payment_status"] == status
    assert "payment_completed" not in data.fields and "completed" not in data.cards[0].fields


@pytest.mark.parametrize("status", ["VCC was charged", "Card is charged", "Virtual Credit Card has been charged"])
def test_traveloka_explicit_positive_card_status_has_fixed_completed_marker(status):
    data = parse_traveloka_print(receipt(status=status), expected())
    assert data.fields["payment_status"] == status
    assert data.fields["payment_completed"] == "true" and data.cards[0].fields["completed"] == "true"


def mock_imap(monkeypatch, messages: list[EmailMessage], *, ids: bytes = b"1 2 3") -> Mock:
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.select.return_value = ("OK", [b"100"])
    client.uid.side_effect = [("OK", [ids])] + [("OK", [(b"body", message.as_bytes())]) for message in messages]
    monkeypatch.setattr("booking_notifier.traveloka_print.imaplib.IMAP4_SSL", Mock(return_value=client))
    return client


CONFIG = {"imap_host": "imap.example.invalid", "email_address": "hotel@example.invalid"}


def test_traveloka_fetch_reads_one_exact_id_search_then_joins_late_receipt_read_only(monkeypatch):
    no_card = (FIXTURES / "traveloka_confirmation.html").read_text(encoding="utf-8")
    client = mock_imap(monkeypatch, [receipt(identifier="20261234000099"), receipt(), confirmation(no_card)])
    data = fetch_traveloka_print(CONFIG, "test-secret", expected())
    assert data.fields["payment_completed"] == "true" and len(data.cards) == 1
    client.select.assert_called_once_with("INBOX", readonly=True)
    assert client.uid.call_args_list[0].args == ("search", None, "TEXT", f'"{IDENTIFIER}"')
    assert [call.args[1] for call in client.uid.call_args_list[1:]] == [b"3", b"2", b"1"]
    assert all(call.args[2] == "(BODY.PEEK[])" for call in client.uid.call_args_list[1:])


def test_traveloka_fetch_caps_downloads_at_six_even_with_many_matching_server_results(monkeypatch):
    client = mock_imap(monkeypatch, [receipt(identifier="20261234000099")] * 6, ids=b"1 2 3 4 5 6 7 8 9")
    with pytest.raises(TravelokaPrintError, match="Chưa tìm thấy"):
        fetch_traveloka_print(CONFIG, "test-secret", expected())
    assert [call.args[1] for call in client.uid.call_args_list[1:]] == [b"9", b"8", b"7", b"6", b"5", b"4"]


def test_traveloka_fetch_can_print_exact_receipt_when_confirmation_is_not_available(monkeypatch):
    mock_imap(monkeypatch, [receipt()], ids=b"1")
    data = fetch_traveloka_print(CONFIG, "test-secret", expected())
    assert data.booking.booking_id == IDENTIFIER and len(data.cards) == 1 and data.fields["payment_completed"] == "true"


def test_traveloka_fetch_sanitizes_unexpected_private_server_errors(monkeypatch):
    monkeypatch.setattr("booking_notifier.traveloka_print.imaplib.IMAP4_SSL", Mock(side_effect=RuntimeError("private PAN secret")))
    with pytest.raises(TravelokaPrintError) as caught:
        fetch_traveloka_print(CONFIG, "test-secret", expected())
    assert "private" not in str(caught.value) and "secret" not in str(caught.value)


def test_traveloka_fetch_does_not_hide_matching_card_errors(monkeypatch):
    message = receipt()
    message.set_content(message.get_content().replace("4111-1111-1111-1111", "Invalid PAN"), subtype="html")
    mock_imap(monkeypatch, [message, confirmation()], ids=b"1 2")
    with pytest.raises(TravelokaPrintError, match="Không in sai thẻ"):
        fetch_traveloka_print(CONFIG, "test-secret", expected())

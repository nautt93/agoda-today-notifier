from __future__ import annotations

import ctypes as ct
import os
import queue
import re
import shutil
import time
from datetime import date
from email import policy
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image, ImageDraw

from app import BookingNotifierApp
from booking_notifier.expedia_print import (
    ExpediaPrintError,
    fetch_expedia_print,
    parse_expedia_print,
    render_expedia_a4,
)
from booking_notifier.models import BookingEvent
from booking_notifier.windows_print import DEVMODE_PREFIX, PRINTDLGW, PrinterJob, _native, set_a4_mode


def sample_message(html: str | None = None) -> EmailMessage:
    message = EmailMessage(policy=policy.default)
    message["From"] = "Expedia <notify@expedia.com>"
    message["Subject"] = "Expedia - New Booking - Arriving on 10 Sep 2026"
    content = html or (Path(__file__).parent / "fixtures" / "expedia_print_test.html").read_text(encoding="utf-8")
    message.set_content(content, subtype="html")
    return message


def test_full_guest_room_contact_and_card_fields_without_address_pollution():
    data = parse_expedia_print(sample_message())
    assert data.booking.guest_name == "MINH TRẦN"
    assert data.fields["phone"] == "+84 000 000 000"
    assert data.fields["email"] == "guest@example.invalid"
    room = data.rooms[0].fields
    assert room["room"] == "Superior Double Room - Breakfast excluded"
    assert room["quantity"] == "1" and room["adults"] == "2"
    assert room["requests"] == "1 Double Bed, Non-Smoking"
    assert room["payable"] == "1,700,000 VND"
    card = data.cards[0].fields
    assert card["cvv"] == "000" and card["expiry"] == "Jul 2028"
    assert card["address"] == "1 Test Street | Test City, WA 00000 | USA"
    assert card["pan"] == "4111-1111-1111-1111"
    assert "pan" not in data.fields and "pan" not in room
    assert card["pan"] not in repr(data) and "cvv" not in repr(data.cards[0])
    assert repr(data) == object.__repr__(data) and repr(data.cards[0]) == object.__repr__(data.cards[0])
    assert not any(key in data.booking.to_dict() for key in ("pan", "cvv", "card"))


def test_multiple_rooms_keep_all_card_blocks_and_correct_amounts():
    html = sample_message().get_content()
    start = html.index("<tr><td>Room Type Code:")
    end = html.index("<tr><td>DO NOT DISCLOSE")
    extra = html[start:end].replace("Superior Double Room", "Deluxe Twin Room")
    extra = extra.replace("4111-1111-1111-1111", "5555-5555-5555-4444").replace("TEST-001", "TEST-002")
    extra = extra.replace("1,700,000 VND", "1,900,000 VND").replace("<td>000</td>", "<td>123</td>")
    data = parse_expedia_print(sample_message(html[:end] + extra + html[end:]))
    assert len(data.rooms) == len(data.cards) == 2
    assert data.rooms[1].fields["room"] == "Deluxe Twin Room - Breakfast excluded"
    assert data.rooms[1].fields["payable"] == "1,900,000 VND"
    assert data.cards[0].rooms == [1] and data.cards[1].rooms == [2]
    page = render_expedia_a4(data)
    assert page.size == (2480, 3508) and page.info["body_font_points"] >= 8
    page.close()


def test_same_card_can_be_grouped_but_different_cvv_cannot():
    html = sample_message().get_content()
    start = html.index("<tr><td>Room Type Code:")
    end = html.index("<tr><td>DO NOT DISCLOSE")
    block = html[start:end]
    data = parse_expedia_print(sample_message(html[:end] + block + html[end:]))
    assert len(data.rooms) == 2 and len(data.cards) == 1 and data.cards[0].rooms == [1, 2]
    changed = block.replace("<td>000</td>", "<td>123</td>")
    data = parse_expedia_print(sample_message(html[:end] + changed + html[end:]))
    assert len(data.cards) == 2


def test_repeated_room_header_preserves_each_guest_and_confirmation():
    html = sample_message().get_content()
    start = html.index("<tr><td>Room Type Code:")
    end = html.index("<tr><td>DO NOT DISCLOSE")
    block = '<tr><td>Guest: SECOND TEST GUEST</td></tr>' + html[start:end].replace("TEST-001", "TEST-002")
    data = parse_expedia_print(sample_message(html[:end] + block + html[end:]))
    assert data.booking.guest_name == "MINH TRẦN"
    assert data.rooms[0].fields["guest"] == "MINH TRẦN"
    assert data.rooms[1].fields["guest"] == "SECOND TEST GUEST"
    assert data.rooms[0].fields["confirmation"] == "TEST-001"
    assert data.rooms[1].fields["confirmation"] == "TEST-002"


def test_no_silently_dropped_extra_card_in_one_room():
    html = sample_message().get_content().replace('<tr><td>Activation Date',
        '<tr><td>Card Number</td><td>5555-5555-5555-4444</td></tr><tr><td>Activation Date')
    with pytest.raises(ExpediaPrintError, match="Không in thiếu thẻ"):
        parse_expedia_print(sample_message(html))


def test_no_card_never_invents_payment_fields():
    html = (Path(__file__).parent / "fixtures" / "expedia_new_booking.html").read_text(encoding="utf-8")
    data = parse_expedia_print(sample_message(html))
    assert not data.cards
    page = render_expedia_a4(data)
    assert page.size == (2480, 3508)
    page.close()


def test_layout_keeps_every_card_field_and_rejects_unreadable_overflow(monkeypatch):
    data = parse_expedia_print(sample_message())
    drawn: list[str] = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kwargs):
        drawn.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    render_expedia_a4(data).close()
    text = "\n".join(drawn)
    for value in ("MINH TRẦN", "1234567890", "4111-1111-1111-1111", "Jul 2028", "CVV: 000", "1,700,000 VND"):
        assert value in text
    assert "DO NOT DISCLOSE" not in text
    data.fields["notes"] = "Long booking instructions " * 2000
    with pytest.raises(ExpediaPrintError, match="quá dài"):
        render_expedia_a4(data)


def test_fetch_is_readonly_targeted_and_checks_booking_date_and_sender(monkeypatch):
    expected = BookingEvent(source="Expedia", booking_id="1234567890", checkin_date=date(2026, 9, 10))
    wrong = sample_message()
    wrong.replace_header("From", "attacker@example.invalid")
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.select.return_value = ("OK", [b"800"])
    client.uid.side_effect = [("OK", [b"1 2 3 4 5"]), ("OK", [(b"body", wrong.as_bytes())]),
                              ("OK", [(b"body", sample_message().as_bytes())])]
    constructor = Mock(return_value=client)
    monkeypatch.setattr("booking_notifier.expedia_print.imaplib.IMAP4_SSL", constructor)
    config = {"imap_host": "imap.example.invalid", "email_address": "hotel@example.invalid"}
    data = fetch_expedia_print(config, "test-secret", expected)
    assert data.booking.booking_id == expected.booking_id
    client.select.assert_called_once_with("INBOX", readonly=True)
    assert client.uid.call_args_list[0].args == ("search", None, "TEXT", '"1234567890"')
    assert all(call.args[2] == "(BODY.PEEK[])" for call in client.uid.call_args_list[1:])
    assert client.uid.call_count == 3
    wrong_date = BookingEvent(source="Expedia", booking_id="1234567890", checkin_date=date(2026, 9, 11))
    with pytest.raises(ExpediaPrintError, match="không khớp"):
        parse_expedia_print(sample_message(), wrong_date)


def test_fetch_sanitizes_sensitive_server_exceptions(monkeypatch):
    constructor = Mock(side_effect=RuntimeError("Secret card payload must never appear"))
    monkeypatch.setattr("booking_notifier.expedia_print.imaplib.IMAP4_SSL", constructor)
    with pytest.raises(ExpediaPrintError) as caught:
        fetch_expedia_print({"imap_host": "example.invalid"}, "secret",
                            BookingEvent(source="Expedia", booking_id="1234567890"))
    assert "Secret card" not in str(caught.value)


def test_fetch_caps_at_three_matching_emails_and_never_accepts_wrong_id(monkeypatch):
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.select.return_value = ("OK", [b"1000"])
    message = sample_message()
    client.uid.side_effect = [("OK", [b"1 2 3 4 5 6"])] + [("OK", [(b"body", message.as_bytes())])] * 3
    monkeypatch.setattr("booking_notifier.expedia_print.imaplib.IMAP4_SSL", Mock(return_value=client))
    with pytest.raises(ExpediaPrintError, match="Chưa tìm thấy"):
        fetch_expedia_print({"imap_host": "example.invalid", "email_address": "test@example.invalid"}, "secret",
                            BookingEvent(source="Expedia", booking_id="9876543210", checkin_date=date(2026, 9, 10)))
    assert [call.args[1] for call in client.uid.call_args_list[1:]] == [b"6", b"5", b"4"]


def test_print_load_freezes_clicked_booking_and_sanitizes_unexpected_errors(monkeypatch):
    desktop = BookingNotifierApp.__new__(BookingNotifierApp)
    desktop.config = {"email_address": "hotel@example.invalid", "password_encrypted": "protected"}
    desktop.closing = desktop.print_loading = False
    desktop.events = queue.Queue()
    desktop.set_status = Mock()
    monkeypatch.setattr("app.unprotect_secret", lambda value: "test-secret")
    thread = Mock()
    constructor = Mock(return_value=thread)
    monkeypatch.setattr("app.threading.Thread", constructor)
    fetch = Mock(side_effect=RuntimeError("Private card payload"))
    monkeypatch.setattr("app.fetch_expedia_print", fetch)
    clicked = BookingEvent(source="Expedia", booking_id="1234567890", checkin_date=date(2026, 9, 10))
    desktop.request_expedia_print(clicked)
    assert desktop.print_loading and fetch.call_count == 0
    thread.start.assert_called_once()
    clicked.booking_id = "OTHER-BOOKING"
    constructor.call_args.kwargs["target"]()
    assert fetch.call_args.args[2].booking_id == "1234567890"
    kind, (selected, image, error) = desktop.events.get_nowait()
    assert kind == "expedia_print_ready" and selected.booking_id == "1234567890" and image is None
    assert "Private" not in error
    desktop.request_expedia_print(clicked)  # A repeated click while loading cannot duplicate the task.
    assert constructor.call_count == 1


def test_a4_settings_preserve_driver_flags_and_clear_custom_paper():
    mode = DEVMODE_PREFIX()
    mode.dmFields = 0x200 | 0x10000 | 0x4 | 0x8
    set_a4_mode(mode)
    assert (mode.dmPaperSize, mode.dmOrientation, mode.dmCopies, mode.dmScale) == (9, 1, 1, 100)
    assert mode.dmFields & 0x200 and not mode.dmFields & (0x10000 | 0x4 | 0x8)
    assert ct.sizeof(DEVMODE_PREFIX) == 92


@pytest.mark.parametrize("fail_at", [None, "StartDocW", "StartPage", "StretchDIBits", "EndPage", "EndDoc"])
def test_native_spool_is_one_page_and_aborts_failed_job(fail_at):
    gdi = Mock()
    gdi.GetDeviceCaps.side_effect = [2480, 3508]
    for name in ("StartDocW", "StartPage", "StretchDIBits", "EndPage", "EndDoc"):
        getattr(gdi, name).return_value = 0 if name == fail_at else 1
    job = PrinterJob(100, gdi)
    image = Image.new("RGB", (248, 351), "white")
    if fail_at:
        with pytest.raises(ExpediaPrintError):
            job.print_page(image)
    else:
        job.print_page(image)
    assert gdi.StartPage.call_count <= 1 and gdi.EndPage.call_count <= 1
    if not fail_at:
        gdi.StartPage.assert_called_once_with(100)
        gdi.EndPage.assert_called_once_with(100)
        gdi.AbortDoc.assert_not_called()
    elif fail_at != "StartDocW":
        gdi.AbortDoc.assert_called_once_with(100)
    gdi.DeleteDC.assert_called_once_with(100)
    assert job.hdc == 0
    image.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows GDI and virtual A4 printer integration")
def test_windows_structs_and_actual_one_page_virtual_pdf_print(tmp_path):
    gdi, kernel, dialog = _native()
    selection = PRINTDLGW()
    selection.lStructSize = ct.sizeof(PRINTDLGW)
    selection.Flags = 0x400  # Only fetch default settings, NEVER open a modal dialog.
    ok = dialog.PrintDlgW(ct.byref(selection))
    try:
        assert dialog.CommDlgExtendedError() != 1  # CDERR_STRUCTSIZE: catches wrong ctypes layout.
    finally:
        for handle in (selection.hDevMode, selection.hDevNames):
            if handle:
                kernel.GlobalFree(handle)
    assert ct.sizeof(PRINTDLGW) == (120 if ct.sizeof(ct.c_void_p) == 8 else 66)
    # Only the virtual PDF printer is allowed. Never send a test to a physical printer.
    spool = ct.WinDLL("winspool.drv", use_last_error=True)
    spool.OpenPrinterW.argtypes, spool.OpenPrinterW.restype = [ct.c_wchar_p, ct.POINTER(ct.c_void_p), ct.c_void_p], ct.c_int32
    spool.ClosePrinter.argtypes = [ct.c_void_p]
    spool.DocumentPropertiesW.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_wchar_p, ct.c_void_p, ct.c_void_p, ct.c_uint32]
    spool.DocumentPropertiesW.restype = ct.c_int32
    handle = ct.c_void_p()
    name = "Microsoft Print to PDF"
    if not spool.OpenPrinterW(name, ct.byref(handle), None):
        pytest.skip(f"Virtual PDF printer unavailable (default-printer settings returned {bool(ok)})")
    try:
        size = spool.DocumentPropertiesW(None, handle, name, None, None, 0)
        assert size >= ct.sizeof(DEVMODE_PREFIX)
        buffer = ct.create_string_buffer(size)
        assert spool.DocumentPropertiesW(None, handle, name, buffer, None, 2) == 1  # DM_OUT_BUFFER
        mode = DEVMODE_PREFIX.from_buffer(buffer)
        set_a4_mode(mode)
        gdi.CreateDCW.argtypes, gdi.CreateDCW.restype = [ct.c_wchar_p, ct.c_wchar_p, ct.c_wchar_p, ct.c_void_p], ct.c_void_p
        hdc = gdi.CreateDCW("WINSPOOL", name, None, buffer)
        assert hdc
        target = tmp_path / "synthetic-expedia-a4.pdf"
        image = render_expedia_a4(parse_expedia_print(sample_message()))
        PrinterJob(hdc, gdi).print_page(image, str(target))
        image.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if target.exists() and target.read_bytes().rstrip().endswith(b"%%EOF"):
                break
            time.sleep(0.1)
        raw = target.read_bytes()
        assert len(re.findall(rb"/Type\s*/Page\b", raw)) == 1
        media = re.search(rb"/MediaBox\s*\[\s*([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s*\]", raw)
        if os.environ.get("BOOKING_UI_SCREENSHOT_DIR"):
            directory = Path(os.environ["BOOKING_UI_SCREENSHOT_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, directory / "synthetic-expedia-printed-a4.pdf")
        assert media, re.findall(rb".{0,20}MediaBox.{0,100}", raw)
        assert abs(float(media[3]) - float(media[1]) - 595.28) < 2
        assert abs(float(media[4]) - float(media[2]) - 841.89) < 2
    finally:
        spool.ClosePrinter(handle)

"""No UI, browser, email or account access is needed for popup state selection."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta

import pytest

from booking_notifier.models import BookingEvent
from booking_notifier.popup_state import BookingComPopupState, booking_com_details_ready, booking_com_popup_state


def complete_event():
    return BookingEvent(
        source="Booking.com", booking_id="5550000001", checkin_date=date.today(),
        checkout_date=date.today() + timedelta(days=2), guest_name="SYNTHETIC FULL GUEST",
        room_type="Deluxe Room x2", total_revenue="VND 800.000", details_loaded_at="2026-10-07T12:00:00",
        details_url="https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/booking.html?res_id=5550000001&hotel_id=12345&lang=vi",
    )


@pytest.mark.parametrize("timestamp", ["", "legacy-no-timestamp", "2026-10-07T12:00:00"])
def test_readiness_requires_fields_not_timestamp_and_accepts_complete_legacy_record(timestamp):
    event = replace(complete_event(), details_loaded_at=timestamp)
    assert booking_com_details_ready(event)
    state = booking_com_popup_state(event)
    assert state.ready and state.phase == "ready" and state.title == "Đã lấy đủ thông tin"
    assert "Excel" in state.message


@pytest.mark.parametrize("field", ["guest_name", "room_type", "checkout_date", "total_revenue"])
def test_timestamp_alone_never_makes_partial_booking_ready(field):
    event = replace(complete_event(), **{field: None if field == "checkout_date" else ""})
    assert not booking_com_details_ready(event)
    state = booking_com_popup_state(event, worker_status="Booking.com 5550000002: đã lấy đầy đủ họ tên/phòng/Excel.")
    assert not state.ready and state.phase == "loading"
    assert state.title == "Vui lòng đợi lấy thông tin"
    assert "5550000002" not in state.message


@pytest.mark.parametrize("field", ["guest_name", "room_type", "total_revenue"])
@pytest.mark.parametrize("blank", [" ", "\t\n", None])
def test_whitespace_or_none_fields_are_not_ready(field, blank):
    assert not booking_com_details_ready(replace(complete_event(), **{field: blank}))


def test_missing_alert_is_unavailable_and_zero_amount_string_is_not_treated_as_missing():
    assert not booking_com_details_ready(None)
    assert booking_com_popup_state(None).phase == "unavailable"
    assert booking_com_details_ready(replace(complete_event(), total_revenue="0"))


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("status", [
    "Booking.com: cần đăng nhập trong Cài đặt.",
    "Booking.com 5550000002: chưa lấy được chi tiết.",
    "https://admin.booking.com/?ses=synthetic-secret otp=654321 password=synthetic-password",
])
def test_actual_complete_details_override_disabled_or_other_booking_global_status(enabled, status):
    state = booking_com_popup_state(complete_event(), enrichment_enabled=enabled, worker_status=status)
    assert state.ready and state.phase == "ready"
    assert state == booking_com_popup_state(complete_event())


def test_disabled_incomplete_popup_explains_enable_setting_and_does_not_request_login_instead():
    event = replace(complete_event(), room_type="")
    state = booking_com_popup_state(event, enrichment_enabled=False, worker_status="Booking.com: cần đăng nhập")
    assert not state.ready and state.phase == "disabled"
    assert "Cài đặt" in state.message and "bật" in state.message
    assert "Đăng nhập" not in state.message


@pytest.mark.parametrize("url", ["", "https://evil.invalid/booking?res_id=5550000001"])
def test_ready_details_override_missing_or_invalid_url_and_all_global_failures(url):
    event = replace(complete_event(), details_url=url)
    state = booking_com_popup_state(event, enrichment_enabled=False, worker_status="cần đăng nhập; lỗi kết nối")
    assert state.ready and state.phase == "ready" and "Excel" in state.message


@pytest.mark.parametrize("url", ["", "https://evil.invalid/booking?res_id=5550000001"])
def test_disabled_setting_precedes_missing_or_invalid_link(url):
    event = replace(complete_event(), room_type="", details_url=url)
    state = booking_com_popup_state(event, enrichment_enabled=False, worker_status="cần đăng nhập")
    assert not state.ready and state.phase == "disabled" and "bật" in state.message


@pytest.mark.parametrize("url", [
    "", "https://evil.invalid/?res_id=5550000001&hotel_id=12345",
    "http://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/booking.html?res_id=5550000001&hotel_id=12345",
    "https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/booking.html?res_id=5550000002&hotel_id=12345",
    "https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/booking.html?res_id=5550000001",
])
def test_incomplete_booking_without_valid_exact_link_never_promises_fetch_or_auto_retry(url):
    event = replace(complete_event(), guest_name="", details_url=url)
    state = booking_com_popup_state(event, worker_status="cần đăng nhập; lỗi kết nối https://evil.invalid/?ses=secret")
    assert not state.ready and state.phase == "unavailable"
    assert state.title == "Chưa có liên kết chi tiết"
    assert "Extranet Booking.com" in state.message and "chưa thể tự bổ sung" in state.message
    assert "tự thử lại" not in state.message and "đang lấy" not in state.message
    assert "secret" not in state.message and "https://" not in state.message


@pytest.mark.parametrize("status", [
    "Booking.com: cần đăng nhập trong Cài đặt → Đăng nhập Booking.com.",
    "Booking.com: không mở được Edge/Chrome. Hãy cài trình duyệt.",
    "BOOKING.COM: CẦN ĐĂNG NHẬP; chưa lấy được chi tiết.",
    "Booking.com: can dang nhap", "Booking.com: khong mo duoc Edge/Chrome",
    "Booking.com: hoàn tất đăng nhập/2FA trong cửa sổ Edge/Chrome của app. Chi tiết sẽ tự bổ sung.",
])
def test_login_status_explains_explicit_config_login_action(status):
    event = replace(complete_event(), guest_name="")
    state = booking_com_popup_state(event, worker_status=status)
    assert not state.ready and state.phase == "login_required"
    assert "Cài đặt → Đăng nhập Booking.com" in state.message
    assert "OTP" in state.message


@pytest.mark.parametrize("status", [
    "Booking.com: chưa tạo được cửa sổ nền an toàn.",
    "Booking.com: không mở được Edge/Chrome. Hãy cài Edge hoặc Chrome.",
])
def test_failed_safe_browser_launch_gives_static_browser_and_login_guidance(status):
    event = replace(complete_event(), room_type="")
    state = booking_com_popup_state(event, worker_status=status)
    assert not state.ready and state.phase == "login_required"
    assert "Edge/Chrome" in state.message and "Cài đặt → Đăng nhập Booking.com" in state.message


@pytest.mark.parametrize("status", [
    "Booking.com: chưa lấy được chi tiết. Hãy thử lại.",
    "Booking.com: không lấy được chi tiết.",
    "Booking.com: chưa lấy được đầy đủ thông tin.",
    "Booking.com: phiên trình duyệt cần kiểm tra; thông báo email vẫn hoạt động.",
    "Booking.com: lỗi kết nối", "Booking.com: mất kết nối", "Booking.com: kết nối thất bại",
    "Booking.com: không thể kết nối", "Connection failed", "Network error", "Timed out", "Browser disconnected",
])
def test_temporary_failure_reassures_automatic_retry(status):
    event = replace(complete_event(), total_revenue="")
    state = booking_com_popup_state(event, worker_status=status)
    assert not state.ready and state.phase == "retrying"
    assert "tự thử lại" in state.message and "tắt âm" in state.message


@pytest.mark.parametrize("status", [
    "", "Booking.com: đang lấy chi tiết 5550000001…",
    "Booking.com 5550000002: đã bổ sung họ tên/hạng phòng/Excel; không báo lặp.",
    "Booking.com: đăng nhập thành công, giữ phiên chạy nền.",
    "Kết nối thành công", "Unrecognized status from another provider",
])
def test_incomplete_booking_stays_loading_without_using_global_success_or_other_booking_id(status):
    event = replace(complete_event(), checkout_date=None)
    state = booking_com_popup_state(event, worker_status=status)
    assert not state.ready and state.phase == "loading"
    assert state.title == "Vui lòng đợi lấy thông tin"
    assert "họ tên" in state.message and "hạng phòng" in state.message and "tắt âm" in state.message
    assert "5550000001" not in state.message and "5550000002" not in state.message


@pytest.mark.parametrize(("status", "phase"), [
    ("cần đăng nhập", "login_required"), ("lỗi kết nối", "retrying"), ("arbitrary", "loading"),
])
def test_arbitrary_worker_text_ids_and_secrets_are_never_echoed_or_printed(status, phase, capsys):
    event = replace(complete_event(), room_type="")
    raw = (f"Booking.com 5550000002: {status} https://admin.booking.com/?ses=synthetic-secret "
           "password=synthetic-password otp=654321 Cookie: synthetic-cookie")
    state = booking_com_popup_state(event, worker_status=raw)
    assert state.phase == phase
    output = state.title + state.message
    assert all(secret not in output for secret in
               ("5550000002", "https://", "synthetic-secret", "synthetic-password", "654321", "synthetic-cookie"))
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_popup_state_is_immutable_and_selection_does_not_modify_booking():
    event = complete_event()
    before = event.to_dict()
    state = booking_com_popup_state(event)
    assert isinstance(state, BookingComPopupState) and event.to_dict() == before
    with pytest.raises(FrozenInstanceError):
        state.phase = "loading"

"""Pure, safe display states for Booking.com's asynchronously enriched popup."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .booking_com import canonical_details_url
from .models import BookingEvent


@dataclass(frozen=True, slots=True)
class BookingComPopupState:
    ready: bool
    phase: str
    title: str
    message: str


def booking_com_details_ready(alert: BookingEvent | None) -> bool:
    """Require actual fields, including legacy records without a load timestamp."""
    return bool(alert is not None and alert.checkout_date and all(
        str(value or "").strip() for value in (alert.guest_name, alert.room_type, alert.total_revenue)
    ))


def _normalized_status(value: str) -> str:
    text = unicodedata.normalize("NFD", str(value or "").casefold().replace("đ", "d"))
    return " ".join("".join(char for char in text if not unicodedata.combining(char)).split())


def booking_com_popup_state(
    alert: BookingEvent | None, enrichment_enabled: bool = True, worker_status: str = "",
) -> BookingComPopupState:
    """Only classify status; never echo IDs, sessions, OTPs or arbitrary worker text."""
    if booking_com_details_ready(alert):
        return BookingComPopupState(
            True, "ready", "Đã lấy đủ thông tin",
            "Thông tin đã đầy đủ. Bạn có thể sao chép sang Excel.",
        )
    if not enrichment_enabled:
        return BookingComPopupState(
            False, "disabled", "Chưa bật lấy chi tiết",
            "Vào Cài đặt và bật tự bổ sung chi tiết Booking.com để lấy đầy đủ thông tin.",
        )
    if alert is None or not canonical_details_url(alert.details_url, alert.booking_id):
        return BookingComPopupState(
            False, "unavailable", "Chưa có liên kết chi tiết",
            "Email không có liên kết chi tiết hợp lệ. App chưa thể tự bổ sung; hãy mở booking trong Extranet Booking.com để xem thông tin.",
        )
    status = _normalized_status(worker_status)
    browser_failure = any(phrase in status for phrase in (
        "khong mo duoc edge/chrome", "chua tao duoc cua so nen an toan",
    ))
    if browser_failure:
        return BookingComPopupState(
            False, "login_required", "Chưa mở được trình duyệt",
            "Kiểm tra Edge/Chrome trên máy, rồi vào Cài đặt → Đăng nhập Booking.com để thử lại và hoàn tất OTP. Email vẫn báo bình thường.",
        )
    if any(phrase in status for phrase in ("can dang nhap", "hoan tat dang nhap/2fa trong cua so")):
        return BookingComPopupState(
            False, "login_required", "Cần đăng nhập Booking.com",
            "Vào Cài đặt → Đăng nhập Booking.com để hoàn tất đăng nhập/OTP. Chi tiết sẽ tự bổ sung sau đó.",
        )
    retry_phrases = (
        "chua lay duoc chi tiet", "khong lay duoc chi tiet", "chua lay duoc day du thong tin",
        "chua lay duoc thong tin", "phien trinh duyet can kiem tra",
        "loi ket noi", "mat ket noi", "ket noi that bai", "khong ket noi", "khong the ket noi",
        "connection error", "connection failed", "connection failure", "network error",
        "timed out", "timeout", "disconnected",
    )
    if any(phrase in status for phrase in retry_phrases):
        return BookingComPopupState(
            False, "retrying", "Đang thử lấy lại thông tin",
            "Chưa lấy được đầy đủ thông tin. App sẽ tự thử lại; bạn có thể tắt âm trong lúc chờ.",
        )
    return BookingComPopupState(
        False, "loading", "Vui lòng đợi lấy thông tin",
        "App đang lấy họ tên đầy đủ, hạng phòng, ngày trả và tổng tiền. Bạn có thể tắt âm trong lúc chờ.",
    )

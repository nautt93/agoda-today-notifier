from __future__ import annotations

import hashlib
import imaplib
import logging
import queue
import socket
import ssl
import threading
from datetime import date, datetime, timedelta
from email import message_from_bytes, policy
from typing import Any

from .models import BOOKING_STATUS_NEW
from .parsing import is_trusted_booking_sender, parse_booking_message
from .state import PARSER_STATE_VERSION, StateStore

LOGGER = logging.getLogger(__name__)
RECENT_MESSAGE_LIMIT = 500  # Same bounded inbox window as 1.5.5.


def is_quiet_hours(when: datetime | None = None, start_hour: int = 0, end_hour: int = 8) -> bool:
    current = when or datetime.now()
    return start_hour <= current.hour < end_hour


def mailbox_uid_key(identity_hash: str, uid_validity: str, uid: str) -> str:
    return f"uid:{PARSER_STATE_VERSION}:{identity_hash}:{uid_validity}:{uid}"


def mailbox_message_key(identity_hash: str, message_id: str) -> str:
    digest = hashlib.sha256(message_id.encode("utf-8", errors="replace")).hexdigest()
    return f"msg:{PARSER_STATE_VERSION}:{identity_hash}:{digest}"


def lifecycle_affects_today(affected_dates: set[date], today: date | None = None) -> bool:
    return (today or date.today()) in affected_dates


def _response_bytes(payload: Any) -> bytes | None:
    if not payload:
        return None
    for item in payload:
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
            return item[1]
        if isinstance(item, bytes) and b"\r\n" in item:
            return item
    return None


class ImapMonitor(threading.Thread):
    def __init__(
        self,
        config: dict[str, Any],
        password: str,
        state: StateStore,
        event_queue: queue.Queue[tuple[str, Any]],
    ) -> None:
        super().__init__(name="ImapMonitor", daemon=True)
        self.config = dict(config)
        self.password = password
        self.state = state
        self.event_queue = event_queue
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        identity = f"{self.config.get('imap_host', '')}|{self.config.get('email_address', '')}|INBOX".lower()
        self.identity_hash = hashlib.sha256(identity.encode()).hexdigest()[:20]

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()

    def check_now(self) -> None:
        self.wake_event.set()

    def emit(self, event_type: str, payload: Any) -> None:
        self.event_queue.put((event_type, payload))

    def run(self) -> None:
        self.emit("status", "Đang theo dõi email Agoda + Expedia")
        while not self.stop_event.is_set():
            try:
                processed = self.scan_mailbox()
                if processed:
                    self.emit("log", f"Đã đọc {processed} email mới.")
                self.emit("status", "Đang theo dõi email Agoda + Expedia")
            except Exception as exc:
                LOGGER.exception("IMAP scan failed")
                self.emit("error", friendly_error(exc))
            delay = min(3600, max(30, int(self.config.get("poll_seconds", 60))))
            self.wake_event.wait(delay)
            self.wake_event.clear()
        self.emit("status", "Đã dừng theo dõi")

    def scan_mailbox(self) -> int:
        host = str(self.config["imap_host"]).strip()
        port = int(self.config.get("imap_port", 993))
        address = str(self.config["email_address"]).strip()
        scan_days = min(365, max(1, int(self.config.get("scan_days", 90))))
        since_date = date.today() - timedelta(days=scan_days)
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        since = f"{since_date.day:02d}-{months[since_date.month - 1]}-{since_date.year}"
        tls_context = ssl.create_default_context()
        processed_count = 0
        failed_count = 0
        today_count = 0
        other_day_count = 0
        self.emit("status", "Đang kiểm tra thư mới nhất Agoda + Expedia…")
        with imaplib.IMAP4_SSL(host, port, ssl_context=tls_context, timeout=30) as client:
            client.login(address, self.password)
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("Không mở được Inbox")
            uid_validity = "unknown"
            try:
                _, values = client.response("UIDVALIDITY")
                if values and values[0]:
                    raw_value = values[0]
                    uid_validity = raw_value.decode("ascii", errors="ignore") if isinstance(raw_value, bytes) else str(raw_value)
            except Exception:
                pass
            # 1.5.5 searches recent UIDs, then fetches only the newest 500 messages.
            # Do not download the whole mailbox or defer alerts until a history replay finishes.
            status, data = client.uid("search", None, "SINCE", since)
            if status != "OK" or not data:
                raise RuntimeError("Không tìm được email trong Inbox")
            uids = sorted(data[0].split(), key=int)[-RECENT_MESSAGE_LIMIT:]
            self.emit("log", f"Chỉ kiểm tra {len(uids)} thư gần nhất (tối đa {RECENT_MESSAGE_LIMIT}); đọc thư mới trước.")
            for uid_raw in reversed(uids):
                if self.stop_event.is_set():
                    break
                uid = uid_raw.decode("ascii", errors="ignore")
                uid_key = mailbox_uid_key(self.identity_hash, uid_validity, uid)
                if self.state.is_processed(uid_key):
                    continue
                try:
                    # Direct body fetch matches 1.5.5 and avoids a second round-trip/header gate.
                    status, payload = client.uid("fetch", uid_raw, "(BODY.PEEK[])")
                except imaplib.IMAP4.abort:
                    raise
                except Exception:
                    failed_count += 1
                    LOGGER.exception("Cannot fetch email UID %s", uid)
                    self.state.record_parse_failure(uid_key, "Không tải được nội dung email")
                    self.emit("log", f"Email UID {uid}: tải lỗi; sẽ thử lại, tiếp tục thư khác.")
                    continue
                raw = _response_bytes(payload) if status == "OK" else None
                if not raw:
                    failed_count += 1
                    self.state.record_parse_failure(uid_key, "Không tải được nội dung email")
                    continue
                try:
                    message = message_from_bytes(raw, policy=policy.default)
                    sender = str(message.get("From", ""))
                    message_id = str(message.get("Message-ID", "")).strip()
                except Exception:
                    failed_count += 1
                    self.state.record_parse_failure(uid_key, "Không đọc được email")
                    continue
                if not message_id:
                    message_id = hashlib.sha256(raw).hexdigest()
                message_key = mailbox_message_key(self.identity_hash, message_id)
                if self.state.is_processed(message_key):
                    # This message was copied/moved to a new UID; remember the alias to avoid downloading it again.
                    self.state.remember_processed_aliases(uid_key)
                    continue
                if not is_trusted_booking_sender(sender):
                    self.state.remember_processed_aliases(uid_key, message_key)
                    processed_count += 1
                    continue
                try:
                    event = parse_booking_message(message)
                except Exception:
                    LOGGER.exception("Cannot parse booking email UID %s", uid)
                    failed_count += 1
                    self.state.record_parse_failure(message_key, "Lỗi đọc nội dung email")
                    self.emit("log", f"Email UID {uid}: lỗi đọc nội dung; đang tiếp tục email khác.")
                    continue
                if event is None:
                    failed_count += 1
                    attempts = self.state.record_parse_failure(message_key, "Mẫu email booking chưa đọc đủ dữ liệu")
                    if attempts in {1, 5, 20}:
                        self.emit("log", f"Email UID {uid}: chưa đọc đủ mã booking/ngày check-in hoặc chưa nhận diện được mẫu xác nhận; sẽ thử lại.")
                    continue
                processed_count += 1
                # Restore the 1.5.5 contract: only NEW confirmations arriving TODAY alert.
                # Do not retain future arrivals for automatic reminders or replay lifecycle events.
                if event.status != BOOKING_STATUS_NEW:
                    self.state.remember_processed_aliases(uid_key, message_key)
                    continue
                today = date.today()
                if event.checkin_date != today:
                    other_day_count += 1
                    self.state.remember_processed_aliases(uid_key, message_key)
                    continue
                alert = self.state.register_today_confirmation(event, (uid_key, message_key), today)
                if alert is None:
                    self.emit("log", f"{event.source} {event.booking_id or '(không có mã)'}: đã ghi nhận; không báo lặp.")
                    continue
                today_count += 1
                self.emit("log", f"BÁO NGAY {alert.source} {alert.booking_id or '(không có mã)'}: check-in {today.isoformat()}.")
                if self.config.get("quiet_hours_enabled", True) and is_quiet_hours():
                    self.emit("deferred_alert", alert)
                else:
                    self.emit("alert", alert)
                self.emit("history_changed", None)
        if processed_count:
            self.emit("history_changed", None)
        self.emit("log", f"Quét xong: {processed_count} thư mới; {today_count} booking hôm nay được ghi nhận; "
                  f"{other_day_count} booking ngày khác bỏ qua; {failed_count} thư đọc lỗi.")
        return processed_count


def test_imap_connection(config: dict[str, Any], password: str) -> None:
    context = ssl.create_default_context()
    with imaplib.IMAP4_SSL(
        str(config["imap_host"]).strip(),
        int(config.get("imap_port", 993)),
        ssl_context=context,
        timeout=30,
    ) as client:
        client.login(str(config["email_address"]).strip(), password)
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Không mở được Inbox")


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, imaplib.IMAP4.error):
        return "Đăng nhập IMAP thất bại. Hãy kiểm tra email, mật khẩu ứng dụng và quyền IMAP."
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "Chứng chỉ bảo mật của máy chủ IMAP không hợp lệ; kết nối đã bị chặn."
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Máy chủ email không phản hồi trong 30 giây."
    if isinstance(exc, OSError):
        return f"Không kết nối được máy chủ email: {exc}"
    return str(exc)

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

from .models import BOOKING_STATUS_CANCELLED, BOOKING_STATUS_MODIFIED
from .parsing import is_trusted_booking_sender, parse_booking_message
from .state import PARSER_STATE_VERSION, StateStore

LOGGER = logging.getLogger(__name__)


def is_quiet_hours(when: datetime | None = None, start_hour: int = 0, end_hour: int = 8) -> bool:
    current = when or datetime.now()
    return start_hour <= current.hour < end_hour


def mailbox_uid_key(identity_hash: str, uid_validity: str, uid: str) -> str:
    return f"uid:{PARSER_STATE_VERSION}:{identity_hash}:{uid_validity}:{uid}"


def mailbox_message_key(identity_hash: str, message_id: str) -> str:
    digest = hashlib.sha256(message_id.encode("utf-8", errors="replace")).hexdigest()
    return f"msg:{PARSER_STATE_VERSION}:{identity_hash}:{digest}"


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
                    self.emit("log", f"Đã xử lý {processed} email booking mới/cập nhật.")
                self._queue_due_alerts()
                self.emit("status", "Đang theo dõi email Agoda + Expedia")
            except Exception as exc:
                LOGGER.exception("IMAP scan failed")
                self.emit("error", friendly_error(exc))
            delay = min(3600, max(30, int(self.config.get("poll_seconds", 60))))
            self.wake_event.wait(delay)
            self.wake_event.clear()
        self.emit("status", "Đã dừng theo dõi")

    def _queue_due_alerts(self) -> None:
        for alert in self.state.queue_due_alerts(date.today()):
            if self.config.get("quiet_hours_enabled", True) and is_quiet_hours():
                self.emit("deferred_alert", alert)
            else:
                self.emit("alert", alert)

    def scan_mailbox(self) -> int:
        host = str(self.config["imap_host"]).strip()
        port = int(self.config.get("imap_port", 993))
        address = str(self.config["email_address"]).strip()
        scan_days = min(365, max(1, int(self.config.get("scan_days", 90))))
        since = (date.today() - timedelta(days=scan_days)).strftime("%d-%b-%Y")
        tls_context = ssl.create_default_context()
        processed_count = 0
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
            # Ask the server for likely senders. Fall back to all headers if its SEARCH parser differs.
            status, data = client.uid(
                "search", None, "SINCE", since, "OR", "FROM", '"agoda"', "FROM", '"expedia"'
            )
            if status != "OK":
                status, data = client.uid("search", None, "SINCE", since)
            if status != "OK" or not data:
                raise RuntimeError("Không tìm được email trong Inbox")
            uids = data[0].split()
            for uid_raw in uids:
                if self.stop_event.is_set():
                    break
                uid = uid_raw.decode("ascii", errors="ignore")
                uid_key = mailbox_uid_key(self.identity_hash, uid_validity, uid)
                if self.state.is_processed(uid_key):
                    continue
                status, payload = client.uid(
                    "fetch", uid_raw,
                    "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID DATE)] RFC822.SIZE)",
                )
                header_bytes = _response_bytes(payload) if status == "OK" else None
                if not header_bytes:
                    # A transient/partial fetch must be retried; never consume the UID.
                    self.state.record_parse_failure(uid_key, "Không tải được header email")
                    continue
                header = message_from_bytes(header_bytes, policy=policy.default)
                sender = str(header.get("From", ""))
                if not is_trusted_booking_sender(sender):
                    continue
                message_id = str(header.get("Message-ID", "")).strip()
                status, payload = client.uid("fetch", uid_raw, "(BODY.PEEK[])")
                raw = _response_bytes(payload) if status == "OK" else None
                if not raw:
                    self.state.record_parse_failure(uid_key, "Không tải được nội dung email")
                    continue
                if not message_id:
                    message_id = hashlib.sha256(raw).hexdigest()
                message_key = mailbox_message_key(self.identity_hash, message_id)
                if self.state.is_processed(message_key):
                    # This message was copied/moved to a new UID; remember the alias to avoid downloading it again.
                    self.state.remember_processed_aliases(uid_key)
                    continue
                message = message_from_bytes(raw, policy=policy.default)
                event = parse_booking_message(message)
                if event is None:
                    attempts = self.state.record_parse_failure(message_key, "Mẫu email booking chưa đọc đủ dữ liệu")
                    if attempts in {1, 5, 20}:
                        self.emit("log", "Có email từ kênh booking chưa đọc được; app sẽ tự thử lại.")
                    continue
                self.state.apply_event(event, (uid_key, message_key))
                processed_count += 1
                if event.status == BOOKING_STATUS_CANCELLED:
                    self.emit("log", f"Đã hủy booking {event.source} {event.booking_id}; xóa cảnh báo chờ.")
                    self.emit("booking_cancelled", event)
                elif event.status == BOOKING_STATUS_MODIFIED:
                    self.emit("log", f"Đã cập nhật booking {event.source} {event.booking_id}.")
                    self.emit("booking_modified", event)
        self._queue_due_alerts()
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

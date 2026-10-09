from __future__ import annotations

import hashlib
import imaplib
import logging
import queue
import re
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from email import message_from_bytes, policy
from typing import Any

from .booking_com import is_booking_com_nonbooking_notice
from .models import BOOKING_STATUS_NEW
from .parsing import is_traveloka_payment_notice, is_trusted_booking_sender, parse_booking_message
from .quiet_hours import normalize_quiet_times, quiet_hours_active
from .state import PARSER_STATE_VERSION, StateStore

LOGGER = logging.getLogger(__name__)
RECENT_MESSAGE_LIMIT = 20  # Bootstrap only; subsequent scans read every new UID.
DETAIL_REPAIR_RETRY_SECONDS = 15 * 60
DETAIL_REPAIR_MESSAGE_LIMIT = 3


class ImapAuthenticationError(imaplib.IMAP4.error):
    """A rejected LOGIN, distinct from a disconnected or failed mailbox command."""


def _login(client: Any, address: str, password: str) -> None:
    try:
        client.login(address, password)
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error as exc:
        raise ImapAuthenticationError("IMAP LOGIN rejected") from exc


@contextmanager
def _imap_connection(host: str, port: int, tls_context: ssl.SSLContext):
    """Keep LOGIN/scan results intact if the server disconnects during LOGOUT."""
    completed = False
    operation_error: BaseException | None = None
    try:
        with imaplib.IMAP4_SSL(host, port, ssl_context=tls_context, timeout=30) as client:
            try:
                yield client
            except BaseException as exc:
                operation_error = exc
                raise
            completed = True
    except (imaplib.IMAP4.error, OSError):
        if operation_error is not None:
            # A LOGOUT error must not obscure a genuine rejected LOGIN.
            raise operation_error from None
        if not completed:
            raise
        # Both scanning and "Kiểm tra IMAP" completed before LOGOUT. The server
        # closing during cleanup does not undo those successful operations.
        LOGGER.debug("IMAP connection closed during logout after completed operation")


def _selected_mailbox_count(status: str, counts: Any) -> int:
    if status != "OK":
        raise RuntimeError("Không mở được Inbox; app sẽ thử kết nối lại.")
    raw_count = counts[0] if isinstance(counts, (list, tuple)) and counts else None
    try:
        count = raw_count.decode("ascii") if isinstance(raw_count, bytes) else str(raw_count)
    except UnicodeDecodeError as exc:
        raise RuntimeError("Máy chủ IMAP trả số thư không hợp lệ; chưa thay đổi mốc đọc thư.") from exc
    if not count.isascii() or not count.isdigit():
        raise RuntimeError("Máy chủ IMAP trả số thư không hợp lệ; chưa thay đổi mốc đọc thư.")
    return int(count)


def _search_uids(status: str, data: Any, error_message: str) -> list[int]:
    if status != "OK" or not isinstance(data, (list, tuple)) or not data or data[0] is None:
        raise RuntimeError(error_message)
    raw = data[0]
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RuntimeError(error_message) from exc
    if not isinstance(raw, str):
        raise RuntimeError(error_message)
    tokens = raw.split()
    if any(not token.isascii() or not token.isdigit() or not 0 < int(token) <= 0xFFFFFFFF for token in tokens):
        raise RuntimeError(error_message)
    return sorted({int(token) for token in tokens})


def is_quiet_hours(when: datetime | None = None, start_hour: int = 0, end_hour: int = 8) -> bool:
    """Compatibility API for callers that supplied whole-hour boundaries."""
    return quiet_hours_active({"quiet_hours_enabled": True, "quiet_start_time": f"{start_hour:02d}:00",
                               "quiet_end_time": f"{end_hour:02d}:00"}, when)


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
        self.repair_requested = threading.Event()
        self.detail_repair_attempts: dict[str, float] = {}
        self._session_lock = threading.Lock()
        self._active_client: Any = None
        identity = f"{self.config.get('imap_host', '')}|{self.config.get('email_address', '')}|INBOX".lower()
        self.identity_hash = hashlib.sha256(identity.encode()).hexdigest()[:20]

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        # A blocked FETCH otherwise holds the old worker until its 30-second timeout.
        # Shutdown wakes its read without sending a concurrent IMAP command from the UI.
        with self._session_lock:
            active_socket = getattr(self._active_client, "sock", None)
        if active_socket is not None:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    @contextmanager
    def _imap_session(self, host: str, port: int, tls_context: ssl.SSLContext):
        try:
            with _imap_connection(host, port, tls_context) as client:
                with self._session_lock:
                    self._active_client = client
                yield client
        finally:
            with self._session_lock:
                self._active_client = None

    def check_now(self) -> None:
        self.repair_requested.set()
        self.wake_event.set()

    def configure_quiet_hours(self, config: dict[str, Any]) -> None:
        """Replace a complete quiet-hours snapshot without restarting IMAP."""
        start, end = normalize_quiet_times(config.get("quiet_start_time", "00:00"),
                                           config.get("quiet_end_time", "08:00"))
        self.config = {**self.config, "quiet_hours_enabled": bool(config.get("quiet_hours_enabled", True)),
                       "quiet_start_time": start, "quiet_end_time": end}

    def emit(self, event_type: str, payload: Any) -> None:
        self.event_queue.put((event_type, payload))

    def run(self) -> None:
        self.emit("status", "Đang theo dõi 5 nguồn booking")
        while not self.stop_event.is_set():
            try:
                processed = self.scan_mailbox()
                if processed:
                    self.emit("log", f"Đã đọc {processed} email mới.")
                self.emit("status", "Đang theo dõi 5 nguồn booking")
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                LOGGER.exception("IMAP scan failed")
                self.emit("error", friendly_error(exc))
            delay = min(3600, max(30, int(self.config.get("poll_seconds", 60))))
            self.wake_event.wait(delay)
            self.wake_event.clear()
        self.emit("status", "Đã dừng theo dõi")

    def scan_mailbox(self) -> int:
        if self.stop_event.is_set():
            return 0
        host = str(self.config["imap_host"]).strip()
        port = int(self.config.get("imap_port", 993))
        address = str(self.config["email_address"]).strip()
        tls_context = ssl.create_default_context()
        processed_count = 0
        failed_count = 0
        today_count = 0
        other_day_count = 0
        self.emit("status", "Đang kiểm tra email booking mới nhất…")
        if self.repair_requested.is_set():
            self.detail_repair_attempts.clear()
            self.repair_requested.clear()
        # Snapshot BEFORE new mail: do not immediately fetch a new email twice.
        incomplete = [record for record in self.state.incomplete_confirmations(date.today())
                      if str(record.get("source", "")).lower() != "booking.com"]
        read_uids: set[int] = set()
        with self._imap_session(host, port, tls_context) as client:
            if self.stop_event.is_set():
                return 0
            _login(client, address, self.password)
            status, counts = client.select("INBOX", readonly=True)
            count = _selected_mailbox_count(status, counts)
            uid_validity = ""
            try:
                _, values = client.response("UIDVALIDITY")
                if values and values[0]:
                    raw_value = values[0]
                    uid_validity = raw_value.decode("ascii", errors="ignore") if isinstance(raw_value, bytes) else str(raw_value)
            except Exception:
                pass
            if not uid_validity.isdigit():
                raise RuntimeError("Máy chủ không trả UIDVALIDITY; chưa thể lưu mốc đọc thư an toàn.")
            # Reading position must survive parser upgrades and retain offline arrivals.
            mailbox_key = f"incremental:v1:{self.identity_hash}:{uid_validity}"
            position = self.state.mailbox_read_position(mailbox_key)
            if position is None:
                position = self.state.mailbox_read_position(f"incremental:p7:{self.identity_hash}:{uid_validity}")
            refresh_details = position is not None and self.state.mailbox_parser_version(mailbox_key) != PARSER_STATE_VERSION
            recent_criteria = (f"{max(1, count - RECENT_MESSAGE_LIMIT + 1)}:*",) if count else ("ALL",)
            if position is None:
                # SEARCH sequence range returns UIDs for only the last 20 entries,
                # not a list of the entire mailbox. Empty inbox may receive mail meanwhile.
                criteria = recent_criteria
            else:
                criteria = ("UID", f"{position[0] + 1}:*")
            status, data = client.uid("search", None, *criteria)
            found = _search_uids(status, data, "Không tìm được email trong Inbox; app sẽ thử lại.")
            # IMAP n:* also matches the last UID when n is greater than that UID.
            uids = found[-RECENT_MESSAGE_LIMIT:] if position is None else [uid for uid in found if uid > position[0]]
            new_count = len(uids)
            if refresh_details:
                status, recent_data = client.uid("search", None, *recent_criteria)
                recent_uids = _search_uids(status, recent_data,
                                          "Không đọc được thư gần nhất để bổ sung tên khách/hạng phòng.")[-RECENT_MESSAGE_LIMIT:]
                uids = sorted(set(uids) | set(recent_uids))
                self.emit("log", f"Nâng cấp parser: kiểm tra nguồn mới và bổ sung tên khách/hạng phòng từ tối đa {RECENT_MESSAGE_LIMIT} thư gần nhất; không báo lặp booking đã đóng.")
            # Preserve both the old cursor and unfinished batch during the 1.7.5 migration.
            work = sorted(set(uids) | set(position[1] if position else []))
            pending = self.state.stage_mailbox_reads(
                mailbox_key, work, minimum_cursor=position[0] if position else 0, parser_version=PARSER_STATE_VERSION,
            )
            if position is None:
                self.emit("log", f"Lần đầu: kiểm tra {len(uids)} thư gần nhất (tối đa {RECENT_MESSAGE_LIMIT}).")
            else:
                self.emit("log", f"Có {new_count} thư mới; {len(pending) - new_count} thư cần đọc lại/bổ sung dữ liệu.")
            for uid_number in pending:
                if self.stop_event.is_set():
                    break
                uid_raw = str(uid_number).encode("ascii")
                uid = uid_raw.decode("ascii", errors="ignore")
                uid_key = mailbox_uid_key(self.identity_hash, uid_validity, uid)
                if self.state.is_processed(uid_key):
                    self.state.finish_mailbox_read(mailbox_key, uid_number)
                    continue
                try:
                    # Direct body fetch matches 1.5.5 and avoids a second round-trip/header gate.
                    read_uids.add(uid_number)
                    status, payload = client.uid("fetch", uid_raw, "(BODY.PEEK[])")
                except (imaplib.IMAP4.abort, OSError):
                    raise
                except Exception:
                    failed_count += 1
                    LOGGER.exception("Cannot fetch email UID %s", uid)
                    self.state.record_parse_failure(uid_key, "Không tải được nội dung email")
                    self.emit("log", f"Email UID {uid}: tải lỗi; sẽ thử lại, tiếp tục thư khác.")
                    continue
                raw = _response_bytes(payload) if status == "OK" else None
                if not raw:
                    if status == "OK":
                        # Expunged/moved UIDs return FETCH OK with no body. Confirm
                        # absence before removing a durable retry, so server errors
                        # cannot silently drop a still-existing reservation email.
                        exists_status, exists_data = client.uid("search", None, "UID", uid)
                        existing = _search_uids(exists_status, exists_data,
                                                "Không xác minh được email chưa tải; app sẽ thử lại.")
                        if uid_number not in existing:
                            self.state.finish_mailbox_read(mailbox_key, uid_number)
                            self.emit("log", f"Email UID {uid}: đã bị xóa/chuyển khỏi Inbox; bỏ mốc đọc lại.")
                            continue
                    failed_count += 1
                    self.state.record_parse_failure(uid_key, "Không tải được nội dung email")
                    continue
                try:
                    message = message_from_bytes(raw, policy=policy.default)
                    sender = message.get("From", "")
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
                    self.state.finish_mailbox_read(mailbox_key, uid_number)
                    continue
                if len(message.get_all("From", [])) != 1 or not is_trusted_booking_sender(sender):
                    self.state.remember_processed_aliases(uid_key, message_key)
                    self.state.finish_mailbox_read(mailbox_key, uid_number)
                    processed_count += 1
                    continue
                if is_traveloka_payment_notice(message):
                    # A recognized receipt is not a failed booking template.
                    # Finish once, so it cannot become a popup or be retried forever.
                    self.state.remember_processed_aliases(uid_key, message_key)
                    self.state.finish_mailbox_read(mailbox_key, uid_number)
                    processed_count += 1
                    continue
                if is_booking_com_nonbooking_notice(message):
                    # Explicit newsletters/payment reminders/review notices are
                    # not malformed confirmations. Unknown booking subjects still retry.
                    self.state.remember_processed_aliases(uid_key, message_key)
                    self.state.finish_mailbox_read(mailbox_key, uid_number)
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
                    self.state.finish_mailbox_read(mailbox_key, uid_number)
                    continue
                today = date.today()
                if event.checkin_date != today:
                    if self.state.repair_known_confirmation(event):
                        self.emit("log", f"Đã bổ sung tên khách/hạng phòng cho {event.source} {event.booking_id}; không phát cảnh báo ngày khác.")
                    other_day_count += 1
                    self.state.remember_processed_aliases(uid_key, message_key)
                    self.state.finish_mailbox_read(mailbox_key, uid_number)
                    continue
                alert = self.state.register_today_confirmation(event, (uid_key, message_key), today)
                self.state.finish_mailbox_read(mailbox_key, uid_number)
                if alert is None:
                    self.emit("log", f"{event.source} {event.booking_id or '(không có mã)'}: đã ghi nhận; không báo lặp.")
                    continue
                today_count += 1
                self.emit("log", f"BÁO NGAY {alert.source} {alert.booking_id or '(không có mã)'}: check-in {today.isoformat()}.")
                if quiet_hours_active(self.config):
                    self.emit("deferred_alert", alert)
                else:
                    self.emit("alert", alert)
                self.emit("history_changed", None)
            self._recover_incomplete_details(client, incomplete, read_uids)
        if processed_count:
            self.emit("history_changed", None)
        self.emit("log", f"Quét xong: {processed_count} thư mới; {today_count} booking hôm nay được ghi nhận; "
                  f"{other_day_count} booking ngày khác bỏ qua; {failed_count} thư đọc lỗi.")
        return processed_count

    def _recover_incomplete_details(
        self, client: Any, candidates: list[dict[str, Any]], read_uids: set[int],
    ) -> None:
        """Repair known cached rows outside the recent window; never queue an alert."""
        today = date.today()
        remaining = {StateStore._record_storage_id(record) for record in self.state.incomplete_confirmations(today)}
        for record in candidates:
            if self.stop_event.is_set():
                break
            storage_id = StateStore._record_storage_id(record)
            booking_id = str(record.get("booking_id", ""))
            if storage_id not in remaining or not re.fullmatch(r"[A-Za-z0-9-]{4,40}", booking_id):
                continue
            retry_key = f"{today.isoformat()}:{storage_id}"
            now = time.monotonic()
            last_attempt = self.detail_repair_attempts.get(retry_key)
            if last_attempt is not None and now - last_attempt < DETAIL_REPAIR_RETRY_SECONDS:
                continue
            self.detail_repair_attempts[retry_key] = now
            source = str(record.get("source", "Agoda"))
            if source.lower() == "booking.com":
                continue
            try:
                # Server-side lookup, not downloading/scanning 500 email bodies.
                criteria = ("HEADER", "Subject", f'"{booking_id}"') if source.lower() == "agoda" else ("TEXT", f'"{booking_id}"')
                status, data = client.uid("search", None, *criteria)
                if status != "OK" or not data or data[0] is None:
                    self.emit("log", f"Chưa tìm được email gốc để bổ sung {source} {booking_id}; sẽ thử lại.")
                    continue
                matches = sorted({int(uid) for uid in data[0].split()}, reverse=True)[:DETAIL_REPAIR_MESSAGE_LIMIT]
                changed = False
                for uid in matches:
                    if uid in read_uids or self.stop_event.is_set():
                        continue
                    status, payload = client.uid("fetch", str(uid).encode("ascii"), "(BODY.PEEK[])")
                    raw = _response_bytes(payload) if status == "OK" else None
                    if not raw:
                        continue
                    message = message_from_bytes(raw, policy=policy.default)
                    if not is_trusted_booking_sender(message.get("From", "")):
                        continue
                    event = parse_booking_message(message)
                    if (event is None or event.status != BOOKING_STATUS_NEW
                            or event.source.lower() != source.lower() or event.booking_id != booking_id
                            or event.checkin_date != today):
                        continue
                    changed = self.state.repair_known_confirmation(event) or changed
                if changed:
                    self.emit("history_changed", None)
                    self.emit("log", f"Đã bổ sung họ tên/hạng phòng từ email gốc của {source} {booking_id}; không báo lặp.")
                else:
                    self.emit("log", f"{source} {booking_id}: chưa bổ sung được dữ liệu; cần kiểm tra email gốc.")
            except (imaplib.IMAP4.abort, OSError):
                raise
            except Exception:
                LOGGER.exception("Cannot recover cached booking %s", booking_id)
                self.emit("log", f"Không đọc lại được email gốc của {source} {booking_id}; thư mới vẫn đã được xử lý.")


def test_imap_connection(config: dict[str, Any], password: str) -> None:
    context = ssl.create_default_context()
    with _imap_connection(
        str(config["imap_host"]).strip(),
        int(config.get("imap_port", 993)),
        context,
    ) as client:
        _login(client, str(config["email_address"]).strip(), password)
        status, counts = client.select("INBOX", readonly=True)
        _selected_mailbox_count(status, counts)


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, imaplib.IMAP4.readonly):
        return "Phiên Inbox đã thay đổi; app sẽ kết nối lại để đọc thư mới."
    if isinstance(exc, imaplib.IMAP4.abort):
        return "Kết nối IMAP bị ngắt; app sẽ tự kết nối lại, không mất mốc đọc thư."
    if isinstance(exc, ImapAuthenticationError):
        return "Đăng nhập IMAP thất bại. Hãy kiểm tra email, mật khẩu ứng dụng và quyền IMAP."
    if isinstance(exc, imaplib.IMAP4.error):
        return "Máy chủ IMAP từ chối thao tác đọc thư; app sẽ thử kết nối lại."
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "Chứng chỉ bảo mật của máy chủ IMAP không hợp lệ; kết nối đã bị chặn."
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Máy chủ email không phản hồi trong 30 giây."
    if isinstance(exc, OSError):
        return f"Không kết nối được máy chủ email: {exc}"
    return str(exc)

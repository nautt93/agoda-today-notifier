from __future__ import annotations

import imaplib
import queue
import socket
import threading
from datetime import date

import pytest

from booking_notifier import mail_monitor
from booking_notifier.state import StateStore

CONFIG = {"imap_host": "imap.example.invalid", "email_address": "hotel@example.invalid",
          "quiet_hours_enabled": False}
RAW_MESSAGE = b"From: friend@example.invalid\r\nSubject: Hello\r\n\r\nHello"


def make_monitor(tmp_path):
    events = queue.Queue()
    state = StateStore(tmp_path / "state.json")
    return mail_monitor.ImapMonitor(CONFIG, "test-password", state, events), state, events


def fake_session(monkeypatch, *, count=b"1", select_status="OK", search_data=b"1", login_error=None,
                 fetch=None, exact_search=None, logout_error=None):
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            if logout_error is not None:
                raise logout_error

        def login(self, *args):
            if login_error is not None:
                raise login_error

        def select(self, *args, **kwargs):
            return select_status, count if isinstance(count, list) else [count]

        def response(self, *args):
            return "OK", [b"123"]

        def uid(self, command, *args):
            calls.append((command, args))
            if command == "search":
                if exact_search is not None and args[1] == "UID" and ":" not in args[2]:
                    return exact_search
                return "OK", [search_data]
            assert command == "fetch"
            if fetch is not None:
                return fetch(int(args[0]))
            return "OK", [(b"BODY[]", RAW_MESSAGE)]

    monkeypatch.setattr(mail_monitor.imaplib, "IMAP4_SSL", Client)
    return calls


@pytest.mark.parametrize("error", [imaplib.IMAP4.abort("socket EOF"), imaplib.IMAP4.readonly("changed")])
def test_connection_loss_during_login_is_not_reported_as_bad_password(tmp_path, monkeypatch, error):
    monitor, _, _ = make_monitor(tmp_path)
    fake_session(monkeypatch, login_error=error)
    with pytest.raises(type(error)) as caught:
        monitor.scan_mailbox()
    explanation = mail_monitor.friendly_error(caught.value)
    assert "kết nối lại" in explanation
    assert "mật khẩu" not in explanation and "Đăng nhập" not in explanation


def test_login_rejection_keeps_credential_guidance_but_mailbox_error_does_not(monkeypatch):
    fake_session(monkeypatch, login_error=imaplib.IMAP4.error("AUTHENTICATIONFAILED"))
    with pytest.raises(mail_monitor.ImapAuthenticationError) as caught:
        mail_monitor.test_imap_connection(CONFIG, "test-password")
    assert "mật khẩu ứng dụng" in mail_monitor.friendly_error(caught.value)
    assert "mật khẩu" not in mail_monitor.friendly_error(imaplib.IMAP4.error("FETCH failed"))


@pytest.mark.parametrize("count", [None, b"", b"unknown", b"-1", b"1\xff", []])
def test_invalid_select_count_never_advances_read_cursor(tmp_path, monkeypatch, count):
    monitor, state, _ = make_monitor(tmp_path)
    calls = fake_session(monkeypatch, count=count)
    with pytest.raises(RuntimeError, match="số thư không hợp lệ"):
        monitor.scan_mailbox()
    assert state.data["mailbox_reads"] == {} and calls == []


def test_rejected_readonly_select_does_not_turn_into_a_credential_error(tmp_path, monkeypatch):
    monitor, state, _ = make_monitor(tmp_path)
    fake_session(monkeypatch, select_status="NO")
    with pytest.raises(RuntimeError, match="Không mở được Inbox") as caught:
        monitor.scan_mailbox()
    assert "Đăng nhập" not in mail_monitor.friendly_error(caught.value)
    assert state.data["mailbox_reads"] == {}


@pytest.mark.parametrize("uids", [b"1 broken 2", b"0", b"-1", b"4294967296", b"\xff", object()])
def test_invalid_search_result_never_stages_or_fetches_uids(tmp_path, monkeypatch, uids):
    monitor, state, _ = make_monitor(tmp_path)
    calls = fake_session(monkeypatch, search_data=uids)
    with pytest.raises(RuntimeError, match="Không tìm được email"):
        monitor.scan_mailbox()
    assert state.data["mailbox_reads"] == {}
    assert [command for command, _ in calls] == ["search"]


def test_expunged_pending_uid_is_verified_and_finished_once(tmp_path, monkeypatch):
    monitor, state, events = make_monitor(tmp_path)
    key = f"incremental:v1:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(key, [7], minimum_cursor=10, parser_version=mail_monitor.PARSER_STATE_VERSION)
    calls = fake_session(monkeypatch, search_data=b"10", fetch=lambda uid: ("OK", [None]),
                         exact_search=("OK", [b""]))
    assert monitor.scan_mailbox() == 0
    assert state.mailbox_read_position(key) == (10, [])
    assert not state.data["parse_failures"]
    assert calls == [("search", (None, "UID", "11:*")), ("fetch", (b"7", "(BODY.PEEK[])")),
                     ("search", (None, "UID", "7"))]
    assert any(kind == "log" and "đã bị xóa/chuyển" in value for kind, value in events.queue)
    calls.clear()
    monitor.scan_mailbox()
    assert calls == [("search", (None, "UID", "11:*"))]


@pytest.mark.parametrize("exact_search", [("OK", [b"7"]), ("NO", [b"server busy"])])
def test_empty_fetch_does_not_drop_existing_or_unverified_uid(tmp_path, monkeypatch, exact_search):
    monitor, state, _ = make_monitor(tmp_path)
    key = f"incremental:v1:{monitor.identity_hash}:123"
    state.stage_mailbox_reads(key, [7], minimum_cursor=10, parser_version=mail_monitor.PARSER_STATE_VERSION)
    fake_session(monkeypatch, search_data=b"10", fetch=lambda uid: ("OK", [None]), exact_search=exact_search)
    if exact_search[0] == "NO":
        with pytest.raises(RuntimeError, match="Không xác minh được"):
            monitor.scan_mailbox()
    else:
        assert monitor.scan_mailbox() == 0
    assert state.mailbox_read_position(key) == (10, [7])
    fake_session(monkeypatch, search_data=b"10")
    assert monitor.scan_mailbox() == 1
    assert state.mailbox_read_position(key) == (10, [])


def test_socket_timeout_abandons_broken_session_but_preserves_entire_batch(tmp_path, monkeypatch):
    monitor, state, _ = make_monitor(tmp_path)

    def timed_out(uid):
        raise TimeoutError("socket timed out")

    calls = fake_session(monkeypatch, count=b"2", search_data=b"1 2", fetch=timed_out)
    with pytest.raises(TimeoutError):
        monitor.scan_mailbox()
    assert [args[0] for command, args in calls if command == "fetch"] == [b"2"]
    key = f"incremental:v1:{monitor.identity_hash}:123"
    assert StateStore(state.path).mailbox_read_position(key) == (2, [1, 2])
    fake_session(monkeypatch, count=b"2", search_data=b"1 2",
                 fetch=lambda uid: ("OK", [(b"BODY[]", RAW_MESSAGE.replace(b"Hello", f"Hello {uid}".encode()))]))
    assert monitor.scan_mailbox() == 2
    assert state.mailbox_read_position(key) == (2, [])


def test_logout_disconnect_does_not_report_failure_after_completed_scan(tmp_path, monkeypatch):
    monitor, state, _ = make_monitor(tmp_path)
    fake_session(monkeypatch, logout_error=imaplib.IMAP4.abort("EOF during LOGOUT"))
    assert monitor.scan_mailbox() == 1
    key = f"incremental:v1:{monitor.identity_hash}:123"
    assert StateStore(state.path).mailbox_read_position(key) == (1, [])
    assert monitor._active_client is None


def test_logout_failure_does_not_hide_an_interrupted_scan(tmp_path, monkeypatch):
    monitor, state, _ = make_monitor(tmp_path)

    def interrupted(uid):
        raise imaplib.IMAP4.abort("EOF during FETCH")

    fake_session(monkeypatch, fetch=interrupted, logout_error=imaplib.IMAP4.abort("EOF during LOGOUT"))
    with pytest.raises(imaplib.IMAP4.abort):
        monitor.scan_mailbox()
    key = f"incremental:v1:{monitor.identity_hash}:123"
    assert StateStore(state.path).mailbox_read_position(key) == (1, [1])
    assert monitor._active_client is None


def test_logout_disconnect_keeps_real_login_rejection_classification(tmp_path, monkeypatch):
    monitor, state, _ = make_monitor(tmp_path)
    fake_session(monkeypatch, login_error=imaplib.IMAP4.error("AUTHENTICATIONFAILED"),
                 logout_error=imaplib.IMAP4.abort("EOF during LOGOUT"))
    with pytest.raises(mail_monitor.ImapAuthenticationError) as caught:
        monitor.scan_mailbox()
    assert "mật khẩu ứng dụng" in mail_monitor.friendly_error(caught.value)
    assert state.data["mailbox_reads"] == {}
    assert monitor._active_client is None


def test_connection_test_success_is_not_undone_by_logout_disconnect(monkeypatch):
    fake_session(monkeypatch, logout_error=imaplib.IMAP4.abort("EOF during LOGOUT"))
    assert mail_monitor.test_imap_connection(CONFIG, "test-password") is None


def test_connection_test_login_rejection_is_not_hidden_by_logout_disconnect(monkeypatch):
    fake_session(monkeypatch, login_error=imaplib.IMAP4.error("AUTHENTICATIONFAILED"),
                 logout_error=imaplib.IMAP4.abort("EOF during LOGOUT"))
    with pytest.raises(mail_monitor.ImapAuthenticationError) as caught:
        mail_monitor.test_imap_connection(CONFIG, "test-password")
    assert "mật khẩu ứng dụng" in mail_monitor.friendly_error(caught.value)


def test_stop_interrupts_blocked_imap_socket_without_error_or_lost_pending_uid(tmp_path, monkeypatch):
    monitor, state, events = make_monitor(tmp_path)
    # Use the real IMAP transport's TCP + 30-second timeout mode. Windows'
    # fully blocking socketpair recv has different cancellation semantics.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client_socket = socket.create_connection(listener.getsockname(), timeout=30)
        peer_socket, _ = listener.accept()
    fetching = threading.Event()

    class Client:
        sock = client_socket

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            client_socket.close()
            peer_socket.close()

        def login(self, *args):
            pass

        def select(self, *args, **kwargs):
            return "OK", [b"1"]

        def response(self, *args):
            return "OK", [b"123"]

        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"1"]
            fetching.set()
            if not self.sock.recv(1):
                raise imaplib.IMAP4.abort("socket EOF")
            raise AssertionError("The fake server must not send a response")

    monkeypatch.setattr(mail_monitor.imaplib, "IMAP4_SSL", Client)
    monitor.start()
    try:
        assert fetching.wait(2), "The worker did not reach its socket read"
        monitor.stop()
        monitor.join(2)
        assert not monitor.is_alive(), "stop() did not wake the blocked IMAP socket"
        key = f"incremental:v1:{monitor.identity_hash}:123"
        assert StateStore(state.path).mailbox_read_position(key) == (1, [1])
        assert not any(kind == "error" for kind, _ in events.queue)
        assert monitor._active_client is None
        assert list(events.queue)[-1] == ("status", "Đã dừng theo dõi")
    finally:
        monitor.stop()
        client_socket.close()
        peer_socket.close()
        monitor.join(2)


def test_stopped_monitor_never_opens_another_connection(tmp_path, monkeypatch):
    monitor, _, _ = make_monitor(tmp_path)
    monitor.stop()
    monkeypatch.setattr(mail_monitor.imaplib, "IMAP4_SSL", lambda *args, **kwargs: pytest.fail("new connection"))
    assert monitor.scan_mailbox() == 0


def test_booking_com_code_only_never_searches_for_missing_guest_or_room(tmp_path):
    monitor, _, events = make_monitor(tmp_path)

    class Client:
        def uid(self, *args):
            pytest.fail("Code-only Booking.com must not perform detail recovery searches")

    record = {"source": "Booking.com", "booking_id": "5741499548", "checkin_date": date.today().isoformat()}
    monitor.state.incomplete_confirmations = lambda today: [record]
    monitor._recover_incomplete_details(Client(), [record], set())
    assert list(events.queue) == []

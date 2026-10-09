from __future__ import annotations

import imaplib
import ipaddress
import queue
import socket
import ssl
import sys
import threading
import time
import traceback
from datetime import UTC, date, datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from booking_notifier import mail_monitor
from booking_notifier.state import StateStore

CONFIG = {"imap_host": "imap.example.invalid", "email_address": "hotel@example.invalid",
          "quiet_hours_enabled": False}
RAW_MESSAGE = b"From: friend@example.invalid\r\nSubject: Hello\r\n\r\nHello"


def make_monitor(tmp_path, **config):
    events = queue.Queue()
    state = StateStore(tmp_path / "state.json")
    return mail_monitor.ImapMonitor({**CONFIG, **config}, "test-password", state, events), state, events


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


@pytest.fixture
def tls_imap_server(tmp_path):
    """Local TLS IMAP server with a synthetic certificate and fragmented FETCH."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(datetime(2020, 1, 1, tzinfo=UTC))
                   .not_valid_after(datetime(2040, 1, 1, tzinfo=UTC))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                                key_encipherment=False, data_encipherment=False,
                                                key_agreement=False, key_cert_sign=True, crl_sign=True,
                                                encipher_only=None, decipher_only=None), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                               x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "synthetic-cert.pem", tmp_path / "synthetic-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                          serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    trusted_context = ssl.create_default_context(cafile=str(cert_path))
    trusted_context.verify_flags |= ssl.VERIFY_X509_STRICT
    fetching = threading.Event()
    release = threading.Event()
    failures = []
    commands = []
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(3)
    threads = []

    def start(*, stalled=True, payload=RAW_MESSAGE):
        def serve():
            try:
                connection, _ = listener.accept()
                connection.settimeout(3)
                with server_context.wrap_socket(connection, server_side=True) as secure, secure.makefile("rb") as stream:
                    secure.sendall(b"* OK Synthetic local IMAP\r\n")
                    while line := stream.readline():
                        tag, command, *_ = line.split()
                        command = command.upper()
                        commands.append(command)
                        if command == b"CAPABILITY":
                            secure.sendall(b"* CAPABILITY IMAP4rev1\r\n" + tag + b" OK capabilities\r\n")
                        elif command == b"LOGIN":
                            secure.sendall(tag + b" OK logged in\r\n")
                        elif command == b"EXAMINE":
                            secure.sendall(b"* 1 EXISTS\r\n* OK [UIDVALIDITY 123] valid\r\n" + tag + b" OK [READ-ONLY] selected\r\n")
                        elif command == b"UID" and b" SEARCH " in line.upper():
                            secure.sendall(b"* SEARCH 1\r\n" + tag + b" OK search\r\n")
                        elif command == b"UID" and b" FETCH " in line.upper():
                            prefix = f"* 1 FETCH (BODY[] {{{len(payload)}}}\r\n".encode()
                            secure.sendall(prefix + payload[:12])
                            fetching.set()
                            if stalled:
                                release.wait(5)
                                return
                            # Pauses span several cancellation polls. They must
                            # not become short I/O timeouts or lose literal bytes.
                            time.sleep(0.25)
                            secure.sendall(payload[12:30])
                            time.sleep(0.25)
                            secure.sendall(payload[30:] + b")\r\n" + tag + b" OK fetched\r\n")
                        elif command == b"LOGOUT":
                            secure.sendall(b"* BYE closing\r\n" + tag + b" OK logout\r\n")
                            return
                        else:
                            raise AssertionError(f"Unexpected synthetic IMAP command {command!r}")
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=serve, daemon=True)
        threads.append(worker)
        worker.start()
        return listener.getsockname()[1]

    try:
        yield start, trusted_context, fetching, release, failures, commands
    finally:
        release.set()
        for worker in threads:
            worker.join(3)
        listener.close()


def _worker_diagnostic(monitor):
    frame = sys._current_frames().get(monitor.ident)
    active = monitor._active_client
    sock = getattr(active, "sock", None)
    metadata = {"client": type(active).__name__, "socket": type(sock).__name__,
                "timeout": sock.gettimeout() if sock is not None else None,
                "stop": monitor.stop_event.is_set()}
    return f"{metadata}\n{''.join(traceback.format_stack(frame)) if frame is not None else 'worker finished'}"


def test_stop_interrupts_blocked_imap_socket_without_error_or_lost_pending_uid(tmp_path, monkeypatch, tls_imap_server):
    start, context, fetching, release, failures, commands = tls_imap_server
    port = start()
    monkeypatch.setattr(mail_monitor.ssl, "create_default_context", lambda: context)
    monitor, state, events = make_monitor(tmp_path, imap_host="127.0.0.1", imap_port=port)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname

    monitor.start()
    try:
        assert fetching.wait(2), f"The worker did not reach FETCH: {failures}; {_worker_diagnostic(monitor)}"
        monitor.stop()
        monitor.join(2)
        assert not monitor.is_alive(), f"stop() did not wake the blocked IMAP socket: {_worker_diagnostic(monitor)}"
        key = f"incremental:v1:{monitor.identity_hash}:123"
        assert StateStore(state.path).mailbox_read_position(key) == (1, [1])
        assert not any(kind == "error" for kind, _ in events.queue)
        assert monitor._active_client is None
        assert list(events.queue)[-1] == ("status", "Đã dừng theo dõi")
    finally:
        monitor.stop()
        release.set()
        monitor.join(2)


@pytest.mark.parametrize("extra_bytes", [0, 32768])
def test_fragmented_tls_fetch_survives_polling_and_preserves_complete_email(tmp_path, monkeypatch, tls_imap_server, extra_bytes):
    start, context, fetching, _, failures, commands = tls_imap_server
    payload = RAW_MESSAGE + b"X" * extra_bytes
    port = start(stalled=False, payload=payload)
    monkeypatch.setattr(mail_monitor.ssl, "create_default_context", lambda: context)
    monitor, state, events = make_monitor(tmp_path, imap_host="127.0.0.1", imap_port=port)
    assert monitor.scan_mailbox() == 1
    assert fetching.is_set() and failures == []
    key = f"incremental:v1:{monitor.identity_hash}:123"
    assert StateStore(state.path).mailbox_read_position(key) == (1, [])
    expected_key = mail_monitor.mailbox_message_key(monitor.identity_hash, mail_monitor.hashlib.sha256(payload).hexdigest())
    assert state.is_processed(expected_key), "Fragmented literal did not match the original complete email"
    assert not state.data["parse_failures"] and not any(kind == "error" for kind, _ in events.queue)
    assert commands[-1] == b"LOGOUT" and monitor._active_client is None


def test_partial_tls_literal_expires_at_command_deadline_and_keeps_uid_pending(tmp_path, monkeypatch, tls_imap_server):
    start, context, fetching, _, _, _ = tls_imap_server
    port = start()
    monkeypatch.setattr(mail_monitor.ssl, "create_default_context", lambda: context)
    command = mail_monitor._CancellableImapTransport._command

    def short_test_deadline(client, name, *args):
        if name == "UID" and args and args[0] == "FETCH":
            client._io_timeout = 0.2
        return command(client, name, *args)

    monkeypatch.setattr(mail_monitor._CancellableImapTransport, "_command", short_test_deadline)
    monitor, state, events = make_monitor(tmp_path, imap_host="127.0.0.1", imap_port=port)
    with pytest.raises(TimeoutError, match="IMAP socket timed out"):
        monitor.scan_mailbox()
    assert fetching.is_set()
    key = f"incremental:v1:{monitor.identity_hash}:123"
    assert StateStore(state.path).mailbox_read_position(key) == (1, [1])
    assert not state.data["parse_failures"] and not any(kind == "alert" for kind, _ in events.queue)
    assert monitor._active_client is None


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

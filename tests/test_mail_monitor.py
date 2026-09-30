from __future__ import annotations

import ssl

from booking_notifier import mail_monitor


def test_imap_connection_requires_verified_tls(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, host, port, ssl_context, timeout):
            captured.update(host=host, port=port, context=ssl_context, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def login(self, address, password):
            captured.update(address=address, password=password)

        def select(self, mailbox, readonly):
            return "OK", [b"1"]

    monkeypatch.setattr(mail_monitor.imaplib, "IMAP4_SSL", FakeClient)
    mail_monitor.test_imap_connection(
        {"imap_host": "imap.example.com", "imap_port": 993, "email_address": "hotel@example.com"},
        "secret",
    )
    assert captured["context"].verify_mode == ssl.CERT_REQUIRED
    assert captured["context"].check_hostname is True
    assert captured["timeout"] == 30


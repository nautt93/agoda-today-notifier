from __future__ import annotations

from email.message import EmailMessage

import pytest


@pytest.fixture
def expedia_message_factory():
    def factory(
        body: str,
        subject: str = "New reservation confirmation",
        sender: str = "Expedia Partner Central <notify@expediapartnercentral.com>",
        html: str | None = None,
        message_id: str = "<booking-1@example>",
    ) -> EmailMessage:
        message = EmailMessage()
        message["From"] = sender
        message["Subject"] = subject
        message["Date"] = "Wed, 30 Sep 2026 10:00:00 +0700"
        message["Message-ID"] = message_id
        message.set_content(body)
        if html:
            message.add_alternative(html, subtype="html")
        return message

    return factory


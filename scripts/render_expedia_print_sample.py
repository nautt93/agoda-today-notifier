"""Render an anonymized two-room A4 preview. Never accepts a real email/card."""
from __future__ import annotations

import sys
from email import policy
from email.message import EmailMessage
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.utils import ImageReader  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from booking_notifier.expedia_print import parse_expedia_print, render_expedia_a4  # noqa: E402


def main() -> None:
    html = (PROJECT_ROOT / "tests/fixtures/expedia_print_test.html").read_text(encoding="utf-8")
    start, end = html.index("<tr><td>Room Type Code:"), html.index("<tr><td>DO NOT DISCLOSE")
    extra = html[start:end].replace("Superior Double Room", "Deluxe Twin Room")
    extra = extra.replace("TEST-001", "TEST-002").replace("1,700,000 VND", "1,900,000 VND")
    extra = extra.replace("4111-1111-1111-1111", "5555-5555-5555-4444")
    html = (html[:end] + extra + html[end:]).replace("Moonlight Da Nang Hotel", "Moonlight Hotel / DỮ LIỆU MẪU - THẺ TEST")
    message = EmailMessage(policy=policy.default)
    message["From"] = "Expedia <notify@expedia.com>"
    message["Subject"] = "Expedia - New Booking - Arriving on 10 Sep 2026"
    message.set_content(html, subtype="html")
    image = render_expedia_a4(parse_expedia_print(message))
    target = PROJECT_ROOT / "output/pdf/expedia-a4-mau.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(target), pagesize=A4)
    pdf.setTitle("Expedia A4 - Synthetic two-room sample")
    pdf.drawImage(ImageReader(image), 0, 0, width=A4[0], height=A4[1])
    pdf.showPage()
    pdf.save()
    image.close()
    print(target)


if __name__ == "__main__":
    main()

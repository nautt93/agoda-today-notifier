from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

WIDTH = 320
HEIGHT = 480


def _value(alert: object | Mapping[str, Any], name: str) -> object:
    return alert.get(name, "") if isinstance(alert, Mapping) else getattr(alert, name, "")


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / ("arialbd.ttf" if bold else "arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu") / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Supplemental") / ("Arial Bold.ttf" if bold else "Arial.ttf"),
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _background() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#071A2C")
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        ratio = y / max(1, HEIGHT - 1)
        color = (
            int(7 + 7 * ratio),
            int(26 + 20 * ratio),
            int(44 + 24 * ratio),
        )
        draw.line((0, y, WIDTH, y), fill=color)
    draw.ellipse((-90, -100, 220, 210), fill="#0C4774")
    draw.ellipse((180, 320, 430, 590), fill="#0A3659")
    return image


def _fit_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int, lines: int = 2) -> list[str]:
    words = " ".join(str(text or "-").split()).split(" ")
    output: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= width:
            current = candidate
            continue
        if current:
            output.append(current)
        current = word
        if len(output) == lines - 1:
            break
    if current and len(output) < lines:
        output.append(current)
    if len(output) == lines and len(" ".join(output)) < len(" ".join(words)):
        while output[-1] and draw.textbbox((0, 0), output[-1] + "…", font=font)[2] > width:
            output[-1] = output[-1][:-1]
        output[-1] += "…"
    return output or ["-"]


def _info(draw: ImageDraw.ImageDraw, y: int, label: str, value: str, *, max_lines: int = 2) -> int:
    draw.text((22, y), label, font=_font(12, True), fill="#8CB2D1")
    y += 20
    font = _font(20, True)
    lines = _fit_lines(draw, value, font, WIDTH - 44, max_lines)
    for line in lines:
        draw.text((22, y), line, font=font, fill="#FFFFFF")
        y += 26
    return y + 13


def render_booking_image(alert: object | Mapping[str, Any]) -> Image.Image:
    image = _background()
    draw = ImageDraw.Draw(image)
    source = str(_value(alert, "source") or "Agoda")
    accent = {"expedia": "#2584FF", "traveloka": "#2DDAE4", "trip": "#99A3FF", "booking.com": "#438AFF"}.get(source.lower(), "#F45B52")
    draw.rounded_rectangle((14, 16, 306, 96), radius=18, fill="#102F49", outline=accent, width=2)
    draw.text((28, 30), source.upper(), font=_font(17, True), fill=accent)
    draw.text((28, 56), "CHECK-IN HÔM NAY", font=_font(23, True), fill="#FFFFFF")
    y = 118
    checkin = _value(alert, "checkin_date")
    if isinstance(checkin, (date, datetime)):
        checkin_text = checkin.strftime("%d/%m/%Y")
    else:
        checkin_text = str(checkin or "-")
    y = _info(draw, y, "MÃ ĐẶT PHÒNG", str(_value(alert, "booking_id") or "-"), max_lines=1)
    if source.lower() != "booking.com":
        y = _info(draw, y, "KHÁCH LƯU TRÚ", str(_value(alert, "guest_name") or "-"))
        y = _info(draw, y, "LOẠI PHÒNG", str(_value(alert, "room_type") or "-"))
    _info(draw, y, "NGÀY NHẬN PHÒNG", checkin_text, max_lines=1)
    draw.rounded_rectangle((18, 432, 302, 466), radius=12, fill="#153C59")
    draw.text((32, 441), "Vui lòng chuẩn bị phòng và đón khách", font=_font(12, True), fill="#9FDCCB")
    return image


def month_grid(current: date) -> list[list[date]]:
    first = current.replace(day=1)
    start = first - timedelta(days=first.weekday())
    return [[start + timedelta(days=week * 7 + day) for day in range(7)] for week in range(6)]


def _centered(draw: ImageDraw.ImageDraw, center: tuple[int, int], text: str, size: int, color: str) -> None:
    font = _font(size, True)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((center[0] - (right - left) / 2 - left, center[1] - (bottom - top) / 2 - top),
              text, font=font, fill=color)


def render_idle_image(now: datetime | None = None) -> Image.Image:
    current = now or datetime.now()
    image = _background()
    draw = ImageDraw.Draw(image)
    _centered(draw, (160, 35), "MOONLIGHT HOTEL", 22, "#FFD985")
    _centered(draw, (160, 105), current.strftime("%H:%M"), 66, "#FFFFFF")
    weekdays = ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật")
    _centered(draw, (160, 162), f"{weekdays[current.weekday()]}, {current:%d/%m/%Y}", 18, "#A9CCE8")
    draw.rounded_rectangle((18, 194, 302, 431), radius=20, fill="#F6F8FC")
    _centered(draw, (160, 219), f"THÁNG {current.month}  •  {current.year}", 18, "#172A42")
    draw.line((35, 239, 285, 239), fill="#D7E4ED")
    for column, label in enumerate(("T2", "T3", "T4", "T5", "T6", "T7", "CN")):
        _centered(draw, (52 + 36 * column, 253), label, 12, "#C34040" if column == 6 else "#697386")
    today = current.date()
    for row, week in enumerate(month_grid(today)):
        for column, day in enumerate(week):
            x, y = 52 + 36 * column, 280 + 25 * row
            color = "#172A42" if column != 6 else "#C34040"
            if day.month != current.month:
                color = "#A9B6C1"
            if day == today:
                draw.ellipse((x - 13, y - 12, x + 13, y + 12), fill="#2563A9")
                color = "#FFFFFF"
            _centered(draw, (x, y), str(day.day), 14, color)
    _centered(draw, (160, 454), "Sẵn sàng nhận booking", 16, "#9FDCCB")
    return image


def render_status_image(title: str, message: str, success: bool = True) -> Image.Image:
    image = _background()
    draw = ImageDraw.Draw(image)
    accent = "#55E3B4" if success else "#FF7676"
    draw.ellipse((118, 70, 202, 154), outline=accent, width=6)
    draw.text((143, 85), "✓" if success else "!", font=_font(42, True), fill=accent)
    for index, line in enumerate(_fit_lines(draw, title, _font(27, True), 280, 2)):
        bbox = draw.textbbox((0, 0), line, font=_font(27, True))
        draw.text(((WIDTH - bbox[2]) / 2, 190 + index * 36), line, font=_font(27, True), fill="#FFFFFF")
    for index, line in enumerate(_fit_lines(draw, message, _font(16), 280, 4)):
        bbox = draw.textbbox((0, 0), line, font=_font(16))
        draw.text(((WIDTH - bbox[2]) / 2, 290 + index * 24), line, font=_font(16), fill="#BFD0DE")
    return image


def image_to_rgb565_le(image: Image.Image) -> bytes:
    if image.size != (WIDTH, HEIGHT):
        raise ValueError(f"Ảnh F92 phải có kích thước {WIDTH}x{HEIGHT}; đã nhận {image.size}.")
    output = bytearray(WIDTH * HEIGHT * 2)
    offset = 0
    for red, green, blue in image.convert("RGB").getdata():
        value = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
        output[offset] = value & 0xFF
        output[offset + 1] = value >> 8
        offset += 2
    return bytes(output)


def render_booking_rgb565(alert: object | Mapping[str, Any]) -> bytes:
    return image_to_rgb565_le(render_booking_image(alert))

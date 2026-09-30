from __future__ import annotations

import queue
import re
import threading
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import serial
from serial.tools import list_ports

from .f92_graphics import image_to_rgb565_le, render_booking_rgb565, render_idle_image, render_status_image

F92_VID = 0x0483
F92_PID = 0x5740
F92_BAUD = 115200
F92_FRAME_SIZE = 320 * 480 * 2


class F92Error(RuntimeError):
    pass


class F92NotFoundError(F92Error):
    pass


class F92TransportError(F92Error):
    pass


@dataclass
class F92Settings:
    enabled: bool = True
    port: str = "AUTO"
    sound_index: int = 4
    builtin_sound_enabled: bool = False

    def __post_init__(self) -> None:
        self.port = normalize_f92_port(self.port)
        self.sound_index = int(self.sound_index)
        if not 1 <= self.sound_index <= 4:
            raise ValueError("Chỉ số âm báo F92 phải từ 1 đến 4.")


def normalize_f92_port(value: object) -> str:
    text = str(value or "AUTO").strip().upper()
    if text == "AUTO" or re.fullmatch(r"COM(?:[1-9]\d{0,2})", text):
        return text
    raise ValueError("Cổng F92 phải là AUTO hoặc dạng COM3.")


def _settings(value: F92Settings | Mapping[str, Any] | None) -> F92Settings:
    if isinstance(value, F92Settings):
        return value
    data = dict(value or {})
    return F92Settings(
        enabled=bool(data.get("f92_enabled", data.get("enabled", True))),
        port=str(data.get("f92_port", data.get("port", "AUTO"))),
        sound_index=int(data.get("f92_sound_index", data.get("sound_index", 4))),
        builtin_sound_enabled=bool(data.get("f92_builtin_sound_enabled", data.get("builtin_sound_enabled", False))),
    )


def sanitize_text(value: object, max_bytes: int = 180) -> str:
    text = str(value or "").replace("đ", "d").replace("Đ", "D")
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    while len(text.encode("utf-8")) > max_bytes:
        text = text[:-1]
    return text


class F92Client:
    def __init__(self, settings: F92Settings | Mapping[str, Any] | None = None, cancel_event: threading.Event | None = None) -> None:
        self.settings = _settings(settings)
        self.cancel_event = cancel_event
        self.handle: serial.Serial | None = None
        self.active_port = ""

    def resolve_port(self) -> str:
        if self.settings.port != "AUTO":
            return self.settings.port
        for info in list_ports.comports():
            if info.vid == F92_VID and info.pid == F92_PID:
                return str(info.device)
            if "VID:PID=0483:5740" in str(info.hwid).upper():
                return str(info.device)
        raise F92NotFoundError("Không tìm thấy F92 USB (VID 0483, PID 5740).")

    def open(self) -> serial.Serial:
        if self.handle and self.handle.is_open:
            return self.handle
        self.active_port = self.resolve_port()
        try:
            self.handle = serial.Serial(self.active_port, F92_BAUD, timeout=0.8, write_timeout=1.0)
            return self.handle
        except (OSError, serial.SerialException) as exc:
            raise F92TransportError(f"Không mở được {self.active_port}: {exc}") from exc

    def close(self) -> None:
        if self.handle:
            try:
                self.handle.close()
            finally:
                self.handle = None

    def exchange(self, command: str, expected: bytes) -> None:
        if "\r" in command or "\n" in command:
            raise ValueError("Lệnh F92 không được chứa ký tự xuống dòng.")
        handle = self.open()
        try:
            handle.reset_input_buffer()
            handle.write(command.encode("utf-8") + b"\r\n")
            handle.flush()
            deadline = time.monotonic() + 0.8
            response = bytearray()
            while time.monotonic() < deadline:
                response.extend(handle.read(max(1, handle.in_waiting)))
                if expected in response:
                    return
                if b"E5!" in response or b":ERROR" in response:
                    raise F92Error(f"F92 từ chối lệnh {command}: {response.decode(errors='replace')}")
            raise F92TransportError(f"F92 không phản hồi lệnh {command}.")
        except (OSError, serial.SerialException) as exc:
            self.close()
            raise F92TransportError(f"Lỗi giao tiếp với {self.active_port}: {exc}") from exc

    def send_framebuffer(self, frame: bytes) -> None:
        if len(frame) != F92_FRAME_SIZE:
            raise ValueError(f"Framebuffer F92 phải có đúng {F92_FRAME_SIZE} byte.")
        handle = self.open()
        try:
            handle.reset_input_buffer()
            view = memoryview(frame)
            for offset in range(0, len(view), 4096):
                if self.cancel_event and self.cancel_event.is_set():
                    raise F92TransportError("Đã hủy truyền ảnh khi đóng ứng dụng.")
                chunk = view[offset:offset + 4096]
                sent = 0
                while sent < len(chunk):
                    written = handle.write(chunk[sent:])
                    if not written:
                        raise F92TransportError(f"{self.active_port} ngừng nhận dữ liệu ảnh.")
                    sent += int(written)
            handle.flush()
            time.sleep(0.08)
        except (OSError, serial.SerialException) as exc:
            self.close()
            raise F92TransportError(f"Lỗi truyền ảnh tới {self.active_port}: {exc}") from exc

    def probe(self) -> str:
        self.exchange("AT", b"\r\nOK\r\n")
        return self.active_port

    def clear(self) -> None:
        self.exchange("AT+DISPLAY_CLEAR", b"+DISPLAY_CLEAR:OK\r\n")

    def display_text(self, text: object, zone: int = 0) -> None:
        if not 0 <= zone <= 6:
            raise ValueError("Vùng hiển thị F92 phải từ 0 đến 6.")
        self.exchange(f"AT+STR_DISPLAY={zone},{sanitize_text(text)}", b"+STR_DISPLAY:OK\r\n")

    def play_sound(self, sound_index: int | None = None) -> None:
        value = int(sound_index or self.settings.sound_index)
        if not 1 <= value <= 4:
            raise ValueError("Chỉ số âm báo F92 phải từ 1 đến 4.")
        self.exchange(f"AT+CODEC_TEST={value}", b"+CODEC_TEST:OK\r\n")

    def show_idle(self, now: datetime | None = None) -> str:
        self.send_framebuffer(image_to_rgb565_le(render_idle_image(now)))
        return "màn hình chờ Moonlight"

    def notify(self, alert: object | Mapping[str, Any]) -> str:
        self.send_framebuffer(render_booking_rgb565(alert))
        if self.settings.builtin_sound_enabled:
            self.play_sound()
        return "màn hình màu"

    def test_device(self) -> str:
        self.probe()
        image = render_status_image("Kết nối thành công", "F92 đã sẵn sàng nhận thông báo Agoda và Expedia.")
        self.send_framebuffer(image_to_rgb565_le(image))
        if self.settings.builtin_sound_enabled:
            self.play_sound()
        return self.active_port


class F92Worker(threading.Thread):
    def __init__(self, event_queue: queue.Queue, settings: F92Settings | Mapping[str, Any] | None = None) -> None:
        super().__init__(name="F92Worker", daemon=True)
        self.event_queue = event_queue
        self.settings = _settings(settings)
        self.operations: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.client = F92Client(self.settings, self.stop_event)

    def configure(self, settings: F92Settings | Mapping[str, Any]) -> None:
        self.operations.put(("configure", settings))

    def notify(self, alert: object | Mapping[str, Any]) -> None:
        self.operations.put(("notify", alert))

    def test(self) -> None:
        self.operations.put(("test", None))

    def idle(self) -> None:
        self.operations.put(("idle", None))

    def close(self, timeout: float = 3.0) -> None:
        self.stop_event.set()
        self.operations.put(("stop", None))
        self.join(timeout)
        self.client.close()

    def run(self) -> None:
        while not self.stop_event.is_set():
            operation, payload = self.operations.get()
            if operation == "stop":
                break
            try:
                if operation == "configure":
                    self.settings = _settings(payload)
                    self.client.close()
                    self.client.settings = self.settings
                    self.event_queue.put(("f92_status", "F92: đã cập nhật cấu hình"))
                elif not self.settings.enabled:
                    self.event_queue.put(("f92_status", "F92: đã tắt"))
                elif operation == "notify":
                    mode = self.client.notify(payload)
                    self.event_queue.put(("f92_status", f"F92: đã báo booking bằng {mode}."))
                elif operation == "test":
                    port = self.client.test_device()
                    self.event_queue.put(("f92_test_result", (True, f"Kết nối thành công trên {port}.")))
                elif operation == "idle":
                    mode = self.client.show_idle()
                    self.event_queue.put(("f92_status", f"F92: đã về {mode}."))
            except Exception as exc:
                self.event_queue.put(("f92_error", str(exc)))
                if operation == "test":
                    self.event_queue.put(("f92_test_result", (False, str(exc))))
        self.client.close()


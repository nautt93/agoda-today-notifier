"""Windows tray lifecycle; callbacks only post events, never touch Tk widgets."""

from __future__ import annotations

import logging
import os
import queue
import threading
from typing import Any

LOGGER = logging.getLogger("booking_notifier")


class SystemTray:
    def __init__(self, events: queue.Queue[tuple[str, Any]]) -> None:
        self.events = events
        self.icon: Any = None
        self.ready = threading.Event()
        self._lock = threading.Lock()
        self._closed = False

    @property
    def available(self) -> bool:
        return self.ready.is_set() and not self._closed

    def start(self) -> None:
        if os.name != "nt":
            return
        try:
            import pystray
            from PIL import Image, ImageDraw

            image = Image.new("RGB", (64, 64), "#172A42")
            drawing = ImageDraw.Draw(image)
            drawing.rounded_rectangle((6, 6, 58, 58), radius=9, fill="#B68A3A")
            drawing.line([(17, 47), (17, 19), (32, 36), (47, 19), (47, 47)], fill="white", width=5)
            self.icon = pystray.Icon(
                "BookingDesk", image, "Booking Desk • Đang theo dõi booking hôm nay",
                menu=pystray.Menu(
                    pystray.MenuItem("Mở Booking hôm nay", self._open, default=True),
                    pystray.MenuItem("Cài đặt", self._settings),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Thoát ứng dụng", self._exit),
                ),
            )
            self.icon.run_detached(setup=self._on_ready)
        except Exception as exc:
            LOGGER.exception("Cannot start Windows system tray")
            self.events.put(("tray_failed", str(exc)))

    def _on_ready(self, icon: Any) -> None:
        try:
            with self._lock:
                closed = self._closed
                if not closed:
                    icon.visible = True
                    self.ready.set()
                    LOGGER.info("Windows system tray ready")
                    self.events.put(("tray_ready", None))
            if closed:
                icon.stop()
        except Exception as exc:
            LOGGER.exception("Cannot show Windows system tray")
            self.events.put(("tray_failed", str(exc)))
            icon.stop()

    def _open(self, _icon: Any, _item: Any) -> None:
        self.events.put(("tray_open", None))

    def _settings(self, _icon: Any, _item: Any) -> None:
        self.events.put(("tray_settings", None))

    def _exit(self, _icon: Any, _item: Any) -> None:
        self.events.put(("tray_exit", None))

    def stop(self) -> None:
        with self._lock:
            self._closed = True
            ready = self.ready.is_set()
        # If closed before startup finishes, _on_ready stops the late icon.
        if self.icon is not None and ready:
            self.icon.stop()

"""Optional, isolated Booking.com browser. All browser calls belong to one worker.

No credentials are entered by the app. The user completes login/2FA in an owned
Edge/Chrome profile, separate from their normal browser. Sessions stay local.
"""
from __future__ import annotations

import queue
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .booking_com import ADMIN_HOME, DETAIL_SNAPSHOT_JS, canonical_details_url, parse_booking_com_details
from .config import atomic_json_write
from .models import BookingEvent
from .state import StateStore

LOGIN_REQUIRED = "Booking.com: cần đăng nhập trong Cài đặt → Đăng nhập Booking.com. Email vẫn báo bình thường."


class BookingComBrowserError(RuntimeError):
    pass


class BookingComBrowser:
    def __init__(self, directory: Path, browser: str = "auto") -> None:
        self.directory = directory
        self.browser = browser if browser in {"msedge", "chrome"} else "auto"
        self.runtime: Any = None
        self.context: Any = None
        self.visible = False

    def _launch(self, visible: bool = False) -> None:
        if self.context is not None:
            browser = self.context.browser
            if browser and browser.is_connected() and (self.visible or not visible):
                return
        self.close()
        from playwright.sync_api import sync_playwright

        self.runtime = sync_playwright().start()
        channels = ("msedge", "chrome") if self.browser == "auto" else (self.browser,)
        for channel in channels:
            try:
                profile = self.directory / channel
                profile.mkdir(parents=True, exist_ok=True)
                self.context = self.runtime.chromium.launch_persistent_context(
                    str(profile), channel=channel, headless=not visible, locale="vi-VN",
                    accept_downloads=False, timeout=7000,
                )
                self.context.set_default_timeout(10000)
                self.context.set_default_navigation_timeout(10000)
                self.visible = visible
                return
            except Exception:
                # Do not log exceptions: browser errors can contain session URLs.
                continue
        self.close()
        raise BookingComBrowserError("Booking.com: không mở được Edge/Chrome. Hãy cài Edge hoặc Chrome và đóng cửa sổ Booking.com cũ của app.")

    def _page(self) -> Any:
        pages = [page for page in self.context.pages if not page.is_closed()]
        return pages[-1] if pages else self.context.new_page()

    def login(self, event: BookingEvent | None = None) -> None:
        self._launch(visible=True)
        page = self._page()
        safe = canonical_details_url(event.details_url, event.booking_id) if event else ""
        page.goto(safe or ADMIN_HOME, wait_until="domcontentloaded")
        page.bring_to_front()

    def fetch(self, event: BookingEvent) -> BookingEvent:
        safe = canonical_details_url(event.details_url, event.booking_id)
        if not safe:
            raise BookingComBrowserError("Booking.com: email không có liên kết chi tiết hợp lệ.")
        self._launch()
        page = self._page()
        if self.visible and (urlsplit(page.url).hostname != "admin.booking.com"
                             or not parse_qs(urlsplit(page.url).query).get("hotel_id")):
            # Never navigate away while the user is typing a password/OTP.
            raise BookingComBrowserError(LOGIN_REQUIRED)
        # Extranet restores the local cookie session and adds its own session
        # parameter. The canonical, token-free link was verified on the site.
        page.goto(safe, wait_until="domcontentloaded")
        if urlsplit(page.url).hostname != "admin.booking.com":
            raise BookingComBrowserError(LOGIN_REQUIRED)
        try:
            page.locator('[data-test-id="reservation-overview-name"]').filter(visible=True).first.wait_for(timeout=10000)
            return parse_booking_com_details(page.evaluate(DETAIL_SNAPSHOT_JS), event)
        except Exception:
            raise BookingComBrowserError("Booking.com: chưa lấy được chi tiết. Hãy đăng nhập/mở booking trong Cài đặt; thông báo email vẫn được giữ.") from None

    def close(self) -> None:
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
        self.context = None
        if self.runtime is not None:
            try:
                self.runtime.stop()
            except Exception:
                pass
        self.runtime = None
        self.visible = False


class BookingComWorker(threading.Thread):
    def __init__(self, events: queue.Queue, state: StateStore, config: dict[str, Any], directory: Path,
                 client_factory: Any = BookingComBrowser) -> None:
        super().__init__(name="BookingComDetails", daemon=True)
        self.events, self.state, self.directory = events, state, directory
        self.config = dict(config)
        self.client_factory = client_factory
        self.commands: queue.Queue = queue.Queue()
        self.stopping = threading.Event()
        self.client: Any = None
        self.retry_after: dict[str, float] = {}
        self.last_status = ""
        self.login_polling = False

    def configure(self, config: dict[str, Any]) -> None:
        self.commands.put(("configure", dict(config)))

    def wake(self, force: bool = False) -> None:
        self.commands.put(("refresh" if force else "wake", None))

    def login(self, event: BookingEvent | None = None) -> None:
        self.commands.put(("login", event))

    def close(self, timeout: float = 35) -> None:
        self.stopping.set()
        self.commands.put(("stop", None))
        if self.is_alive():
            self.join(timeout)

    def _status(self, text: str) -> None:
        if text != self.last_status:
            self.last_status = text
            self.events.put(("booking_com_status", text))

    def _client(self) -> Any:
        if self.client is None:
            self.client = self.client_factory(self.directory, str(self.config.get("booking_com_browser", "auto")))
        return self.client

    def refresh(self) -> None:
        if not self.config.get("booking_com_enrichment", True) or self.stopping.is_set():
            return
        today = date.today()
        for event in self.state.booking_com_candidates(today):
            if self.stopping.is_set():
                return
            if self.retry_after.get(event.storage_id, 0) > time.monotonic():
                continue
            try:
                self._status(f"Booking.com: đang lấy chi tiết {event.booking_id}…")
                enriched = self._client().fetch(event)
                if not self.stopping.is_set() and self.state.enrich_booking_com(enriched, date.today()):
                    self.events.put(("history_changed", None))
                    self._status(f"Booking.com {event.booking_id}: đã bổ sung họ tên/hạng phòng/Excel; không báo lặp.")
                self.retry_after.pop(event.storage_id, None)
            except Exception as exc:
                self.retry_after[event.storage_id] = time.monotonic() + (20 if self.login_polling else 900)
                self._status(str(exc) if isinstance(exc, BookingComBrowserError) else
                             "Booking.com: chưa lấy được chi tiết; thử Đăng nhập Booking.com trong Cài đặt.")
                # A closed browser/profile must be reopenable after manual login.
                if self.client is not None and not self.login_polling:
                    self.client.close()
                    self.client = None
                # Service/login failure must not hold a queued Login command
                # behind twenty serial failures. Other IDs resume next wake.
                break

    def run(self) -> None:
        try:
            self.refresh()
            while not self.stopping.is_set():
                try:
                    command, payload = self.commands.get(timeout=10 if self.login_polling else 60)
                except queue.Empty:
                    command, payload = "wake", None
                if command == "stop":
                    break
                if command == "configure":
                    if payload.get("booking_com_browser") != self.config.get("booking_com_browser") and self.client is not None:
                        self.client.close()
                        self.client = None
                        self.login_polling = False
                    self.config = payload
                    self.retry_after.clear()
                elif command == "refresh":
                    self.retry_after.clear()
                elif command == "login":
                    try:
                        self._client().login(payload)
                        self.login_polling = True
                        self.retry_after.clear()
                        self._status("Booking.com: hoàn tất đăng nhập/2FA trong cửa sổ Edge/Chrome của app. Chi tiết sẽ tự bổ sung.")
                    except Exception as exc:
                        self._status(str(exc) if isinstance(exc, BookingComBrowserError) else
                                     "Booking.com: không mở được trang đăng nhập; hãy thử lại.")
                    # Leave the visible login page untouched for the user.
                    continue
                self.refresh()
        finally:
            if self.client is not None:
                self.client.close()


def packaged_browser_smoke(output: Path) -> int:
    """Verify the frozen driver/installed browser against synthetic DOM only."""
    client: BookingComBrowser | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="booking-browser-smoke-") as scratch:
            client = BookingComBrowser(Path(scratch))
            client._launch()
            page = client._page()
            today, tomorrow = date.today(), date.today() + timedelta(days=1)
            event = BookingEvent(source="Booking.com", booking_id="5550000001", checkin_date=today,
                                 details_url=canonical_details_url("https://admin.booking.com" +
                                     "/hotel/hoteladmin/extranet_ng/manage/booking.html?res_id=5550000001&hotel_id=12345"))
            # Route every request: this test cannot contact a real reservation.
            markup = '<main id="main-content">' + ''.join(
                f'<p class="res-content__label">{label}</p><p class="res-content__info">{value}</p>'
                for label, value in (("Mã số đặt phòng:", event.booking_id), ("Nhận phòng", today.isoformat()),
                                     ("Trả phòng", tomorrow.isoformat()), ("Tổng số căn", "2"), ("Tổng tiền phòng", "VND 800.000"))
            ) + '<span data-test-id="reservation-overview-name">SYNTHETIC FULL GUEST</span>' + \
                '<div class="res-room-title__name">Deluxe Room</div><div class="res-room-title__name">Deluxe Room</div></main>'
            client.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=markup))
            page.goto(event.details_url)
            enriched = parse_booking_com_details(page.evaluate(DETAIL_SNAPSHOT_JS), event)
            assert enriched.guest_name == "SYNTHETIC FULL GUEST" and enriched.room_type == "Deluxe Room x2"
            assert enriched.total_revenue == "VND 800.000" and enriched.checkout_date == tomorrow
            atomic_json_write(output, {"ok": True, "driver": "playwright", "full_details": True})
            client.close()
            return 0
    except Exception:
        if client:
            client.close()
        atomic_json_write(output, {"ok": False})
        return 1

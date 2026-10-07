"""Optional, isolated Booking.com browser. All browser calls belong to one worker.

No credentials are entered by the app. The user completes login/2FA in an owned
Edge/Chrome profile, separate from their normal browser. Sessions stay local.
"""
from __future__ import annotations

import os
import queue
import secrets
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .booking_com import ADMIN_HOME, DETAIL_SNAPSHOT_JS, canonical_details_url, parse_booking_com_details
from .browser_windows import hide_owned_browser_windows, set_owned_browser_window_visible
from .config import atomic_json_write
from .models import BookingEvent
from .state import StateStore

LOGIN_REQUIRED = "Booking.com: cần đăng nhập trong Cài đặt → Đăng nhập Booking.com. Email vẫn báo bình thường."
AUTHENTICATED_PAGE_JS = """() => {
    const visible = el => !!el && !!el.getClientRects().length;
    const challenges = document.querySelectorAll(
        'input[type="password"], input[autocomplete="one-time-code"], input[name*="otp" i], '
        + 'input[id*="otp" i], input[name*="verification" i], input[name="code" i], '
        + 'iframe[src*="account.booking.com"]');
    if ([...challenges].some(visible)) return false;
    return [...document.querySelectorAll(
        '[data-test-id="reservation-overview-name"], a[href*="extranet_ng/manage/bookings"], '
        + 'a[href*="logout"], form[action*="logout"]')].some(visible);
}"""


class BookingComBrowserError(RuntimeError):
    pass


class BookingComBrowser:
    def __init__(self, directory: Path, browser: str = "auto") -> None:
        self.directory = directory
        self.browser = browser if browser in {"msedge", "chrome"} else "auto"
        self.runtime: Any = None
        self.context: Any = None
        self.visible = False
        self.keeper_page: Any = None
        self.work_page: Any = None
        self.awaiting_login = False

    def is_connected(self) -> bool:
        try:
            return bool(self.context is not None and self.context.browser and self.context.browser.is_connected())
        except Exception:
            return False

    def _browser_pid(self) -> int:
        """Query this live, isolated browser; never resolve a normal Chrome by name."""
        session = self.context.browser.new_browser_cdp_session()
        try:
            processes = session.send("SystemInfo.getProcessInfo")["processInfo"]
            return next(int(process["id"]) for process in processes if process["type"] == "browser")
        finally:
            session.detach()

    def _set_window_visible(self, page: Any, visible: bool) -> bool:
        if os.name != "nt":
            # Development/synthetic tests only. Production is Windows.
            return False
        marker = "BookingDesk-" + secrets.token_hex(16)
        title = page.title()
        try:
            page.evaluate("title => { document.title = title; }", marker)
            for _ in range(20):
                if set_owned_browser_window_visible(self._browser_pid(), marker, visible):
                    return True
                page.wait_for_timeout(50)
            return False
        finally:
            try:
                page.evaluate("title => { document.title = title; }", title)
            except Exception:
                pass

    def background(self) -> bool:
        """Hide the owned windows, preserving the browser, tabs and session cookies."""
        if not self.is_connected():
            return False
        if os.name == "nt" and not hide_owned_browser_windows(self._browser_pid()):
            return False
        self.visible = False
        return True

    def _launch(self, visible: bool = False) -> None:
        if self.is_connected():
            return  # Showing/hiding never restarts or changes the browser mode.
        self.close()
        from playwright.sync_api import sync_playwright

        self.runtime = sync_playwright().start()
        channels = ("msedge", "chrome") if self.browser == "auto" else (self.browser,)
        for channel in channels:
            try:
                profile = self.directory / channel
                profile.mkdir(parents=True, exist_ok=True)
                self.context = self.runtime.chromium.launch_persistent_context(
                    str(profile), channel=channel, headless=False, locale="vi-VN",
                    accept_downloads=False, timeout=7000,
                )
                self.context.set_default_timeout(10000)
                self.context.set_default_navigation_timeout(10000)
                # A separate hidden blank window keeps this same browser alive
                # when the user closes the visible login/detail window with X.
                self.keeper_page = self.context.pages[0] if self.context.pages else self.context.new_page()
                self.keeper_page.goto("about:blank")
                hidden = self._set_window_visible(self.keeper_page, False)
                if os.name == "nt" and not hidden:
                    raise BookingComBrowserError("Booking.com: chưa tạo được cửa sổ nền an toàn.")
                self.visible = False
                return
            except Exception:
                # Do not log exceptions: browser errors can contain session URLs.
                if self.context is not None:
                    try:
                        self.context.close()
                    except Exception:
                        pass
                    self.context = None
                self.keeper_page = self.work_page = None
                continue
        self.close()
        raise BookingComBrowserError("Booking.com: không mở được Edge/Chrome. Hãy cài Edge hoặc Chrome và đóng cửa sổ Booking.com cũ của app.")

    def _new_work_page(self) -> Any:
        session = self.context.browser.new_browser_cdp_session()
        try:
            with self.context.expect_page(timeout=5000) as opened:
                session.send("Target.createTarget", {"url": "about:blank", "newWindow": True})
            page = opened.value
        finally:
            session.detach()
        hidden = self._set_window_visible(page, False)
        if os.name == "nt" and not hidden:
            page.close()  # Only the new blank window, never the healthy session.
            raise BookingComBrowserError("Booking.com: chưa ẩn được cửa sổ mới; phiên hiện tại vẫn được giữ.")
        return page

    def _page(self) -> Any:
        if self.work_page is not None and not self.work_page.is_closed():
            return self.work_page
        if self.work_page is not None:
            self.visible = False
            self.awaiting_login = False  # A closed OTP tab cannot own the next challenge.
        pages = [page for page in self.context.pages if page is not self.keeper_page and not page.is_closed()]
        if pages:
            self.work_page = pages[-1]
        else:
            self.work_page = self._new_work_page()
            self.visible = False
        return self.work_page

    def _authenticated_page(self, page: Any) -> bool:
        try:
            url = urlsplit(page.url)
            hotel = parse_qs(url.query).get("hotel_id", [])
            if (url.scheme != "https" or url.hostname != "admin.booking.com" or url.username or url.password
                    or url.port not in (None, 443) or not url.path.startswith("/hotel/hoteladmin/extranet_ng/")
                    or len(hotel) != 1 or not hotel[0].isdigit()
                    or any(part in url.path.lower() for part in ("login", "sign-in", "verify", "otp", "authentication"))):
                return False
            return page.evaluate(AUTHENTICATED_PAGE_JS) is True
        except Exception:
            return False

    def maintain(self) -> str:
        """Pump the owning browser without navigating a password/OTP screen."""
        if not self.is_connected():
            return "idle"
        page = self.work_page
        if page is None:
            return "idle"
        if page.is_closed():
            self.visible = False
            waiting, self.awaiting_login = self.awaiting_login, False
            return "closed" if waiting else "idle"
        page.wait_for_timeout(1)
        if self.awaiting_login:
            if not self._authenticated_page(page):
                return "waiting"
            self.awaiting_login = False
            return "authenticated" if self.background() else "authenticated-visible"
        return "idle"

    def login(self, event: BookingEvent | None = None) -> None:
        self._launch(visible=True)
        page = self._page()
        safe = canonical_details_url(event.details_url, event.booking_id) if event else ""
        # Opening an already displayed OTP screen must not reset its challenge.
        if not self.awaiting_login:
            self.awaiting_login = not self._authenticated_page(page)
            if safe or self.awaiting_login:
                page.goto(safe or ADMIN_HOME, wait_until="domcontentloaded")
        shown = self._set_window_visible(page, True)
        if os.name == "nt" and not shown:
            raise BookingComBrowserError("Booking.com: chưa hiện được cửa sổ đăng nhập; phiên hiện tại vẫn được giữ.")
        self.visible = True
        page.bring_to_front()

    def fetch(self, event: BookingEvent) -> BookingEvent:
        safe = canonical_details_url(event.details_url, event.booking_id)
        if not safe:
            raise BookingComBrowserError("Booking.com: email không có liên kết chi tiết hợp lệ.")
        self._launch()
        page = self._page()
        if (self.awaiting_login or self.visible) and not self._authenticated_page(page):
            # Never navigate away while the user is typing a password/OTP.
            raise BookingComBrowserError(LOGIN_REQUIRED)
        # Extranet restores the local cookie session and adds its own session
        # parameter. The canonical, token-free link was verified on the site.
        page.goto(safe, wait_until="domcontentloaded")
        if urlsplit(page.url).hostname != "admin.booking.com":
            self.awaiting_login = True
            raise BookingComBrowserError(LOGIN_REQUIRED)
        try:
            page.locator('[data-test-id="reservation-overview-name"]').filter(visible=True).first.wait_for(timeout=10000)
            if not self._authenticated_page(page):
                self.awaiting_login = True
                raise BookingComBrowserError(LOGIN_REQUIRED)
            enriched = parse_booking_com_details(page.evaluate(DETAIL_SNAPSHOT_JS), event)
            if self.awaiting_login or not self.visible:
                self.awaiting_login = False
                # Chromium can restore a native window while creating/navigating
                # a replacement tab after X. Re-hide after the page is loaded,
                # but never hide a window the user explicitly opened to inspect.
                self.background()
            return enriched
        except BookingComBrowserError:
            raise
        except Exception:
            if not self._authenticated_page(page):
                self.awaiting_login = True
                raise BookingComBrowserError(LOGIN_REQUIRED) from None
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
        self.keeper_page = self.work_page = None
        self.awaiting_login = False


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

    def background(self) -> None:
        self.commands.put(("background", None))

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
                self.retry_after[event.storage_id] = time.monotonic() + (20 if self.login_polling else 60)
                self._status(str(exc) if isinstance(exc, BookingComBrowserError) else
                             "Booking.com: chưa lấy được chi tiết; thử Đăng nhập Booking.com trong Cài đặt.")
                # A timeout, expired session or parser error must not destroy a
                # healthy browser's session cookies or in-memory auth state.
                # Service/login failure must not hold a queued Login command
                # behind twenty serial failures. Other IDs resume next wake.
                break

    def run(self) -> None:
        try:
            self.refresh()
            while not self.stopping.is_set():
                try:
                    command, payload = self.commands.get(timeout=2 if self.login_polling else 60)
                except queue.Empty:
                    command, payload = "wake", None
                if command == "stop":
                    break
                if command == "configure":
                    if (payload.get("booking_com_browser") != self.config.get("booking_com_browser")
                            or not payload.get("booking_com_enrichment", True)) and self.client is not None:
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
                elif command == "background":
                    try:
                        if self.client is not None and self.client.background():
                            self._status("Booking.com: đã ẩn trình duyệt, giữ cùng phiên chạy nền. Nếu chưa hoàn tất OTP, mở lại Đăng nhập Booking.com.")
                        else:
                            self._status("Booking.com: chưa ẩn được trình duyệt; phiên hiện tại vẫn được giữ.")
                    except Exception:
                        self._status("Booking.com: chưa ẩn được trình duyệt; phiên hiện tại vẫn được giữ.")
                if self.client is not None:
                    try:
                        session = self.client.maintain()
                        if session in {"authenticated", "authenticated-visible"}:
                            self.login_polling = False
                            self.retry_after.clear()
                            self._status("Booking.com: đăng nhập thành công, giữ phiên chạy nền. Có thể mở lại trong Cài đặt."
                                         if session == "authenticated" else
                                         "Booking.com: đăng nhập thành công nhưng chưa ẩn được cửa sổ. Phiên vẫn được giữ; thử nút Ẩn trình duyệt.")
                        elif session == "closed":
                            self.login_polling = False
                            self._status(LOGIN_REQUIRED)
                    except Exception:
                        self._status("Booking.com: phiên trình duyệt cần kiểm tra; thông báo email vẫn hoạt động.")
                self.refresh()
        finally:
            if self.client is not None:
                self.client.close()


def packaged_browser_smoke(output: Path) -> int:
    """Verify the frozen driver/installed browser against synthetic DOM only."""
    client: BookingComBrowser | None = None
    stage = "launch"
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
            markup = '<!doctype html><meta charset="utf-8"><main id="main-content">' + ''.join(
                f'<p class="res-content__label">{label}</p><p class="res-content__info">{value}</p>'
                for label, value in (("Mã số đặt phòng:", event.booking_id), ("Nhận phòng", today.isoformat()),
                                     ("Trả phòng", tomorrow.isoformat()), ("Tổng số căn", "2"), ("Tổng tiền phòng", "VND 800.000"))
            ) + '<span data-test-id="reservation-overview-name">SYNTHETIC FULL GUEST</span>' + \
                '<div class="res-room-title__name">Deluxe Room</div><div class="res-room-title__name">Deluxe Room</div></main>'
            client.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=markup))
            client.context.set_offline(True)  # Any request missed by routing must fail, not contact a real site.
            stage = "navigate"
            page.goto(event.details_url)
            stage = "parse"
            enriched = parse_booking_com_details(page.evaluate(DETAIL_SNAPSHOT_JS), event)
            assert enriched.guest_name == "SYNTHETIC FULL GUEST" and enriched.room_type == "Deluxe Room x2"
            assert enriched.total_revenue == "VND 800.000" and enriched.checkout_date == tomorrow
            stage = "live-session"
            context, pid = client.context, client._browser_pid()
            page.evaluate("sessionStorage.setItem('synthetic-tab-session', 'not-a-real-credential')")
            context.add_cookies([{"name": "synthetic-session-cookie", "value": "not-a-real-session",
                                  "url": "https://admin.booking.com", "httpOnly": True, "secure": True}])
            client.awaiting_login = True
            assert client.maintain() == "authenticated" and not client.visible
            assert client.fetch(event).guest_name == enriched.guest_name
            assert client.context is context and client._browser_pid() == pid
            assert page.evaluate("sessionStorage.getItem('synthetic-tab-session')") == "not-a-real-credential"
            stage = "closed-work-window"
            page.close()
            client.keeper_page.wait_for_timeout(100)
            assert client.is_connected() and client._browser_pid() == pid
            assert client.fetch(event).room_type == enriched.room_type
            assert client.context is context and client.work_page is not page
            assert any(cookie["name"] == "synthetic-session-cookie" and cookie["expires"] == -1
                       for cookie in context.cookies())
            atomic_json_write(output, {"ok": True, "driver": "playwright", "full_details": True,
                                       "live_session_kept": True, "session_kept_after_window_close": True})
            client.close()
            return 0
    except Exception as exc:
        if client:
            client.close()
        atomic_json_write(output, {"ok": False, "stage": stage, "error": type(exc).__name__})
        return 1

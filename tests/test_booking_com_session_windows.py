"""Native headed-browser session checks; all network responses are synthetic."""
from __future__ import annotations

import ctypes as ct
import os
import subprocess
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from booking_notifier import browser_windows
from booking_notifier.booking_com import DETAIL_PATH, canonical_details_url
from booking_notifier.booking_com_browser import LOGIN_REQUIRED, BookingComBrowser, BookingComBrowserError
from booking_notifier.models import BookingEvent

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows headed Edge/Chrome session and owned windows")

SIGN_IN_URL = "https://account.booking.com/sign-in?next=synthetic-only"
COOKIE_NAME = "booking-desk-synthetic-session"
COOKIE_VALUE = "synthetic-not-a-real-session"
STORAGE_KEY = "booking-desk-synthetic-runtime"


def _basic_event():
    return BookingEvent(
        source="Booking.com", booking_id="5550000001", checkin_date=date.today(),
        details_url=canonical_details_url(
            f"https://admin.booking.com{DETAIL_PATH}?res_id=5550000001&hotel_id=12345",
        ),
    )


def _owned_windows(api, pid):
    return [hwnd for hwnd in api.enumerate_windows() if browser_windows._owned_window(api, hwnd, pid)]


def _assert_native_visibility(client, api, pid, *, all_hidden):
    # Chromium updates native titles/visibility asynchronously after CDP calls.
    for _ in range(40):
        handles = _owned_windows(api, pid)
        if handles:
            visible = [bool(api.user32.IsWindowVisible(hwnd)) for hwnd in handles]
            if (not any(visible)) if all_hidden else any(visible):
                return handles
        client.keeper_page.wait_for_timeout(50)
    pytest.fail("Owned Chromium window visibility did not match requested state")


def _assert_session_cookie(context):
    matches = [cookie for cookie in context.cookies() if cookie["name"] == COOKIE_NAME]
    assert len(matches) == 1
    assert matches[0]["value"] == COOKIE_VALUE
    assert matches[0]["expires"] == -1  # A runtime session cookie, not a persistent cookie.


def _close_native_work_window(client, api, pid):
    page = client.work_page
    marker = "BookingDesk-native-close-" + uuid.uuid4().hex
    original_title = page.title()
    page.evaluate("title => { document.title = title; }", marker)
    try:
        hwnd = None
        for _ in range(40):
            matches = [candidate for candidate in _owned_windows(api, pid)
                       if browser_windows._owned_window(api, candidate, pid, marker)]
            if len(matches) == 1:
                hwnd = matches[0]
                break
            page.wait_for_timeout(50)
        assert hwnd is not None, "Could not identify exact synthetic work window"
        # As with the production helper, revalidate immediately before mutation.
        assert browser_windows._owned_window(api, hwnd, pid, marker)
        assert api.is_window(hwnd) and api.window_pid(hwnd) == pid
        with page.expect_event("close", timeout=10000):
            assert api.user32.PostMessageW(hwnd, 0x0010, 0, 0), ct.get_last_error()  # WM_CLOSE: real X equivalent.
    finally:
        if not page.is_closed():
            page.evaluate("title => { document.title = title; }", original_title)
    assert page.is_closed()


def test_native_headed_cookie_session_survives_hide_and_browser_x_then_handles_server_expiry(tmp_path):
    if os.environ.get("BOOKING_COM_SESSION_NATIVE_CHILD") != "1":
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q",
             f"{Path(__file__).resolve()}::test_native_headed_cookie_session_survives_hide_and_browser_x_then_handles_server_expiry"],
            env={**os.environ, "BOOKING_COM_SESSION_NATIVE_CHILD": "1"}, check=True, timeout=90,
        )
        return

    api = browser_windows._native()
    api.user32.IsWindowVisible.argtypes, api.user32.IsWindowVisible.restype = [ct.c_void_p], ct.c_int32
    api.user32.PostMessageW.argtypes = [ct.c_void_p, ct.c_uint32, ct.c_size_t, ct.c_ssize_t]
    api.user32.PostMessageW.restype = ct.c_int32
    client = BookingComBrowser(tmp_path / "dedicated-native-synthetic-profile")
    event = _basic_event()
    markup = (Path(__file__).parent / "fixtures" / "booking_com_details.html").read_text(encoding="utf-8")
    markup = markup.replace("ARRIVAL", date.today().isoformat())
    markup = markup.replace("DEPARTURE", (date.today() + timedelta(days=2)).isoformat())
    server = {"expired": False}

    def synthetic_response(route):
        host = urlsplit(route.request.url).hostname
        if host == "admin.booking.com" and server["expired"]:
            route.fulfill(status=302, headers={"location": SIGN_IN_URL}, body="")
        elif host == "admin.booking.com":
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=markup)
        elif host == "account.booking.com":
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=(
                '<!doctype html><meta charset="utf-8"><title>Synthetic OTP</title>'
                '<label>OTP <input name="otp" autocomplete="one-time-code"></label>'
            ))
        else:
            # No request may escape to Booking.com, an account, or a third party.
            route.fulfill(status=200, content_type="text/plain", body="synthetic-only")

    try:
        client._launch()  # Headed browser with separate hidden keeper window.
        context, keeper = client.context, client.keeper_page
        context.route("**/*", synthetic_response)
        pid = client._browser_pid()
        assert pid > 0 and keeper.url == "about:blank"
        assert context.browser.is_connected()
        client.login(event)
        page = client.work_page
        assert page is not keeper and page.url == event.details_url
        assert client.visible and client.awaiting_login
        context.add_cookies([{
            "name": COOKIE_NAME, "value": COOKIE_VALUE, "url": "https://admin.booking.com",
            "httpOnly": True, "secure": True,
        }])
        page.evaluate("key => sessionStorage.setItem(key, 'synthetic-runtime-only')", STORAGE_KEY)
        _assert_session_cookie(context)
        # A trusted admin URL and guest-name marker are not proof of completed
        # authentication while a visible verification challenge remains.
        page.evaluate("""() => {
            const input = document.createElement('input');
            input.id = 'booking-desk-synthetic-otp';
            input.name = 'code';
            input.autocomplete = 'one-time-code';
            document.body.appendChild(input);
        }""")
        assert page.locator('#booking-desk-synthetic-otp').is_visible()
        assert client.maintain() == "waiting"
        assert client.visible and client.awaiting_login and page.url == event.details_url
        assert client.context is context and client.work_page is page and client._browser_pid() == pid
        _assert_native_visibility(client, api, pid, all_hidden=False)
        page.evaluate("""() => {
            document.getElementById('booking-desk-synthetic-otp').remove();
            const input = document.createElement('input');
            input.name = 'postal_code';
            input.autocomplete = 'postal-code';
            document.body.appendChild(input);
        }""")
        assert page.locator('input[name="postal_code"]').is_visible()
        assert client._authenticated_page(page)
        assert client.maintain() == "authenticated"
        assert not client.visible and not client.awaiting_login
        assert len(_assert_native_visibility(client, api, pid, all_hidden=True)) >= 2
        assert client.context is context and client._browser_pid() == pid and client.work_page is page
        _assert_session_cookie(context)
        assert page.evaluate("key => sessionStorage.getItem(key)", STORAGE_KEY) == "synthetic-runtime-only"

        client.login()  # Restore the existing page without navigation or restart.
        assert client.visible and client.work_page is page and client.context is context
        _assert_native_visibility(client, api, pid, all_hidden=False)
        assert client._browser_pid() == pid
        assert page.evaluate("key => sessionStorage.getItem(key)", STORAGE_KEY) == "synthetic-runtime-only"
        assert client.background()
        full = client.fetch(event)
        assert full.guest_name == "NGUYỄN SYNTHETIC FULL GUEST"
        assert full.room_type == "Deluxe Double Room x2; Triple City View x1"
        assert full.checkout_date == date.today() + timedelta(days=2)
        assert full.total_revenue == "VND 1.200.000"
        assert client.context is context and client.work_page is page and client._browser_pid() == pid
        assert page.evaluate("key => sessionStorage.getItem(key)", STORAGE_KEY) == "synthetic-runtime-only"
        _assert_session_cookie(context)
        _assert_native_visibility(client, api, pid, all_hidden=True)

        client.login()
        _assert_native_visibility(client, api, pid, all_hidden=False)
        _close_native_work_window(client, api, pid)
        assert not keeper.is_closed() and context.browser.is_connected()
        assert client.context is context and client._browser_pid() == pid
        _assert_session_cookie(context)
        assert client.maintain() == "idle"
        _assert_native_visibility(client, api, pid, all_hidden=True)

        recovered = client.fetch(event)
        replacement = client.work_page
        assert replacement is not page and replacement is not keeper
        assert client.context is context and client._browser_pid() == pid
        assert recovered.guest_name == full.guest_name and recovered.room_type == full.room_type
        assert recovered.total_revenue == full.total_revenue and not client.awaiting_login
        _assert_session_cookie(context)
        _assert_native_visibility(client, api, pid, all_hidden=True)
        # Deliberately do not assert sessionStorage from the CLOSED tab survives.

        server["expired"] = True  # Server revokes access despite the cookie still existing.
        with pytest.raises(BookingComBrowserError) as failure:
            client.fetch(event)
        assert str(failure.value) == LOGIN_REQUIRED
        assert COOKIE_VALUE not in str(failure.value) and "https://" not in str(failure.value)
        assert client.awaiting_login and client.context is context and client._browser_pid() == pid
        assert client.work_page is replacement and replacement.url == SIGN_IN_URL
        replacement.locator('input[autocomplete="one-time-code"]').wait_for(state="visible", timeout=5000)
        assert replacement.locator('input[autocomplete="one-time-code"]').is_visible()
        navigation = []
        replacement.on("framenavigated", lambda frame: navigation.append(frame.url)
                       if frame == replacement.main_frame else None)
        client.login(event)  # Show the existing OTP challenge, never reset it.
        assert client.visible and client.awaiting_login and client.context is context
        assert client.work_page is replacement and client._browser_pid() == pid
        assert replacement.url == SIGN_IN_URL and not navigation
        _assert_native_visibility(client, api, pid, all_hidden=False)
        assert client.maintain() == "waiting"
        assert client.visible and client.awaiting_login and not navigation
        with pytest.raises(BookingComBrowserError, match="đăng nhập"):
            client.fetch(event)
        assert client.context is context and replacement.url == SIGN_IN_URL and not navigation
        assert context.browser.is_connected() and not keeper.is_closed()
    finally:
        client.close()

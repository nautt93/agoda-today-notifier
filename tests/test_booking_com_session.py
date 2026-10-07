"""Session lifecycle checks using synthetic pages only; no browser/account I/O."""
from __future__ import annotations

import queue
import threading
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

import booking_notifier.booking_com_browser as browser_module
from booking_notifier.booking_com import ADMIN_HOME, DETAIL_PATH, canonical_details_url
from booking_notifier.booking_com_browser import (
    AUTHENTICATED_PAGE_JS,
    BookingComBrowser,
    BookingComBrowserError,
    BookingComWorker,
)
from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore

AUTHENTICATED_URL = "https://admin.booking.com/hotel/hoteladmin/extranet_ng/home.html?hotel_id=12345"
SIGN_IN_URL = "https://account.booking.com/sign-in?next=synthetic-only"


def basic_event():
    return BookingEvent(
        source="Booking.com", booking_id="5550000001", checkin_date=date.today(),
        details_url=canonical_details_url(
            f"https://admin.booking.com{DETAIL_PATH}?res_id=5550000001&hotel_id=12345",
        ),
    )


def full_event(event):
    return replace(event, guest_name="SYNTHETIC FULL GUEST", room_type="Deluxe Room x2",
                   checkout_date=date.today() + timedelta(days=2), total_revenue="VND 800.000",
                   details_loaded_at="2026-10-07T12:00:00")


def fake_page(url=AUTHENTICATED_URL, *, closed=False, authenticated=True):
    page = Mock()
    page.url = url
    page.is_closed.return_value = closed
    page.evaluate.return_value = authenticated
    return page


def connected_client(tmp_path, page=None):
    """An owned browser session whose CDP calls are harmless mocks."""
    client = BookingComBrowser(tmp_path / "isolated-synthetic-profile")
    page = page or fake_page()
    keeper = fake_page("about:blank")
    browser = Mock()
    browser.is_connected.return_value = True
    context = Mock()
    context.browser, context.pages = browser, [keeper, page]
    context.new_cdp_session.return_value.send.return_value = {
        "windowId": 1, "bounds": {"windowState": "normal"},
    }
    browser.new_browser_cdp_session.return_value.send.return_value = {
        "windowId": 1, "bounds": {"windowState": "normal"},
    }
    client.context, client.runtime = context, Mock()
    client.keeper_page, client.work_page = keeper, page
    client.visible, client.awaiting_login = True, False
    # Windows CI must not resolve or hide any physical application windows.
    client._browser_pid = Mock(return_value=424242)
    client._set_window_visible = Mock(return_value=True)
    return client, page


@pytest.mark.parametrize("visible", [False, True])
def test_launch_reuses_healthy_owned_context_when_showing_or_backgrounding(tmp_path, monkeypatch, visible):
    from playwright import sync_api

    client, page = connected_client(tmp_path)
    client.visible = False  # Reopening the login UI must not restart an already backgrounded session.
    context, runtime = client.context, client.runtime
    close = Mock()
    start = Mock()
    start.return_value.start.return_value = runtime
    runtime.chromium.launch_persistent_context.return_value = context
    monkeypatch.setattr(sync_api, "sync_playwright", start)
    monkeypatch.setattr(client, "close", close)
    client._launch(visible=visible)
    assert client.context is context and client.runtime is runtime and client.work_page is page
    close.assert_not_called()
    context.close.assert_not_called()
    runtime.stop.assert_not_called()
    start.assert_not_called()
    runtime.chromium.launch_persistent_context.assert_not_called()


def test_background_preserves_live_context_page_and_does_not_navigate(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_module, "hide_owned_browser_windows", Mock(return_value=True))
    client, page = connected_client(tmp_path)
    context, runtime = client.context, client.runtime
    client.background()
    assert client.context is context and client.work_page is page and client.is_connected()
    assert not client.visible
    context.close.assert_not_called()
    runtime.stop.assert_not_called()
    page.close.assert_not_called()
    page.goto.assert_not_called()


def test_fresh_launch_is_headed_with_hidden_keeper_even_for_background_fetch(tmp_path, monkeypatch):
    from playwright import sync_api

    client = BookingComBrowser(tmp_path / "synthetic-only")
    keeper, context, runtime = fake_page("about:blank"), Mock(), Mock()
    context.pages = [keeper]
    runtime.chromium.launch_persistent_context.return_value = context
    start = Mock()
    start.return_value.start.return_value = runtime
    monkeypatch.setattr(sync_api, "sync_playwright", start)
    hide = Mock(return_value=True)
    monkeypatch.setattr(client, "_set_window_visible", hide)
    client._launch(visible=False)
    assert runtime.chromium.launch_persistent_context.call_args.kwargs["headless"] is False
    assert client.context is context and client.keeper_page is keeper and client.work_page is None
    keeper.goto.assert_called_once_with("about:blank")
    hide.assert_called_once_with(keeper, False)
    context.close.assert_not_called()


def test_windows_fresh_keeper_hide_failure_closes_unsafe_new_browser(tmp_path, monkeypatch):
    from playwright import sync_api

    monkeypatch.setattr(browser_module, "os", SimpleNamespace(name="nt"))
    client = BookingComBrowser(tmp_path / "synthetic-only", browser="chrome")
    keeper, context, runtime = fake_page("about:blank"), Mock(), Mock()
    context.pages = [keeper]
    runtime.chromium.launch_persistent_context.return_value = context
    start = Mock()
    start.return_value.start.return_value = runtime
    monkeypatch.setattr(sync_api, "sync_playwright", start)
    hide = Mock(return_value=False)
    monkeypatch.setattr(client, "_set_window_visible", hide)
    with pytest.raises(BookingComBrowserError) as failure:
        client._launch()
    hide.assert_called_once_with(keeper, False)
    context.close.assert_called_once()
    runtime.stop.assert_called_once()
    assert client.context is None and client.runtime is None
    assert client.keeper_page is client.work_page is None
    assert not client.visible and "https://" not in str(failure.value)


def test_windows_new_work_window_hide_failure_disposes_only_new_page_not_healthy_session(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_module, "os", SimpleNamespace(name="nt"))
    client, existing = connected_client(tmp_path)
    new_page = fake_page("about:blank")
    opened = MagicMock()
    opened.__enter__.return_value = SimpleNamespace(value=new_page)
    client.context.expect_page.return_value = opened
    client._set_window_visible.return_value = False
    with pytest.raises(BookingComBrowserError):
        client._new_work_page()
    new_page.close.assert_called_once()
    client.context.close.assert_not_called()
    client.runtime.stop.assert_not_called()
    assert client.work_page is existing and client.is_connected()
    client.context.browser.new_browser_cdp_session.return_value.detach.assert_called_once()


def test_windows_login_show_failure_preserves_healthy_context_and_otp_page(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_module, "os", SimpleNamespace(name="nt"))
    client, page = connected_client(tmp_path, fake_page(SIGN_IN_URL, authenticated=False))
    client.visible, client.awaiting_login = False, True
    client._set_window_visible.return_value = False
    with pytest.raises(BookingComBrowserError):
        client.login(basic_event())
    assert not client.visible and client.awaiting_login and client.work_page is page and client.is_connected()
    client.context.close.assert_not_called()
    client.runtime.stop.assert_not_called()
    page.goto.assert_not_called()
    page.bring_to_front.assert_not_called()


def test_windows_background_hide_failure_preserves_visible_live_session(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_module, "os", SimpleNamespace(name="nt"))
    hide = Mock(return_value=False)
    monkeypatch.setattr(browser_module, "hide_owned_browser_windows", hide)
    client, page = connected_client(tmp_path)
    assert client.background() is False
    hide.assert_called_once_with(424242)
    assert client.visible and client.is_connected() and client.work_page is page
    client.context.close.assert_not_called()
    client.runtime.stop.assert_not_called()
    page.close.assert_not_called()


def test_background_does_not_launch_missing_browser(tmp_path, monkeypatch):
    client = BookingComBrowser(tmp_path)
    launch = Mock()
    monkeypatch.setattr(client, "_launch", launch)
    assert client.background() is False
    launch.assert_not_called()


def test_page_never_uses_hidden_keeper_as_reservation_or_login_page(tmp_path, monkeypatch):
    client, old = connected_client(tmp_path)
    old.is_closed.return_value = True
    client.work_page = None
    client.context.pages = [client.keeper_page]
    new = fake_page()

    def create_page():
        client.work_page = new
        return new

    create = Mock(side_effect=create_page)
    monkeypatch.setattr(client, "_new_work_page", create)
    assert client._page() is new
    assert client._page() is new
    create.assert_called_once()
    client.keeper_page.goto.assert_not_called()


@pytest.mark.parametrize("url", [
    "http://admin.booking.com/hotel/hoteladmin/extranet_ng/home.html?hotel_id=12345",
    "https://admin.booking.com.evil.invalid/hotel/hoteladmin/extranet_ng/home.html?hotel_id=12345",
    "https://account.booking.com/sign-in?hotel_id=12345",
    "https://admin.booking.com/?hotel_id=12345",
    "https://admin.booking.com/hotel/hoteladmin/extranet_ng/home.html",
    "https://admin.booking.com/hotel/hoteladmin/extranet_ng/home.html?hotel_id=abc",
    "https://admin.booking.com/hotel/hoteladmin/extranet_ng/home.html?hotel_id=12345&hotel_id=12346",
    "https://username:password@admin.booking.com/hotel/hoteladmin/extranet_ng/home.html?hotel_id=12345",
    "https://admin.booking.com:444/hotel/hoteladmin/extranet_ng/home.html?hotel_id=12345",
])
def test_untrusted_or_auth_url_cannot_qualify_as_authenticated_even_with_dom_marker(tmp_path, url):
    client = BookingComBrowser(tmp_path)
    assert not client._authenticated_page(fake_page(url, authenticated=True))


@pytest.mark.parametrize("authenticated", [False, True])
def test_authenticated_page_requires_verified_dom_not_only_admin_url(tmp_path, authenticated):
    client = BookingComBrowser(tmp_path)
    assert client._authenticated_page(fake_page(authenticated=authenticated)) is authenticated


def test_authentication_probe_is_boolean_and_does_not_read_or_persist_secret_values(tmp_path):
    profile = tmp_path / "synthetic-profile-not-created"
    client, page = BookingComBrowser(profile), fake_page()
    assert client._authenticated_page(page)
    page.evaluate.assert_called_once_with(AUTHENTICATED_PAGE_JS)
    assert "password" in AUTHENTICATED_PAGE_JS and "one-time-code" in AUTHENTICATED_PAGE_JS
    assert all(secret_reader not in AUTHENTICATED_PAGE_JS for secret_reader in
               (".value", "document.cookie", "localStorage", "sessionStorage", "storage_state"))
    assert not profile.exists()


def test_auth_probe_failure_is_false_and_does_not_expose_raw_exception(tmp_path):
    client, page = BookingComBrowser(tmp_path), fake_page()
    page.evaluate.side_effect = RuntimeError("https://admin.booking.com/?ses=synthetic-secret otp=654321")
    assert client._authenticated_page(page) is False


def test_fetch_on_admin_otp_page_does_not_navigate_or_destroy_live_context(tmp_path):
    client, page = connected_client(tmp_path, fake_page(AUTHENTICATED_URL, authenticated=False))
    client.awaiting_login = True
    with pytest.raises(BookingComBrowserError, match="đăng nhập"):
        client.fetch(basic_event())
    page.goto.assert_not_called()
    client.context.close.assert_not_called()


def test_login_during_password_or_otp_entry_preserves_page_and_live_context(tmp_path, monkeypatch):
    client, page = connected_client(tmp_path, fake_page(SIGN_IN_URL, authenticated=False))
    client.awaiting_login = True
    context = client.context
    monkeypatch.setattr(client, "_launch", Mock())
    client.login(basic_event())
    assert client.context is context and client.work_page is page and client.awaiting_login
    page.goto.assert_not_called()
    context.close.assert_not_called()


@pytest.mark.parametrize("for_booking", [False, True])
def test_login_immediately_after_otp_window_x_opens_new_challenge_without_waiting_for_maintain(
    tmp_path, monkeypatch, for_booking,
):
    client, old = connected_client(tmp_path, fake_page(SIGN_IN_URL, closed=True, authenticated=False))
    client.awaiting_login = True
    context, keeper = client.context, client.keeper_page
    new = fake_page("about:blank", authenticated=False)
    create = Mock(return_value=new)
    monkeypatch.setattr(client, "_new_work_page", create)
    maintain = Mock(side_effect=AssertionError("Login must not depend on a prior maintenance tick"))
    monkeypatch.setattr(client, "maintain", maintain)
    event = basic_event() if for_booking else None
    client.login(event)
    new.goto.assert_called_once_with(event.details_url if event else ADMIN_HOME, wait_until="domcontentloaded")
    assert client.work_page is new and client.context is context and client.keeper_page is keeper
    assert client.awaiting_login and client.visible and client.is_connected()
    create.assert_called_once()
    maintain.assert_not_called()
    new.bring_to_front.assert_called_once()
    old.goto.assert_not_called()
    keeper.goto.assert_not_called()
    keeper.close.assert_not_called()
    context.close.assert_not_called()
    client.runtime.stop.assert_not_called()


def test_maintain_does_not_create_or_launch_browser_when_none_exists(tmp_path, monkeypatch):
    client = BookingComBrowser(tmp_path)
    launch = Mock()
    monkeypatch.setattr(client, "_launch", launch)
    assert client.maintain() == "idle"
    launch.assert_not_called()
    assert client.context is None


def test_maintain_waits_for_otp_without_navigation_or_hiding(tmp_path, monkeypatch):
    client, page = connected_client(tmp_path, fake_page(SIGN_IN_URL, authenticated=False))
    client.awaiting_login = True
    background = Mock()
    monkeypatch.setattr(client, "background", background)
    assert client.maintain() == "waiting"
    assert client.awaiting_login and client.visible
    page.goto.assert_not_called()
    background.assert_not_called()
    client.context.new_page.assert_not_called()


def test_maintain_verified_login_auto_backgrounds_same_live_context_once(tmp_path, monkeypatch):
    client, page = connected_client(tmp_path)
    client.awaiting_login = True
    context = client.context
    background = Mock()
    monkeypatch.setattr(client, "background", background)
    assert client.maintain() == "authenticated"
    assert not client.awaiting_login and client.context is context and client.work_page is page
    background.assert_called_once()
    context.close.assert_not_called()
    page.goto.assert_not_called()
    assert client.maintain() == "idle"
    background.assert_called_once()


def test_maintain_verified_login_reports_visible_when_backgrounding_fails(tmp_path, monkeypatch):
    client, page = connected_client(tmp_path)
    client.awaiting_login = True
    background = Mock(return_value=False)
    monkeypatch.setattr(client, "background", background)
    assert client.maintain() == "authenticated-visible"
    assert client.visible and not client.awaiting_login and client.work_page is page and client.is_connected()
    background.assert_called_once()
    client.context.close.assert_not_called()
    page.goto.assert_not_called()
    assert client.maintain() == "idle"
    background.assert_called_once()


def test_maintain_reports_closed_login_tab_without_secret_or_auto_navigation(tmp_path):
    client, page = connected_client(tmp_path, fake_page(SIGN_IN_URL, closed=True, authenticated=False))
    client.awaiting_login = True
    assert client.maintain() == "closed"
    assert client.is_connected()  # The keeper preserves the owned process, not an authenticated OTP tab.
    client.context.close.assert_not_called()
    client.context.new_page.assert_not_called()
    page.goto.assert_not_called()


@pytest.mark.parametrize("acknowledged", [False, True])
def test_worker_recoverable_fetch_error_keeps_browser_and_pending_then_enriches_without_alert(tmp_path, acknowledged):
    state, events, factory = StateStore(tmp_path / "state.json"), queue.Queue(), Mock()
    event = basic_event()
    pending = state.register_today_confirmation(event, ("synthetic-mail",), date.today())
    if acknowledged:
        state.acknowledge(pending)
    client = factory.return_value
    client.is_connected.return_value = True
    client.fetch.side_effect = RuntimeError("unsafe https://admin.booking.com/?ses=synthetic-secret otp=654321")
    worker = BookingComWorker(events, state, {"booking_com_enrichment": True}, tmp_path, factory)
    worker.refresh()
    assert worker.client is client
    client.close.assert_not_called()
    assert len(state.history()) == (1 if acknowledged else 0)
    assert len(state.pending_for_date(date.today())) == (0 if acknowledged else 1)
    statuses = [str(payload) for kind, payload in list(events.queue) if kind == "booking_com_status"]
    assert statuses and all("synthetic-secret" not in text and "654321" not in text and "https://" not in text
                            for text in statuses)
    assert "synthetic-secret" not in state.path.read_text(encoding="utf-8")
    assert "654321" not in state.path.read_text(encoding="utf-8")
    worker.retry_after.clear()
    client.fetch.side_effect = lambda candidate: full_event(candidate)
    worker.refresh()
    worker.refresh()
    client.close.assert_not_called()
    assert client.fetch.call_count == 2 and worker.client is client
    kinds = [kind for kind, _ in list(events.queue)]
    assert kinds.count("history_changed") == 1 and "alert" not in kinds
    assert not state.booking_com_candidates(date.today())
    assert "synthetic-secret" not in state.path.read_text(encoding="utf-8")
    if acknowledged:
        assert len(state.history()) == 1 and not state.pending_for_date(date.today())
    else:
        assert state.pending_for_date(date.today())[0].guest_name == "SYNTHETIC FULL GUEST"


def test_worker_maintains_login_even_without_any_booking_candidates(tmp_path):
    events, factory = queue.Queue(), Mock()
    worker = BookingComWorker(events, StateStore(tmp_path / "state.json"), {"booking_com_enrichment": True}, tmp_path, factory)
    client = Mock()
    client.maintain.return_value = "authenticated"
    worker.client, worker.login_polling = client, True
    worker.commands = Mock()
    worker.commands.get.side_effect = [queue.Empty, ("stop", None)]
    worker.run()
    assert client.maintain.called
    factory.assert_not_called()
    client.fetch.assert_not_called()
    assert "alert" not in [kind for kind, _ in list(events.queue)]
    client.close.assert_called_once()  # Only the explicit Stop/finally path disposes it.


def test_worker_authenticated_visible_status_does_not_pretend_window_is_backgrounded(tmp_path):
    events, factory = queue.Queue(), Mock()
    worker = BookingComWorker(events, StateStore(tmp_path / "state.json"), {"booking_com_enrichment": True}, tmp_path, factory)
    client = Mock()
    client.maintain.return_value = "authenticated-visible"
    worker.client, worker.login_polling = client, True
    worker.commands = Mock()
    worker.commands.get.side_effect = [queue.Empty, ("stop", None)]
    worker.run()
    statuses = [str(payload) for kind, payload in list(events.queue) if kind == "booking_com_status"]
    assert statuses and any("chưa ẩn" in status for status in statuses)
    assert all("chạy nền" not in status for status in statuses)
    assert not worker.login_polling
    factory.assert_not_called()
    client.fetch.assert_not_called()
    client.close.assert_called_once()


def test_worker_background_command_is_queued_and_executed_on_own_thread(tmp_path):
    events, factory = queue.Queue(), Mock()
    worker = BookingComWorker(events, StateStore(tmp_path / "state.json"), {"booking_com_enrichment": False}, tmp_path, factory)
    client, handled, owners = Mock(), threading.Event(), []
    client.maintain.return_value = "idle"

    def background():
        owners.append(threading.get_ident())
        handled.set()
        return True

    client.background.side_effect = background
    worker.client = client
    worker.background()
    client.background.assert_not_called()
    worker.start()
    try:
        assert handled.wait(timeout=3), "Background command was not serviced by the owning worker"
        assert owners == [worker.ident] and worker.ident != threading.get_ident()
    finally:
        worker.close(timeout=3)
    assert not worker.is_alive()
    client.close.assert_called_once()
    kinds = [kind for kind, _ in list(events.queue)]
    assert "booking_com_status" in kinds and "alert" not in kinds


@pytest.mark.parametrize("command", ["background", "wake"])
def test_worker_browser_lifecycle_exception_status_is_sanitized_and_never_alerts(tmp_path, command):
    events, factory = queue.Queue(), Mock()
    worker = BookingComWorker(events, StateStore(tmp_path / "state.json"), {"booking_com_enrichment": True}, tmp_path, factory)
    client = Mock()
    client.background.side_effect = RuntimeError("https://admin.booking.com/?ses=synthetic-secret otp=654321")
    client.maintain.side_effect = RuntimeError("Cookie: synthetic-secret; otp=654321")
    worker.client = client
    worker.commands.put((command, None))
    worker.commands.put(("stop", None))
    worker.run()
    statuses = [str(payload) for kind, payload in list(events.queue) if kind == "booking_com_status"]
    assert statuses and all("synthetic-secret" not in text and "654321" not in text and "https://" not in text
                            for text in statuses)
    assert "alert" not in [kind for kind, _ in list(events.queue)]
    factory.assert_not_called()
    client.fetch.assert_not_called()
    client.close.assert_called_once()


def test_connection_probe_handles_missing_or_disconnected_context_without_creating_it(tmp_path):
    client = BookingComBrowser(tmp_path)
    assert not client.is_connected()
    client.context = SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: False))
    assert not client.is_connected()

"""Authenticated staggered detail rendering; no real browser, account or network."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

import booking_notifier.booking_com_browser as browser_module
from booking_notifier.booking_com import DETAIL_PATH, DETAIL_SNAPSHOT_JS, canonical_details_url
from booking_notifier.booking_com_browser import AUTHENTICATED_PAGE_JS, LOGIN_REQUIRED, BookingComBrowser
from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore


class SyntheticClock:
    def __init__(self):
        self.seconds = 100.0

    def monotonic(self):
        return self.seconds


class HydratingPage:
    """Each navigation restarts hydration, matching a client-rendered detail page."""
    def __init__(self, clock, url, stages, challenge_after=None):
        self.clock, self.details_url, self.stages = clock, url, stages
        self.challenge_after = challenge_after
        self.navigation_started = clock.seconds
        self.goto_calls, self.snapshot_calls, self.waits = [], [], []
        self.locators = Mock()
        self.close = Mock()

    @property
    def elapsed(self):
        return self.clock.seconds - self.navigation_started

    @property
    def challenged(self):
        return self.challenge_after is not None and self.elapsed >= self.challenge_after

    @property
    def url(self):
        if self.challenged:
            return "https://account.booking.com/sign-in?synthetic-only"
        return self.details_url

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        self.navigation_started = self.clock.seconds

    def locator(self, *args, **kwargs):
        # A name becoming visible alone is not a readiness signal. No fake
        # locator wait advances the independent, full-snapshot hydration clock.
        return self.locators

    def evaluate(self, expression, *args):
        if expression == AUTHENTICATED_PAGE_JS:
            return not self.challenged
        assert expression == DETAIL_SNAPSHOT_JS, "Only allowlisted detail/auth DOM reads are expected"
        self.snapshot_calls.append(self.elapsed)
        current = self.stages[0][1]
        for ready_after, snapshot in self.stages:
            if ready_after <= self.elapsed:
                current = snapshot
        return deepcopy(current)

    def wait_for_timeout(self, milliseconds):
        assert milliseconds > 0
        self.waits.append(milliseconds)
        self.clock.seconds += milliseconds / 1000

    def is_closed(self):
        return False


def basic_event():
    return BookingEvent(
        source="Booking.com", booking_id="5550000001", checkin_date=date.today(),
        details_url=canonical_details_url(
            f"https://admin.booking.com{DETAIL_PATH}?res_id=5550000001&hotel_id=12345",
        ),
    )


def complete_snapshot(event):
    return {
        "url": event.details_url, "names": ["SYNTHETIC FULL GUEST"],
        "rooms": ["Deluxe Room", "Deluxe Room", "Triple Room"],
        "fields": [
            ["Mã số đặt phòng", event.booking_id], ["Nhận phòng", event.checkin_date.isoformat()],
            ["Trả phòng", (event.checkin_date + timedelta(days=2)).isoformat()],
            ["Tổng số căn", "3"], ["Tổng tiền phòng", "VND 1.200.000"],
        ],
    }


def client_with_page(tmp_path, monkeypatch, stages, *, challenge_after=None):
    event, clock = basic_event(), SyntheticClock()
    page = HydratingPage(clock, event.details_url, stages, challenge_after)
    client = BookingComBrowser(tmp_path / "never-created-synthetic-browser-profile")
    client.context, client.runtime = Mock(), Mock()
    client.keeper_page, client.work_page = Mock(), page
    client._launch = Mock()
    client._page = Mock(return_value=page)
    client.background = Mock(return_value=True)
    monkeypatch.setattr(browser_module.time, "monotonic", clock.monotonic)
    return event, client, page, clock


def assert_kept_session(client, page):
    client.context.close.assert_not_called()
    client.runtime.stop.assert_not_called()
    page.close.assert_not_called()


def test_guest_visible_before_amount_and_room_hydration_eventually_reads_complete_same_page(tmp_path, monkeypatch):
    event = basic_event()
    full = complete_snapshot(event)
    initial = {**full, "rooms": [], "fields": full["fields"][:3]}
    event, client, page, _clock = client_with_page(tmp_path, monkeypatch, [(0, initial), (0.75, full)])
    original = event.to_dict()
    result = client.fetch(event)
    assert result.guest_name == "SYNTHETIC FULL GUEST"
    assert result.room_type == "Deluxe Room x2; Triple Room x1" and result.total_revenue == "VND 1.200.000"
    assert result.nights == 2 and result.details_loaded_at
    assert len(page.goto_calls) == 1 and len(page.snapshot_calls) >= 2 and page.waits
    assert page.snapshot_calls[0] < 0.75 <= page.snapshot_calls[-1]
    assert event.to_dict() == original
    assert_kept_session(client, page)


def test_initial_empty_guest_and_fields_can_hydrate_without_failed_early_snapshot_or_reload(tmp_path, monkeypatch):
    full = complete_snapshot(basic_event())
    empty = {"url": full["url"], "names": [], "rooms": [], "fields": []}
    only_name = {**empty, "names": full["names"]}
    event, client, page, _clock = client_with_page(tmp_path, monkeypatch, [(0, empty), (0.25, only_name), (1.0, full)])
    result = client.fetch(event)
    assert result.guest_name == full["names"][0] and result.checkout_date == date.today() + timedelta(days=2)
    assert len(page.goto_calls) == 1 and page.snapshot_calls[-1] >= 1.0
    assert page.waits and not client.awaiting_login
    assert_kept_session(client, page)


@pytest.mark.parametrize("delay", [0.25, 1.5, 3.0])
def test_repeated_slow_render_fetches_navigate_once_each_and_do_not_restart_session(tmp_path, monkeypatch, delay):
    full = complete_snapshot(basic_event())
    early = {**full, "fields": full["fields"][:-1]}
    event, client, page, _clock = client_with_page(tmp_path, monkeypatch, [(0, early), (delay, full)])
    results = [client.fetch(event) for _attempt in range(3)]
    assert all(result.total_revenue == "VND 1.200.000" for result in results)
    assert len(page.goto_calls) == 3 and len(page.snapshot_calls) >= 6
    assert not client.awaiting_login
    assert_kept_session(client, page)


def test_login_challenge_appearing_while_hydrating_stops_poll_without_navigating_otp(tmp_path, monkeypatch):
    full = complete_snapshot(basic_event())
    early = {**full, "fields": full["fields"][:-1]}
    event, client, page, clock = client_with_page(
        tmp_path, monkeypatch, [(0, early), (5, full)], challenge_after=0.5,
    )
    before = event.to_dict()
    with pytest.raises(browser_module.BookingComBrowserError) as failure:
        client.fetch(event)
    assert str(failure.value) == LOGIN_REQUIRED and client.awaiting_login
    assert len(page.goto_calls) == 1 and page.challenged
    assert clock.seconds - page.navigation_started < 5
    assert event.to_dict() == before
    client.background.assert_not_called()
    assert_kept_session(client, page)


@pytest.mark.parametrize("mismatch", ["booking", "property", "arrival", "room-count"])
def test_permanent_identity_or_allocation_mismatch_never_commits_details_or_guesses(tmp_path, monkeypatch, mismatch):
    full = complete_snapshot(basic_event())
    if mismatch == "booking":
        full["fields"][0][1] = "5550000002"
    elif mismatch == "property":
        full["url"] = full["url"].replace("hotel_id=12345", "hotel_id=12346")
    elif mismatch == "arrival":
        full["fields"][1][1] = (date.today() + timedelta(days=1)).isoformat()
    else:
        full["rooms"] = full["rooms"][:1]
    event, client, page, clock = client_with_page(tmp_path, monkeypatch, [(0, full)])
    state = StateStore(tmp_path / "state.json")
    state.register_today_confirmation(event, ("synthetic-confirmation",), date.today())
    state.acknowledge(event)  # Already closed history is also protected from unverified data.
    saved = state.path.read_bytes()
    before = event.to_dict()
    with pytest.raises(browser_module.BookingComReservationError) as failure:
        result = client.fetch(event)
        state.enrich_booking_com(result, date.today())
    assert "https://" not in str(failure.value)
    assert event.to_dict() == before and state.path.read_bytes() == saved
    assert state.history()[0]["guest_name"] == "" and state.history()[0]["room_type"] == ""
    assert len(page.goto_calls) == 1 and 0 < clock.seconds - page.navigation_started <= 30
    assert not client.awaiting_login
    assert_kept_session(client, page)


@pytest.mark.parametrize("missing", ["name", "room", "units", "amount", "checkout"])
def test_terminal_missing_fields_have_bounded_same_page_wait_and_safe_per_booking_error(tmp_path, monkeypatch, missing):
    full = complete_snapshot(basic_event())
    if missing == "name":
        full["names"] = []
    elif missing == "room":
        full["rooms"] = []
    else:
        labels = {"units": "Tổng số căn", "amount": "Tổng tiền phòng", "checkout": "Trả phòng"}
        full["fields"] = [pair for pair in full["fields"] if pair[0] != labels[missing]]
    event, client, page, clock = client_with_page(tmp_path, monkeypatch, [(0, full)])
    before = event.to_dict()
    with pytest.raises(browser_module.BookingComReservationError) as failure:
        client.fetch(event)
    assert str(failure.value) and "https://" not in str(failure.value)
    assert len(page.goto_calls) == 1 and len(page.snapshot_calls) >= 2
    assert 0 < clock.seconds - page.navigation_started <= 30
    assert page.waits and not client.awaiting_login and event.to_dict() == before
    assert_kept_session(client, page)

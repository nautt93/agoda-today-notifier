"""Authenticated per-booking failures must not starve later arrivals or replay alerts."""
from __future__ import annotations

import queue
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

import booking_notifier.booking_com_browser as browser_module
from booking_notifier.booking_com import canonical_details_url
from booking_notifier.booking_com_browser import (
    LOGIN_REQUIRED,
    BookingComBrowserError,
    BookingComReservationError,
    BookingComWorker,
)
from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore


def basic_event(index):
    booking_id = str(5550000000 + index)
    return BookingEvent(
        source="Booking.com", booking_id=booking_id, checkin_date=date.today(),
        details_url=canonical_details_url(
            f"https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/booking.html?res_id={booking_id}&hotel_id=12345",
        ),
    )


def complete(event):
    return replace(event, guest_name="SYNTHETIC FULL GUEST", room_type="Deluxe Room x2",
                   checkout_date=date.today() + timedelta(days=2), total_revenue="VND 800.000",
                   details_loaded_at="2026-10-08T12:00:00")


def worker_for(tmp_path, monkeypatch, *, acknowledged=False, count=3):
    state, events, client = StateStore(tmp_path / "state.json"), queue.Queue(), Mock()
    basics = [basic_event(index) for index in range(1, count + 1)]
    for event in basics:
        queued = state.register_today_confirmation(event, (f"synthetic-mail-{event.booking_id}",), date.today())
        if acknowledged:
            state.acknowledge(queued)
    client.fetch.side_effect = complete
    factory = Mock(return_value=client)
    worker = BookingComWorker(events, state, {"booking_com_enrichment": True}, tmp_path, factory)
    clock = [0.0]
    monkeypatch.setattr(browser_module.time, "monotonic", lambda: clock[0])
    return worker, state, events, client, basics, clock


@pytest.mark.parametrize("acknowledged", [False, True])
def test_permanent_first_reservation_failure_never_starves_other_two_on_sixty_second_ticks(
    tmp_path, monkeypatch, acknowledged,
):
    worker, state, events, client, basics, clock = worker_for(tmp_path, monkeypatch, acknowledged=acknowledged)
    before = list(state.data["processed_keys"])
    attempts = []

    def fetch(event):
        attempts.append(event.booking_id)
        if event.booking_id == basics[0].booking_id:
            raise BookingComReservationError("Booking.com: chưa lấy đủ chi tiết cho booking này; app sẽ thử lại.")
        return complete(event)

    client.fetch.side_effect = fetch
    for tick in (0.0, 60.0, 120.0, 180.0):
        clock[0] = tick
        worker.refresh()
    assert attempts == [basics[0].booking_id, basics[1].booking_id, basics[2].booking_id,
                        basics[0].booking_id, basics[0].booking_id, basics[0].booking_id]
    assert [candidate.booking_id for candidate in state.booking_com_candidates(date.today())] == [basics[0].booking_id]
    assert worker.retry_after == {basics[0].storage_id: 240.0}
    client.close.assert_not_called()
    kinds = [kind for kind, _ in list(events.queue)]
    assert kinds.count("history_changed") == 2 and "alert" not in kinds
    assert state.data["processed_keys"] == before
    records = state.history() if acknowledged else state.pending_for_date(date.today())
    assert len(records) == 3
    if acknowledged:
        assert not state.pending_for_date(date.today())
        assert sum(bool(record["guest_name"]) for record in records) == 2
    else:
        assert len(state.history()) == 0
        assert sum(bool(record.guest_name) for record in records) == 2


def test_every_failed_reservation_keeps_its_own_retry_deadline_without_restarting_browser(tmp_path, monkeypatch):
    worker, state, events, client, basics, clock = worker_for(tmp_path, monkeypatch)
    client.fetch.side_effect = BookingComReservationError("Booking.com: chưa lấy đủ chi tiết cho booking này.")
    worker.refresh()
    assert client.fetch.call_count == 3
    assert worker.retry_after == {event.storage_id: 60.0 for event in basics}
    clock[0] = 59.999
    worker.refresh()
    assert client.fetch.call_count == 3
    clock[0] = 60.0
    worker.refresh()
    assert client.fetch.call_count == 6 and len(state.pending_for_date(date.today())) == 3
    client.close.assert_not_called()
    assert "alert" not in [kind for kind, _ in list(events.queue)]


@pytest.mark.parametrize("failure", [BookingComBrowserError(LOGIN_REQUIRED), RuntimeError("synthetic global browser failure")])
def test_shared_auth_or_unknown_browser_failure_stops_batch_and_preserves_otp_and_all_pending(
    tmp_path, monkeypatch, failure,
):
    worker, state, events, client, basics, _clock = worker_for(tmp_path, monkeypatch)
    client.fetch.side_effect = failure
    worker.refresh()
    assert client.fetch.call_count == 1
    assert client.fetch.call_args.args[0].booking_id == basics[0].booking_id
    assert worker.retry_after == {basics[0].storage_id: 60.0}
    assert len(state.pending_for_date(date.today())) == 3
    assert all(not candidate.guest_name for candidate in state.booking_com_candidates(date.today()))
    client.close.assert_not_called()
    assert "history_changed" not in [kind for kind, _ in list(events.queue)]
    assert "alert" not in [kind for kind, _ in list(events.queue)]


@pytest.mark.parametrize("command", ["login", "configure", "stop"])
def test_pending_command_before_refresh_is_serviced_before_any_reservation_fetch(tmp_path, monkeypatch, command):
    worker, state, _events, client, _basics, _clock = worker_for(tmp_path, monkeypatch)
    worker.commands.put((command, None))
    worker.refresh()
    client.fetch.assert_not_called()
    assert worker.commands.get_nowait() == (command, None)
    assert len(state.pending_for_date(date.today())) == 3


@pytest.mark.parametrize("command", ["login", "configure", "stop"])
def test_command_queued_during_failed_fetch_yields_before_next_reservation(tmp_path, monkeypatch, command):
    worker, state, events, client, _basics, _clock = worker_for(tmp_path, monkeypatch)

    def fetch(_event):
        worker.commands.put((command, None))
        raise BookingComReservationError("Booking.com: chưa lấy đủ chi tiết cho booking này.")

    client.fetch.side_effect = fetch
    worker.refresh()
    assert client.fetch.call_count == 1
    assert worker.commands.get_nowait() == (command, None)
    assert len(state.pending_for_date(date.today())) == 3
    client.close.assert_not_called()
    assert "alert" not in [kind for kind, _ in list(events.queue)]


def test_command_queued_during_success_also_yields_without_second_fetch_or_alert(tmp_path, monkeypatch):
    worker, state, events, client, _basics, _clock = worker_for(tmp_path, monkeypatch)

    def fetch(event):
        worker.commands.put(("login", None))
        return complete(event)

    client.fetch.side_effect = fetch
    worker.refresh()
    assert client.fetch.call_count == 1 and worker.commands.get_nowait() == ("login", None)
    assert len(state.booking_com_candidates(date.today())) == 2
    kinds = [kind for kind, _ in list(events.queue)]
    assert kinds.count("history_changed") == 1 and "alert" not in kinds


def test_worker_batch_is_bounded_to_twenty_candidates_even_with_synthetic_oversized_state(tmp_path, monkeypatch):
    worker, state, events, client, basics, _clock = worker_for(tmp_path, monkeypatch, count=25)
    state.booking_com_candidates = Mock(return_value=basics)
    client.fetch.side_effect = BookingComReservationError("Booking.com: chưa lấy đủ chi tiết cho booking này.")
    worker.refresh()
    assert client.fetch.call_count == 20
    assert [call.args[0].booking_id for call in client.fetch.call_args_list] == [event.booking_id for event in basics[:20]]
    assert len(worker.retry_after) == 20
    client.close.assert_not_called()
    assert "alert" not in [kind for kind, _ in list(events.queue)]


def test_repaired_first_reservation_recovers_on_retry_without_extra_notification(tmp_path, monkeypatch):
    worker, state, events, client, basics, clock = worker_for(tmp_path, monkeypatch)
    client.fetch.side_effect = [BookingComReservationError("Booking.com: chưa lấy đủ chi tiết cho booking này."),
                                complete(basics[1]), complete(basics[2])]
    worker.refresh()
    assert len(state.booking_com_candidates(date.today())) == 1
    clock[0] = 60.0
    client.fetch.side_effect = complete
    worker.refresh()
    worker.refresh()
    assert client.fetch.call_count == 4 and not worker.retry_after
    assert not state.booking_com_candidates(date.today())
    assert len(state.pending_for_date(date.today())) == 3
    kinds = [kind for kind, _ in list(events.queue)]
    assert kinds.count("history_changed") == 3 and "alert" not in kinds

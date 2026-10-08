"""F92 notification suspension with synthetic clients only; never open serial/USB."""
from __future__ import annotations

import queue
import threading
from datetime import date
from unittest.mock import Mock

import pytest

import booking_notifier.f92_device as f92_module
from booking_notifier.f92_device import F92Worker
from booking_notifier.models import BookingEvent


class SyntheticClient:
    def __init__(self, settings, cancel_event):
        self.settings, self.cancel_event = settings, cancel_event
        self.calls = []
        self.sounds = []
        self.idle_received, self.notify_received = threading.Event(), threading.Event()

    def close(self):
        self.calls.append(("close",))

    def notify(self, alert, play_sound=True):
        self.calls.append(("notify", alert, play_sound))
        if play_sound and self.settings.builtin_sound_enabled:
            self.sounds.append(self.settings.sound_index)
        self.notify_received.set()
        return "synthetic booking framebuffer"

    def show_idle(self):
        self.calls.append(("idle",))
        self.idle_received.set()
        return "synthetic idle framebuffer"

    def test_device(self):
        self.calls.append(("test",))
        if self.settings.builtin_sound_enabled:
            self.sounds.append(self.settings.sound_index)
        return self.settings.port


@pytest.fixture
def make_worker(monkeypatch):
    # A regression must fail loudly if any test tries resolving/opening hardware.
    serial_open, discover = Mock(side_effect=AssertionError("No physical serial I/O")), Mock(
        side_effect=AssertionError("No physical USB enumeration"),
    )
    monkeypatch.setattr(f92_module.serial, "Serial", serial_open)
    monkeypatch.setattr(f92_module.list_ports, "comports", discover)
    monkeypatch.setattr(f92_module, "F92Client", SyntheticClient)

    def create(**settings):
        config = {"f92_enabled": True, "f92_port": "COM8", "f92_builtin_sound_enabled": True,
                  "f92_sound_index": 4, **settings}
        return F92Worker(queue.Queue(), config)

    yield create
    serial_open.assert_not_called()
    discover.assert_not_called()


def event(booking_id="SYNTHETIC-BOOKING"):
    return BookingEvent(source="Booking.com", booking_id=booking_id, checkin_date=date.today(),
                        guest_name="SYNTHETIC FULL GUEST", room_type="Deluxe Room x1")


def run_queued(worker):
    worker.operations.put(("stop", None))
    worker.run()


def calls(worker, operation):
    return [call for call in worker.client.calls if call[0] == operation]


def test_suspension_event_and_idle_are_updated_only_on_false_to_true_transition(make_worker):
    worker = make_worker()
    assert isinstance(worker.notifications_suspended, threading.Event)
    assert not worker.notifications_suspended.is_set() and worker.operations.empty()
    worker.set_notifications_suspended(True)
    assert worker.notifications_suspended.is_set()
    assert list(worker.operations.queue) == [("idle", None)]
    worker.idle_pending.clear()  # Even after an earlier idle completes, repeated True is not a transition.
    worker.set_notifications_suspended(True)
    assert list(worker.operations.queue) == [("idle", None)]
    worker.set_notifications_suspended(False)
    assert not worker.notifications_suspended.is_set()
    assert list(worker.operations.queue) == [("idle", None)]
    worker.set_notifications_suspended(True)
    assert list(worker.operations.queue) == [("idle", None), ("idle", None)]


@pytest.mark.parametrize("play_sound", [False, True])
def test_notify_during_quiet_is_not_enqueued_and_external_booking_remains_pending(make_worker, play_sound):
    worker, alert = make_worker(), event()
    external_pending = [alert]
    before = alert.to_dict()
    worker.set_notifications_suspended(True)
    worker.notify(alert, play_sound=play_sound)
    assert list(worker.operations.queue) == [("idle", None)]
    run_queued(worker)
    assert len(calls(worker, "idle")) == 1 and not calls(worker, "notify")
    assert worker.client.sounds == []
    assert external_pending == [alert] and alert.to_dict() == before


@pytest.mark.parametrize("play_sound", [False, True])
def test_notification_queued_before_boundary_is_rechecked_and_skipped_without_hardware_sound(make_worker, play_sound):
    worker, alert = make_worker(), event()
    worker.notify(alert, play_sound=play_sound)
    assert list(worker.operations.queue) == [("notify", (alert, play_sound, 0))]
    worker.set_notifications_suspended(True)
    run_queued(worker)
    assert not calls(worker, "notify") and worker.client.sounds == []
    assert calls(worker, "idle") == [("idle",)] and not worker.idle_pending.is_set()
    statuses = list(worker.event_queue.queue)
    assert statuses == [("f92_status", "F92: đã về synthetic idle framebuffer.")]


def test_resume_allows_one_fresh_notification_without_replaying_dropped_quiet_alerts(make_worker):
    worker, alert = make_worker(), event()
    worker.notify(alert)  # Queued immediately before quiet starts; it must be skipped.
    worker.set_notifications_suspended(True)
    worker.notify(event("DROPPED-DURING-QUIET"))
    worker.start()
    try:
        assert worker.client.idle_received.wait(timeout=3)
        assert not calls(worker, "notify") and worker.client.sounds == []
        worker.set_notifications_suspended(False)
        worker.notify(alert)
        assert worker.client.notify_received.wait(timeout=3)
    finally:
        worker.close(timeout=3)
    assert not worker.is_alive()
    assert calls(worker, "notify") == [("notify", alert, True)]
    assert worker.client.sounds == [4]


def test_suspend_and_resume_before_queue_drains_permanently_invalidates_old_notification(make_worker):
    worker, alert = make_worker(), event()
    worker.notify(alert)
    worker.set_notifications_suspended(True)
    worker.set_notifications_suspended(False)
    worker.notify(alert)  # App resumes the same unacknowledged booking exactly once.
    assert list(worker.operations.queue) == [
        ("notify", (alert, True, 0)), ("idle", None), ("notify", (alert, True, 1)),
    ]
    run_queued(worker)
    assert calls(worker, "notify") == [("notify", alert, True)]
    assert worker.client.sounds == [4]
    assert len(calls(worker, "idle")) == 1
    assert sum("đã báo booking" in str(payload) for kind, payload in worker.event_queue.queue if kind == "f92_status") == 1


def test_resume_epoch_invalidates_old_notifications_without_draining_configure_test_or_idle_order(make_worker):
    worker, old, fresh = make_worker(), event("OLD-BEFORE-QUIET"), event("FRESH-AFTER-RESUME")
    worker.notify(old)
    worker.configure({"f92_enabled": True, "f92_port": "COM12", "f92_builtin_sound_enabled": False})
    worker.test()
    worker.set_notifications_suspended(True)
    worker.set_notifications_suspended(False)
    worker.notify(fresh)
    run_queued(worker)
    assert calls(worker, "notify") == [("notify", fresh, True)]
    assert [call[0] for call in worker.client.calls if call[0] != "close"] == ["test", "idle", "notify"]
    assert worker.client.settings.port == "COM12" and worker.client.sounds == []
    assert list(worker.event_queue.queue) == [
        ("f92_status", "F92: đã cập nhật cấu hình"),
        ("f92_test_result", (True, "Kết nối thành công trên COM12.")),
        ("f92_status", "F92: đã về synthetic idle framebuffer."),
        ("f92_status", "F92: đã báo booking bằng synthetic booking framebuffer."),
    ]


def test_each_quiet_entry_retires_previous_epoch_even_after_multiple_fast_resume_cycles(make_worker):
    worker = make_worker()
    stale_one, stale_two, current = event("EPOCH-0"), event("EPOCH-1"), event("EPOCH-2")
    worker.notify(stale_one)
    worker.set_notifications_suspended(True)
    worker.set_notifications_suspended(True)  # No additional retirement for an unchanged quiet state.
    worker.set_notifications_suspended(False)
    worker.notify(stale_two)
    worker.set_notifications_suspended(True)
    worker.set_notifications_suspended(False)
    worker.notify(current)
    run_queued(worker)
    assert calls(worker, "notify") == [("notify", current, True)]
    assert worker.client.sounds == [4]


def test_configure_and_explicit_device_test_are_not_drained_or_suppressed_by_quiet_flag(make_worker):
    worker = make_worker()
    worker.configure({"f92_enabled": True, "f92_port": "COM12", "f92_sound_index": 2,
                      "f92_builtin_sound_enabled": False})
    worker.test()
    worker.set_notifications_suspended(True)
    worker.notify(event())
    run_queued(worker)
    assert worker.notifications_suspended.is_set()
    assert worker.client.settings.port == "COM12" and worker.client.settings.sound_index == 2
    assert not worker.client.settings.builtin_sound_enabled
    assert calls(worker, "test") == [("test",)] and calls(worker, "idle") == [("idle",)]
    assert not calls(worker, "notify") and worker.client.sounds == []
    assert list(worker.event_queue.queue) == [
        ("f92_status", "F92: đã cập nhật cấu hình"),
        ("f92_test_result", (True, "Kết nối thành công trên COM12.")),
        ("f92_status", "F92: đã về synthetic idle framebuffer."),
    ]


def test_disabled_device_test_still_returns_expected_result_while_quiet(make_worker):
    worker = make_worker(f92_enabled=False)
    worker.set_notifications_suspended(True)
    worker.test()
    run_queued(worker)
    assert list(worker.event_queue.queue) == [("f92_test_result", (False, "F92 đang tắt trong cấu hình."))]
    assert not calls(worker, "notify") and not calls(worker, "test") and not calls(worker, "idle")
    assert not worker.idle_pending.is_set()


def test_notification_after_resume_preserves_existing_play_sound_false_behavior(make_worker):
    worker, alert = make_worker(), event()
    worker.set_notifications_suspended(True)
    worker.set_notifications_suspended(False)
    worker.notify(alert, play_sound=False)
    run_queued(worker)
    assert calls(worker, "notify") == [("notify", alert, False)]
    assert worker.client.sounds == []

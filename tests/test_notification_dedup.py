from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import BookingNotifierApp
from booking_notifier.models import BookingEvent
from booking_notifier.state import StateStore


def notification_app(state):
    app = BookingNotifierApp.__new__(BookingNotifierApp)
    app.state = state
    app.quiet_var = SimpleNamespace(get=lambda: False)
    app.queued_ids = set()
    app.alert_queue = []
    app.active_alert = None
    app._show_next_alert = Mock()
    app.f92_worker = Mock()
    app.play_sound = Mock()
    return app


@pytest.mark.parametrize("source", ["Agoda", "Expedia"])
def test_late_worker_event_after_acknowledgement_cannot_reopen_popup(tmp_path, source):
    state = StateStore(tmp_path / "state.json")
    event = BookingEvent(source=source, booking_id="1234567890", checkin_date=date.today(), guest_name="Test Guest")
    alert = state.register_today_confirmation(event, ("uid:1", "msg:1"), date.today())
    app = notification_app(state)
    # The minute tick sees the persisted pending booking before the worker emits.
    app.enqueue_alert(state.pending_for_date(date.today())[0])
    app._show_next_alert.assert_called_once()
    state.acknowledge(alert)
    app.alert_queue.clear()
    app.queued_ids.clear()
    app.state = StateStore(state.path)
    # The worker's original event arrives after the user has closed the popup.
    app.enqueue_alert(alert)
    app._show_next_alert.assert_called_once()
    assert not app.alert_queue and not app.queued_ids
    assert len(app.state.history()) == 1 and not app.state.pending_for_date(date.today())
    # The same guest may legitimately have a second reservation; do not merge it.
    other = BookingEvent(source=source, booking_id="1234567891", checkin_date=date.today(), guest_name="Test Guest")
    other_alert = app.state.register_today_confirmation(other, ("uid:2",), date.today())
    app.enqueue_alert(other_alert)
    assert app._show_next_alert.call_count == 2 and app.alert_queue == [other_alert]


def test_already_acknowledged_events_inside_popup_queue_are_discarded(tmp_path):
    state = StateStore(tmp_path / "state.json")
    app = notification_app(state)
    for source in ("Agoda", "Expedia"):
        event = BookingEvent(source=source, booking_id="1234567890", checkin_date=date.today())
        alert = state.register_today_confirmation(event, (), date.today())
        app.alert_queue.extend([alert, alert])
        app.queued_ids.add(alert.storage_id)
        state.acknowledge(alert)
    BookingNotifierApp._show_next_alert(app)
    assert app.active_alert is None and not app.alert_queue and not app.queued_ids
    app.f92_worker.idle.assert_called_once()
    app.play_sound.assert_not_called()


def test_yesterdays_queue_cannot_open_after_day_rollover(tmp_path):
    app = notification_app(StateStore(tmp_path / "state.json"))
    stale = BookingEvent(source="Expedia", booking_id="1234567890", checkin_date=date.today() - timedelta(days=1))
    app.alert_queue.append(stale)
    app.queued_ids.add(stale.storage_id)
    BookingNotifierApp._show_next_alert(app)
    assert not app.alert_queue and not app.queued_ids and app.active_alert is None
    app.f92_worker.idle.assert_called_once()

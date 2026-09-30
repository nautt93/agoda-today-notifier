from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import BookingNotifierApp
from booking_notifier.f92_graphics import image_to_rgb565_le, month_grid, render_idle_image


@pytest.mark.parametrize("today", [date(2026, 9, 30), date(2026, 8, 31), date(2028, 2, 29), date(2027, 1, 1)])
def test_calendar_includes_entire_month_and_six_weeks(today):
    days = [day for week in month_grid(today) for day in week]
    assert len(days) == 42
    assert days[0].weekday() == 0
    assert days[-1].weekday() == 6
    assert today in days
    assert today.replace(day=1) in days
    assert all(b - a == timedelta(days=1) for a, b in zip(days, days[1:], strict=False))


def test_idle_frame_updates_at_midnight_and_is_valid_f92_frame():
    before = render_idle_image(datetime(2026, 9, 30, 23, 59))
    after = render_idle_image(datetime(2026, 10, 1, 0, 0))
    assert before.size == after.size == (320, 480)
    assert before.tobytes() != after.tobytes()
    assert len(image_to_rgb565_le(after)) == 320 * 480 * 2


def test_clock_refresh_does_not_overwrite_booking():
    app = SimpleNamespace(active_alert=object(), alert_queue=[], f92_worker=Mock(), root=Mock(),
                          _f92_clock_tick=Mock())
    BookingNotifierApp._f92_clock_tick(app)
    app.f92_worker.idle.assert_not_called()
    app.root.after.assert_called_once()
    app.active_alert = None
    BookingNotifierApp._f92_clock_tick(app)
    app.f92_worker.idle.assert_called_once()


def test_empty_alert_queue_restores_calendar():
    app = SimpleNamespace(active_alert=None, alert_queue=[], f92_worker=Mock())
    BookingNotifierApp._show_next_alert(app)
    app.f92_worker.idle.assert_called_once()

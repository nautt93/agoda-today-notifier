"""Pure scheduling tests: no network, app startup or reliance on today's clock."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

import booking_notifier.quiet_hours as quiet_module
from booking_notifier.quiet_hours import normalize_quiet_times, quiet_hours_active


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 10, 8, hour, minute, second)


@pytest.mark.parametrize(("start", "end", "expected"), [
    ("00:00", "08:00", ("00:00", "08:00")),
    ("22:30", "06:15", ("22:30", "06:15")),
    (" 1:05 ", " 8:00\n", ("01:05", "08:00")),
    ("9:00", "17:59", ("09:00", "17:59")),
    ("23:59", "00:00", ("23:59", "00:00")),
    ("00:00", "23:59", ("00:00", "23:59")),
])
def test_normalize_valid_times_returns_canonical_24_hour_pair(start, end, expected):
    assert normalize_quiet_times(start, end) == expected


@pytest.mark.parametrize("invalid", [
    "", " ", "24:00", "25:15", "23:60", "08:99", "8:0", "08:5", "008:05",
    "-1:05", "+1:05", "1.5:00", "8 :00", "08: 00", "08:00:00", "08:00 AM",
    "８:００", "١:٠٥", None, False, 800, b"08:00", [], {},
])
@pytest.mark.parametrize("field", ["start", "end"])
def test_normalize_rejects_invalid_or_nonstring_endpoint_with_clear_vietnamese_error(invalid, field):
    start, end = (invalid, "08:00") if field == "start" else ("00:00", invalid)
    with pytest.raises(ValueError, match="không hợp lệ.*HH:MM.*00:00.*23:59") as error:
        normalize_quiet_times(start, end)
    assert ("bắt đầu" if field == "start" else "kết thúc") in str(error.value)


@pytest.mark.parametrize(("start", "end"), [
    ("00:00", "00:00"), ("08:00", "08:00"), ("23:59", "23:59"), (" 1:05 ", "01:05"),
])
def test_equal_normalized_endpoints_are_rejected_not_treated_as_24_hours(start, end):
    with pytest.raises(ValueError, match="bắt đầu.*kết thúc.*khác nhau"):
        normalize_quiet_times(start, end)


@pytest.mark.parametrize(("when", "expected"), [
    (at(0), True), (at(0, 0, 59), True), (at(7, 59, 59), True),
    (at(8), False), (at(8, 0, 59), False), (at(12), False), (at(23, 59, 59), False),
])
def test_legacy_missing_keys_default_enabled_midnight_to_eight(when, expected):
    assert quiet_hours_active({}, when) is expected
    assert quiet_hours_active({"quiet_hours_enabled": True}, when) is expected


@pytest.mark.parametrize(("when", "expected"), [
    (at(9, 14, 59), False), (at(9, 15), True), (at(9, 15, 59), True),
    (at(12), True), (at(17, 44, 59), True), (at(17, 45), False), (at(17, 45, 59), False),
    (at(0), False), (at(23, 59, 59), False),
])
def test_daytime_custom_range_includes_start_excludes_end_ignoring_seconds(when, expected):
    config = {"quiet_start_time": "09:15", "quiet_end_time": "17:45"}
    assert quiet_hours_active(config, when) is expected


@pytest.mark.parametrize(("when", "expected"), [
    (at(22, 29, 59), False), (at(22, 30), True), (at(22, 30, 59), True),
    (at(23, 59, 59), True), (at(0), True), (at(6, 14, 59), True),
    (at(6, 15), False), (at(6, 15, 59), False), (at(12), False),
])
def test_overnight_custom_range_crosses_midnight_with_exact_minute_edges(when, expected):
    config = {"quiet_hours_enabled": True, "quiet_start_time": "22:30", "quiet_end_time": "06:15"}
    assert quiet_hours_active(config, when) is expected


@pytest.mark.parametrize(("when", "expected"), [
    (at(23, 58, 59), False), (at(23, 59), True), (at(0), True), (at(0, 0, 59), True), (at(0, 1), False),
])
def test_short_overnight_range_handles_midnight_without_losing_end_minute(when, expected):
    assert quiet_hours_active({"quiet_start_time": "23:59", "quiet_end_time": "00:01"}, when) is expected


@pytest.mark.parametrize("when", [at(0), at(6), at(12), at(23)])
def test_disabled_quiet_hours_never_silences_even_with_invalid_schedule(when, monkeypatch):
    normalize = Mock(side_effect=AssertionError("Disabled quiet hours must not validate or apply a schedule"))
    monkeypatch.setattr(quiet_module, "normalize_quiet_times", normalize)
    assert quiet_hours_active({"quiet_hours_enabled": False, "quiet_start_time": "invalid", "quiet_end_time": None}, when) is False
    normalize.assert_not_called()


@pytest.mark.parametrize(("start", "end"), [
    ("invalid", "23:59"), ("22:30", "invalid"), ("bad", "bad"),
    ("12:00", "12:00"), ("1:05", "01:05"), (None, "22:00"), ("22:00", None),
    ([], "20:00"), ("20:00", {}), ("24:00", "23:59"), ("22:30", "06:60"),
])
def test_corrupt_stored_values_fall_back_as_whole_legacy_pair_without_partial_repair(start, end):
    config = {"quiet_hours_enabled": True, "quiet_start_time": start, "quiet_end_time": end}
    assert quiet_hours_active(config, at(4)) is True
    assert quiet_hours_active(config, at(8)) is False
    assert quiet_hours_active(config, at(12)) is False
    assert quiet_hours_active(config, at(23)) is False


def test_missing_one_endpoint_uses_that_keys_legacy_default_when_pair_remains_valid():
    assert quiet_hours_active({"quiet_start_time": "22:30"}, at(23))
    assert quiet_hours_active({"quiet_start_time": "22:30"}, at(7, 59, 59))
    assert not quiet_hours_active({"quiet_start_time": "22:30"}, at(8))
    assert quiet_hours_active({"quiet_end_time": "02:15"}, at(2, 14, 59))
    assert not quiet_hours_active({"quiet_end_time": "02:15"}, at(2, 15))


def test_normalized_user_times_work_at_runtime_without_mutating_config():
    config = {"quiet_hours_enabled": True, "quiet_start_time": " 1:05 ", "quiet_end_time": " 8:00\n"}
    before = dict(config)
    assert not quiet_hours_active(config, at(1, 4, 59))
    assert quiet_hours_active(config, at(1, 5))
    assert not quiet_hours_active(config, at(8))
    assert config == before


def test_default_now_uses_local_clock_from_datetime_when_argument_is_omitted(monkeypatch):
    class FixedClock:
        @staticmethod
        def now():
            return at(6, 15, 59)

    monkeypatch.setattr(quiet_module, "datetime", FixedClock)
    assert quiet_hours_active({})
    assert not quiet_hours_active({"quiet_start_time": "22:30", "quiet_end_time": "06:15"})


def test_explicit_timezone_aware_datetime_uses_supplied_wall_clock_minutes():
    when = at(22, 30, 59).replace(tzinfo=timezone(timedelta(hours=7)))
    assert quiet_hours_active({"quiet_start_time": "22:30", "quiet_end_time": "06:15"}, when)


@pytest.mark.parametrize(("start", "end", "expected_minutes"), [("09:15", "17:45", 510), ("22:30", "06:15", 465)])
def test_full_day_minute_enumeration_matches_exact_daytime_and_overnight_duration(start, end, expected_minutes):
    config = {"quiet_start_time": start, "quiet_end_time": end}
    active_minutes = sum(quiet_hours_active(config, at(minute // 60, minute % 60, 59)) for minute in range(1440))
    assert active_minutes == expected_minutes

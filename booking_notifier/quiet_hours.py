"""Shared local-clock quiet scheduling, with a safe legacy fallback for old config."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

LEGACY_QUIET_START = "00:00"
LEGACY_QUIET_END = "08:00"
_TIME_PATTERN = re.compile(r"([0-9]{1,2}):([0-9]{2})")


def _time_minutes(value: str, label: str) -> int:
    error = f"Giờ {label} yên lặng không hợp lệ. Nhập HH:MM từ 00:00 đến 23:59."
    if not isinstance(value, str):
        raise ValueError(error)
    match = _TIME_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError(error)
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        raise ValueError(error)
    return hour * 60 + minute


def normalize_quiet_times(start: str, end: str) -> tuple[str, str]:
    """Validate an H:MM/HH:MM pair and return canonical, distinct 24-hour times."""
    start_minute, end_minute = _time_minutes(start, "bắt đầu"), _time_minutes(end, "kết thúc")
    if start_minute == end_minute:
        raise ValueError("Giờ bắt đầu và giờ kết thúc yên lặng phải khác nhau.")
    return (
        f"{start_minute // 60:02d}:{start_minute % 60:02d}",
        f"{end_minute // 60:02d}:{end_minute % 60:02d}",
    )


def quiet_hours_active(config: dict[str, Any], when: datetime | None = None) -> bool:
    """Start-inclusive/end-exclusive local minutes, including overnight ranges.

    A corrupt saved endpoint invalidates the whole pair: use the original
    00:00–08:00 range, never mix a custom endpoint with a repaired endpoint.
    """
    if not config.get("quiet_hours_enabled", True):
        return False
    try:
        start, end = normalize_quiet_times(
            config.get("quiet_start_time", LEGACY_QUIET_START),
            config.get("quiet_end_time", LEGACY_QUIET_END),
        )
    except ValueError:
        start, end = LEGACY_QUIET_START, LEGACY_QUIET_END
    start_minute = int(start[:2]) * 60 + int(start[3:])
    end_minute = int(end[:2]) * 60 + int(end[3:])
    current = when if when is not None else datetime.now()
    minute = current.hour * 60 + current.minute
    if start_minute < end_minute:
        return start_minute <= minute < end_minute
    return minute >= start_minute or minute < end_minute

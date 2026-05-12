"""Pure scheduling logic.

Splitting this away from ``calendar_service`` lets us unit-test slot finding
without ever touching the Google API.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from models.schemas import FreeSlot


def _normalise_busy(
    busy: Iterable[tuple[datetime, datetime]],
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[datetime, datetime]]:
    """Clip busy intervals to the window and merge overlapping ones."""
    clipped: list[tuple[datetime, datetime]] = []
    for s, e in busy:
        if e <= window_start or s >= window_end:
            continue
        clipped.append((max(s, window_start), min(e, window_end)))
    clipped.sort(key=lambda x: x[0])

    merged: list[tuple[datetime, datetime]] = []
    for s, e in clipped:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def working_windows(
    now: datetime,
    days_ahead: int,
    earliest_hour: int,
    latest_hour: int,
    tz: ZoneInfo,
) -> list[tuple[datetime, datetime]]:
    """Return [start, end] working windows for the next ``days_ahead`` days.

    Today's window starts at ``max(now, earliest_hour)`` so we never propose a
    slot in the past.
    """
    windows: list[tuple[datetime, datetime]] = []
    today = now.astimezone(tz).date()
    for offset in range(days_ahead):
        day = today + timedelta(days=offset)
        start = datetime.combine(day, time(earliest_hour, 0), tzinfo=tz)
        end = datetime.combine(day, time(0, 0) if latest_hour == 24 else time(latest_hour, 0), tzinfo=tz)
        if latest_hour == 24:
            end = end + timedelta(days=1)
        if offset == 0 and now.astimezone(tz) > start:
            # Round "now" up to the next 15 min boundary so suggestions look tidy.
            n = now.astimezone(tz)
            minute = (n.minute // 15 + 1) * 15
            extra_hours, minute = divmod(minute, 60)
            start = n.replace(minute=minute, second=0, microsecond=0) + timedelta(
                hours=extra_hours
            )
        if start < end:
            windows.append((start, end))
    return windows


def find_free_slots(
    busy: Iterable[tuple[datetime, datetime]],
    duration_minutes: int,
    now: datetime,
    days_ahead: int = 3,
    earliest_hour: int = 9,
    latest_hour: int = 18,
    tz_name: str = "UTC",
    max_results: int = 5,
) -> list[FreeSlot]:
    """Return up to ``max_results`` candidate slots of ``duration_minutes``.

    The algorithm is intentionally simple: for each working window in the next
    ``days_ahead`` days we subtract busy intervals and keep the first opening
    long enough to fit the requested duration. We then try to spread choices
    across days (one per day before doubling up).
    """
    tz = ZoneInfo(tz_name)
    duration = timedelta(minutes=duration_minutes)
    windows = working_windows(now, days_ahead, earliest_hour, latest_hour, tz)

    first_per_day: list[FreeSlot] = []
    extra: list[FreeSlot] = []

    for w_start, w_end in windows:
        merged = _normalise_busy(busy, w_start, w_end)
        cursor = w_start
        day_slots: list[FreeSlot] = []
        for b_start, b_end in merged:
            if b_start - cursor >= duration:
                slot_end = b_start
                # Emit non-overlapping fixed-duration slots within the gap.
                t = cursor
                while slot_end - t >= duration:
                    day_slots.append(FreeSlot(start=t, end=t + duration))
                    t = t + duration
            cursor = max(cursor, b_end)
        if w_end - cursor >= duration:
            t = cursor
            while w_end - t >= duration:
                day_slots.append(FreeSlot(start=t, end=t + duration))
                t = t + duration

        if day_slots:
            first_per_day.append(day_slots[0])
            extra.extend(day_slots[1:])

    combined = first_per_day + extra
    return combined[:max_results]

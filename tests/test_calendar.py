"""Unit tests for the slot-finding logic.

These don't hit the network - we test the pure scheduler module by feeding it
synthetic busy intervals and asserting on the returned ``FreeSlot``s.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from services.scheduler_service import (
    _normalise_busy,
    find_free_slots,
    working_windows,
)

UTC = ZoneInfo("UTC")


def _dt(y: int, m: int, d: int, h: int, mi: int = 0, tz: ZoneInfo = UTC) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=tz)


def test_working_windows_skip_past_hours_today():
    now = _dt(2026, 5, 11, 11, 23)  # Monday 11:23 UTC
    windows = working_windows(
        now=now, days_ahead=2, earliest_hour=9, latest_hour=18, tz=UTC
    )
    assert len(windows) == 2
    # Today's window starts after 11:23, rounded up to a 15-min boundary.
    assert windows[0][0] >= now
    assert windows[0][0].minute % 15 == 0
    # Tomorrow's window is the full 9:00-18:00.
    assert windows[1][0] == _dt(2026, 5, 12, 9)
    assert windows[1][1] == _dt(2026, 5, 12, 18)


def test_normalise_busy_clips_and_merges():
    window_start = _dt(2026, 5, 11, 9)
    window_end = _dt(2026, 5, 11, 18)
    busy = [
        (_dt(2026, 5, 11, 8), _dt(2026, 5, 11, 10)),  # clipped to 9-10
        (_dt(2026, 5, 11, 9, 30), _dt(2026, 5, 11, 10, 30)),  # merged with prev
        (_dt(2026, 5, 11, 14), _dt(2026, 5, 11, 15)),
        (_dt(2026, 5, 11, 19), _dt(2026, 5, 11, 20)),  # outside window
    ]
    merged = _normalise_busy(busy, window_start, window_end)
    assert merged == [
        (_dt(2026, 5, 11, 9), _dt(2026, 5, 11, 10, 30)),
        (_dt(2026, 5, 11, 14), _dt(2026, 5, 11, 15)),
    ]


def test_find_free_slots_basic():
    """30-min slots fit the gaps between two busy blocks."""
    now = _dt(2026, 5, 11, 9)
    busy = [
        (_dt(2026, 5, 11, 10), _dt(2026, 5, 11, 11)),
        (_dt(2026, 5, 11, 13), _dt(2026, 5, 11, 14)),
    ]
    slots = find_free_slots(
        busy=busy,
        duration_minutes=30,
        now=now,
        days_ahead=1,
        earliest_hour=9,
        latest_hour=18,
        tz_name="UTC",
        max_results=10,
    )
    assert slots, "should return at least one slot"
    # First slot should start at 9:00.
    assert slots[0].start == _dt(2026, 5, 11, 9)
    assert slots[0].end == _dt(2026, 5, 11, 9, 30)
    # No slot may overlap with a busy interval.
    for s in slots:
        for b_start, b_end in busy:
            assert not (s.start < b_end and s.end > b_start), (
                f"{s} overlaps busy ({b_start}, {b_end})"
            )


def test_find_free_slots_no_availability():
    """Calendar fully booked → no slots. (Failure mode #1.)"""
    now = _dt(2026, 5, 11, 9)
    busy = [(_dt(2026, 5, 11, 9), _dt(2026, 5, 11, 18))]
    slots = find_free_slots(
        busy=busy,
        duration_minutes=30,
        now=now,
        days_ahead=1,
        earliest_hour=9,
        latest_hour=18,
        tz_name="UTC",
    )
    assert slots == []


def test_find_free_slots_request_outside_working_hours():
    """User wants a slot 'late at night' but working hours are 09-18.

    Even with an empty calendar, no slot should be returned because the
    request falls outside the working window. (Failure mode #2.)
    """
    now = _dt(2026, 5, 11, 9)
    slots = find_free_slots(
        busy=[],  # entirely free calendar
        duration_minutes=30,
        now=now,
        days_ahead=1,
        earliest_hour=23,  # only 23:00-24:00 considered "working"
        latest_hour=24,
        tz_name="UTC",
        max_results=10,
    )
    assert slots, "the 23:00-24:00 window should still produce slots"
    # All slots must be at or after 23:00.
    assert all(s.start.hour >= 23 for s in slots)

    # And conversely: if the user asks for a window that doesn't exist
    # (earliest >= latest), no slots are produced.
    no_slots = find_free_slots(
        busy=[],
        duration_minutes=30,
        now=now,
        days_ahead=1,
        earliest_hour=22,
        latest_hour=22,
        tz_name="UTC",
    )
    assert no_slots == []


def test_find_free_slots_spreads_across_days_first():
    """When multiple days have openings, return one per day before doubling up."""
    now = _dt(2026, 5, 11, 9)
    busy: list[tuple[datetime, datetime]] = []
    slots = find_free_slots(
        busy=busy,
        duration_minutes=60,
        now=now,
        days_ahead=3,
        earliest_hour=9,
        latest_hour=18,
        tz_name="UTC",
        max_results=3,
    )
    assert len(slots) == 3
    days = {s.start.date() for s in slots}
    assert len(days) == 3, "expected one slot per day for the first 3 results"


def test_find_free_slots_duration_filter():
    """A 15-min gap should not yield a 30-min slot."""
    now = _dt(2026, 5, 11, 9)
    busy = [
        (_dt(2026, 5, 11, 9, 15), _dt(2026, 5, 11, 17, 0)),
    ]
    slots = find_free_slots(
        busy=busy,
        duration_minutes=30,
        now=now,
        days_ahead=1,
        earliest_hour=9,
        latest_hour=18,
        tz_name="UTC",
    )
    # 17:00-18:00 is the only big-enough gap; 9:00-9:15 is too short.
    assert slots
    assert all(s.start >= _dt(2026, 5, 11, 17, 0) for s in slots)
    assert (slots[0].end - slots[0].start) == timedelta(minutes=30)


def test_find_free_slots_timezone():
    """Working hours are interpreted in the requested timezone."""
    london = ZoneInfo("Europe/London")
    now = datetime(2026, 5, 11, 6, 0, tzinfo=UTC)  # 07:00 London (BST)
    slots = find_free_slots(
        busy=[],
        duration_minutes=30,
        now=now,
        days_ahead=1,
        earliest_hour=9,
        latest_hour=18,
        tz_name="Europe/London",
        max_results=1,
    )
    assert slots
    # First slot should start at 09:00 London local time.
    assert slots[0].start.astimezone(london).hour == 9
    assert slots[0].start.astimezone(london).minute == 0


def test_freeslot_label_includes_day_date_and_timezone():
    """Labels presented to the user must always include day, date, and tz."""
    tokyo = ZoneInfo("Asia/Tokyo")
    now = datetime(2026, 5, 11, 0, 0, tzinfo=UTC)  # 09:00 Tokyo
    slots = find_free_slots(
        busy=[],
        duration_minutes=30,
        now=now,
        days_ahead=1,
        earliest_hour=13,
        latest_hour=14,
        tz_name="Asia/Tokyo",
        max_results=1,
    )
    assert slots
    label = slots[0].label()
    # e.g. "Mon 11 May, 13:00 to 13:30 JST"
    assert "13:00 to 13:30" in label
    assert "JST" in label
    assert "May" in label
    # Day-of-week prefix present (one of the three-letter abbreviations).
    assert any(d in label for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))

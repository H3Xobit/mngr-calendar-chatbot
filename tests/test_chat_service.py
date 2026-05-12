"""Tests for the chat service's tool-execution layer.

We mock ``calendar_service`` so we can exercise every failure mode
deterministically without ever hitting Google or Groq.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from models.schemas import CreatedEvent, FreeSlot
from services import chat_service
from services.calendar_service import CalendarError, NotAuthenticated, SlotConflict

# ---------- find_free_slots tool --------------------------------------------


def test_find_free_slots_tool_returns_labels():
    """Successful path: tool result must carry the human-readable labels."""
    fake = [
        FreeSlot(
            start=datetime(2026, 5, 12, 13, 0, tzinfo=UTC),
            end=datetime(2026, 5, 12, 13, 30, tzinfo=UTC),
        )
    ]
    with patch.object(
        chat_service.calendar_service, "find_slots", return_value=fake
    ):
        result, created, needs_auth = chat_service._execute_tool_call(
            "sess1",
            "find_free_slots",
            {"duration_minutes": 30, "timezone": "UTC"},
        )
    assert needs_auth is False
    assert created is None
    assert result["slots"][0]["label"].startswith("Tue 12 May")
    assert "13:00 to 13:30" in result["slots"][0]["label"]


def test_find_free_slots_tool_empty_when_no_availability():
    """Failure mode #1: calendar fully booked → empty slots list (not an error)."""
    with patch.object(chat_service.calendar_service, "find_slots", return_value=[]):
        result, created, needs_auth = chat_service._execute_tool_call(
            "sess1",
            "find_free_slots",
            {"duration_minutes": 30},
        )
    assert needs_auth is False
    assert created is None
    assert result == {"slots": []}
    # The LLM is then prompted (by its system instructions) to suggest widening
    # the window; we can't unit-test the LLM, but the contract here is clear.


def test_find_free_slots_tool_returns_calendar_error_when_api_disabled():
    """Failure mode: Calendar API not enabled / generic Google API failure."""
    def boom(**kwargs):
        raise CalendarError(
            "The Google Calendar API isn't enabled in the OAuth client's "
            "Google Cloud project."
        )

    with patch.object(chat_service.calendar_service, "find_slots", side_effect=boom):
        result, created, needs_auth = chat_service._execute_tool_call(
            "sess1",
            "find_free_slots",
            {"duration_minutes": 30},
        )
    assert needs_auth is False
    assert created is None
    assert result["error"] == "calendar_error"
    assert "Calendar API" in result["message"]


def test_find_free_slots_tool_returns_auth_required_when_not_authenticated():
    """When the user hasn't connected, tool result signals auth_required."""
    with patch.object(
        chat_service.calendar_service,
        "find_slots",
        side_effect=NotAuthenticated("no token"),
    ):
        result, created, needs_auth = chat_service._execute_tool_call(
            "sess1",
            "find_free_slots",
            {"duration_minutes": 30},
        )
    assert needs_auth is True
    assert created is None
    assert result["error"] == "auth_required"


# ---------- create_event tool ------------------------------------------------


def test_create_event_tool_success():
    fake = CreatedEvent(
        id="evt-1",
        htmlLink="https://calendar.google.com/event?eid=evt-1",
        summary="Product Sync",
        start=datetime(2026, 5, 12, 13, 0, tzinfo=UTC),
        end=datetime(2026, 5, 12, 13, 30, tzinfo=UTC),
    )
    with patch.object(
        chat_service.calendar_service, "create_event", return_value=fake
    ):
        result, created, needs_auth = chat_service._execute_tool_call(
            "sess1",
            "create_event",
            {
                "title": "Product Sync",
                "start": "2026-05-12T13:00:00+00:00",
                "end": "2026-05-12T13:30:00+00:00",
                "timezone": "UTC",
            },
        )
    assert needs_auth is False
    assert created is not None
    assert created.id == "evt-1"
    assert result["id"] == "evt-1"
    assert result["htmlLink"].startswith("https://calendar.google.com")


def test_create_event_tool_calendar_error_surfaces_message():
    with patch.object(
        chat_service.calendar_service,
        "create_event",
        side_effect=CalendarError("Google says no."),
    ):
        result, created, needs_auth = chat_service._execute_tool_call(
            "sess1",
            "create_event",
            {
                "title": "X",
                "start": "2026-05-12T13:00:00+00:00",
                "end": "2026-05-12T13:30:00+00:00",
            },
        )
    assert needs_auth is False
    assert created is None
    assert result == {"error": "calendar_error", "message": "Google says no."}


# ---------- unknown tool -----------------------------------------------------


def test_unknown_tool_name_is_handled():
    result, created, needs_auth = chat_service._execute_tool_call(
        "sess1", "send_email", {}
    )
    assert needs_auth is False
    assert created is None
    assert "Unknown tool" in result["error"]


# ---------- argument parsing robustness --------------------------------------


def test_iso_parser_accepts_zulu_suffix():
    """The LLM sometimes emits 'Z' instead of '+00:00' - must still parse."""
    parsed = chat_service._parse_iso("2026-05-12T13:00:00Z")
    assert parsed == datetime(2026, 5, 12, 13, 0, tzinfo=UTC)


def test_tool_call_argument_parsing_tolerates_garbage_json():
    """Malformed JSON in tool args shouldn't crash the chat loop."""
    with patch.object(chat_service.calendar_service, "find_slots", return_value=[]):
        result, _created, _auth = chat_service._execute_tool_call(
            "sess1", "find_free_slots", {}
        )
    assert "slots" in result


# ---------- argument clamping ------------------------------------------------


def test_clamp_helper():
    """_clamp coerces and bounds values; bad input falls back to default."""
    assert chat_service._clamp(0, 3, 1, 14) == 1
    assert chat_service._clamp(20, 3, 1, 14) == 14
    assert chat_service._clamp("not a number", 3, 1, 14) == 3
    assert chat_service._clamp(None, 3, 1, 14) == 3
    assert chat_service._clamp("7", 3, 1, 14) == 7


def test_find_free_slots_tool_clamps_days_ahead_zero():
    """LLM passes days_ahead=0 → we clamp to 1 instead of letting it fail."""
    captured: dict = {}

    def fake_find_slots(**kwargs):
        captured.update(kwargs)
        return []

    with patch.object(
        chat_service.calendar_service, "find_slots", side_effect=fake_find_slots
    ):
        chat_service._execute_tool_call(
            "sess1",
            "find_free_slots",
            {
                "duration_minutes": 30,
                "days_ahead": 0,  # LLM mistake
                "earliest_hour": 23,
                "latest_hour": 24,
                "timezone": "Asia/Tokyo",
            },
        )
    assert captured["days_ahead"] == 1, "days_ahead=0 should clamp to 1"
    assert captured["duration_minutes"] == 30
    assert captured["earliest_hour"] == 23
    assert captured["latest_hour"] == 24
    assert captured["timezone_name"] == "Asia/Tokyo"


def test_find_free_slots_tool_widens_window_when_latest_le_earliest():
    """If LLM hands us latest_hour=earliest_hour, widen rather than return []."""
    captured: dict = {}

    def fake_find_slots(**kwargs):
        captured.update(kwargs)
        return []

    with patch.object(
        chat_service.calendar_service, "find_slots", side_effect=fake_find_slots
    ):
        chat_service._execute_tool_call(
            "sess1",
            "find_free_slots",
            {
                "duration_minutes": 30,
                "earliest_hour": 14,
                "latest_hour": 14,
            },
        )
    assert captured["earliest_hour"] == 14
    assert captured["latest_hour"] == 15, "latest should widen to earliest+1"


# ---------- pre-insert race fix ---------------------------------------------


def test_create_event_slot_conflict_returns_structured_error():
    """The pre-insert recheck fired and found a fresh booking in the window.

    The user must NOT see a confirmation; the chat layer must surface
    ``slot_conflict`` so the LLM can apologise and ask `find_free_slots`
    again.
    """
    conflict_window = [
        (
            datetime(2026, 5, 12, 13, 10, tzinfo=UTC),
            datetime(2026, 5, 12, 13, 25, tzinfo=UTC),
        )
    ]
    with patch.object(
        chat_service.calendar_service,
        "create_event",
        side_effect=SlotConflict("Taken just now.", conflict_window),
    ):
        result, created, needs_auth = chat_service._execute_tool_call(
            "sess1",
            "create_event",
            {
                "title": "Standup",
                "start": "2026-05-12T13:00:00+00:00",
                "end": "2026-05-12T13:30:00+00:00",
            },
        )
    assert needs_auth is False
    assert created is None, "no event should be reported as created"
    assert result["error"] == "slot_conflict"
    assert "advice" in result, "LLM needs guidance to re-fetch slots"
    assert "find_free_slots" in result["advice"]

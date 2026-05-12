"""Tests for ``calendar_service.create_event`` and its pre-insert race fix.

We mock the Google client at the seam (``_client_for``) and ``list_busy_intervals``
so these tests are fully offline and deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from services import calendar_service
from services.calendar_service import CalendarError, SlotConflict


@pytest.fixture
def fake_google_client():
    """A MagicMock that pretends to be the Google Calendar v3 client."""
    client = MagicMock()
    # events().insert(...).execute() chain
    client.events.return_value.insert.return_value.execute.return_value = {
        "id": "evt-123",
        "summary": "Created Event",
        "htmlLink": "https://calendar.google.com/event?id=evt-123",
    }
    return client


def test_create_event_blocks_when_freebusy_recheck_finds_overlap(fake_google_client):
    """Race fix: if a conflict appears between find_free_slots and insert,
    we must raise SlotConflict and NEVER call events.insert."""
    start = datetime(2026, 5, 12, 13, 0, tzinfo=UTC)
    end = datetime(2026, 5, 12, 13, 30, tzinfo=UTC)

    overlapping_busy = [
        (
            datetime(2026, 5, 12, 13, 10, tzinfo=UTC),
            datetime(2026, 5, 12, 13, 20, tzinfo=UTC),
        )
    ]

    with (
        patch.object(calendar_service, "_client_for", return_value=fake_google_client),
        patch.object(
            calendar_service, "list_busy_intervals", return_value=overlapping_busy
        ),
    ):
        with pytest.raises(SlotConflict) as exc_info:
            calendar_service.create_event(
                session_id="sess1",
                title="Should be blocked",
                start=start,
                end=end,
            )

    assert exc_info.value.conflicts == overlapping_busy
    # Critical: the booking call must NEVER have fired.
    fake_google_client.events.return_value.insert.assert_not_called()


def test_create_event_ignores_edge_touching_intervals(fake_google_client):
    """A busy interval whose end == proposed start is not a real conflict.

    Without this, booking the slot immediately after a meeting ends would
    falsely fail.
    """
    start = datetime(2026, 5, 12, 13, 0, tzinfo=UTC)
    end = datetime(2026, 5, 12, 13, 30, tzinfo=UTC)

    edge_touching = [
        (
            datetime(2026, 5, 12, 12, 30, tzinfo=UTC),
            datetime(2026, 5, 12, 13, 0, tzinfo=UTC),  # ends exactly at start
        ),
        (
            datetime(2026, 5, 12, 13, 30, tzinfo=UTC),  # starts exactly at end
            datetime(2026, 5, 12, 14, 0, tzinfo=UTC),
        ),
    ]

    with (
        patch.object(calendar_service, "_client_for", return_value=fake_google_client),
        patch.object(
            calendar_service, "list_busy_intervals", return_value=edge_touching
        ),
    ):
        created = calendar_service.create_event(
            session_id="sess1",
            title="Adjacent",
            start=start,
            end=end,
        )
    assert created.id == "evt-123"


def test_create_event_recheck_can_be_skipped_for_integration_tests(fake_google_client):
    """Callers who already verified freshness can skip the extra freebusy call."""
    start = datetime(2026, 5, 12, 13, 0, tzinfo=UTC)
    end = datetime(2026, 5, 12, 13, 30, tzinfo=UTC)

    # We DO NOT patch list_busy_intervals, because skip_conflict_recheck=True
    # should mean it is never called. If the implementation calls it anyway
    # the test would fail with a real-network attempt.
    with patch.object(calendar_service, "_client_for", return_value=fake_google_client):
        created = calendar_service.create_event(
            session_id="sess1",
            title="Trusted",
            start=start,
            end=end,
            skip_conflict_recheck=True,
        )

    assert created.id == "evt-123"


def test_create_event_succeeds_when_recheck_returns_no_overlap(fake_google_client):
    """Happy path: freebusy returns intervals that don't overlap; insert fires."""
    start = datetime(2026, 5, 12, 13, 0, tzinfo=UTC)
    end = datetime(2026, 5, 12, 13, 30, tzinfo=UTC)

    with (
        patch.object(calendar_service, "_client_for", return_value=fake_google_client),
        patch.object(calendar_service, "list_busy_intervals", return_value=[]),
    ):
        created = calendar_service.create_event(
            session_id="sess1",
            title="Clean booking",
            start=start,
            end=end,
        )
    assert created.id == "evt-123"
    assert created.summary == "Created Event"


def test_friendly_http_error_translates_access_not_configured():
    """The most common production tripping-stone, surfaced as actionable text."""

    class FakeResp:
        status = 403

    class FakeHttpError(Exception):
        def __init__(self):
            self.resp = FakeResp()
            self.error_details = [{"reason": "accessNotConfigured"}]

        def __str__(self):
            return "Google Calendar API has not been used in project ..."

    msg = calendar_service._friendly_http_error(FakeHttpError())
    assert "enable" in msg.lower()
    assert "console.cloud.google.com" in msg


def test_friendly_http_error_translates_401_token_invalid():
    class FakeResp:
        status = 401

    class FakeHttpError(Exception):
        def __init__(self):
            self.resp = FakeResp()
            self.error_details = []
            self.reason = "Unauthorized"

    msg = calendar_service._friendly_http_error(FakeHttpError())
    assert "reconnect" in msg.lower() or "no longer valid" in msg.lower()


def test_friendly_http_error_falls_through_for_unknown_codes():
    class FakeResp:
        status = 500

    class FakeHttpError(Exception):
        def __init__(self):
            self.resp = FakeResp()
            self.error_details = []
            self.reason = "Internal"

    msg = calendar_service._friendly_http_error(FakeHttpError())
    assert "Google API error" in msg


def test_create_event_raises_calendar_error_when_recheck_itself_fails(fake_google_client):
    """If we can't verify the slot, we propagate the underlying error rather
    than blindly booking. Booking on guesswork is worse than failing loudly."""
    start = datetime(2026, 5, 12, 13, 0, tzinfo=UTC)
    end = datetime(2026, 5, 12, 13, 30, tzinfo=UTC)

    with (
        patch.object(calendar_service, "_client_for", return_value=fake_google_client),
        patch.object(
            calendar_service,
            "list_busy_intervals",
            side_effect=CalendarError("freebusy is down"),
        ),
    ):
        with pytest.raises(CalendarError):
            calendar_service.create_event(
                session_id="sess1",
                title="Should not book",
                start=start,
                end=end,
            )

    fake_google_client.events.return_value.insert.assert_not_called()

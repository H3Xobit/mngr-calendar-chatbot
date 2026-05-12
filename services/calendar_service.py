"""Google Calendar read/write wrapper.

This module is the only place that talks to the Google Calendar HTTP API.
``scheduler_service`` does the pure slot math so it can be unit tested.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from auth.google_auth import load_credentials
from models.schemas import CreatedEvent, FreeSlot
from services.scheduler_service import find_free_slots
from utils.logger import get_logger

log = get_logger(__name__)


class CalendarError(RuntimeError):
    """Raised when Google Calendar is unreachable or refuses a request."""


class NotAuthenticated(CalendarError):
    """No (valid) credentials are stored for this session."""


class SlotConflict(CalendarError):
    """The chosen slot is no longer free.

    Raised by ``create_event`` when the pre-insert freebusy recheck finds
    a conflicting event has appeared between the original ``find_free_slots``
    call and the booking attempt. This closes the well-known race in any
    calendar app that books a slot the user picked seconds earlier.
    """

    def __init__(self, message: str, conflicts: list[tuple[datetime, datetime]]):
        super().__init__(message)
        self.conflicts = conflicts


def _friendly_http_error(exc: HttpError) -> str:
    """Translate common Google API errors into actionable messages."""
    reason = ""
    try:
        details = exc.error_details if hasattr(exc, "error_details") else []
        if details and isinstance(details, list):
            reason = details[0].get("reason", "")
    except Exception:
        pass

    status = exc.resp.status if hasattr(exc, "resp") else None

    if reason == "accessNotConfigured" or "has not been used in project" in str(exc):
        return (
            "The Google Calendar API isn't enabled in the OAuth client's "
            "Google Cloud project. Enable it at "
            "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com "
            "then retry."
        )
    if status == 403 and reason in {"insufficientPermissions", "forbidden"}:
        return (
            "Google rejected the request as forbidden. Check that the OAuth "
            "consent screen grants the calendar.readonly and calendar.events "
            "scopes, then reconnect."
        )
    if status == 401:
        return (
            "Google says the access token is no longer valid. Click 'Reconnect "
            "Google Calendar' to refresh it."
        )
    return f"Google API error: {getattr(exc, 'reason', str(exc))}"


# ---------- Client helpers ----------------------------------------------------


def _client_for(session_id: str):
    creds = load_credentials(session_id)
    if not creds:
        log.warning(
            "_client_for(session=%s): load_credentials returned None "
            "(no token file or could not parse).",
            session_id[:8],
        )
        raise NotAuthenticated("No valid Google credentials for this session.")
    if not creds.valid:
        log.warning(
            "_client_for(session=%s): credentials present but not valid "
            "(expired=%s, has_refresh=%s).",
            session_id[:8],
            getattr(creds, "expired", "?"),
            bool(getattr(creds, "refresh_token", None)),
        )
        raise NotAuthenticated("No valid Google credentials for this session.")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def has_valid_credentials(session_id: str) -> bool:
    """Cheap auth check that doesn't hit the network."""
    creds = load_credentials(session_id)
    return bool(creds and creds.valid)


def get_user_email(session_id: str) -> str | None:
    try:
        svc = _client_for(session_id)
        cal = svc.calendarList().get(calendarId="primary").execute()
        return cal.get("id")
    except NotAuthenticated:
        return None
    except HttpError as exc:
        log.warning("Failed to fetch primary calendar id: %s", exc)
        return None


# ---------- Reads -------------------------------------------------------------


def list_busy_intervals(
    session_id: str, time_min: datetime, time_max: datetime
) -> list[tuple[datetime, datetime]]:
    """Use the freebusy API to fetch busy intervals on the primary calendar."""
    svc = _client_for(session_id)
    body = {
        "timeMin": time_min.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "timeMax": time_max.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "items": [{"id": "primary"}],
    }
    try:
        resp = svc.freebusy().query(body=body).execute()
    except HttpError as exc:
        log.exception("Google freebusy query failed")
        raise CalendarError(_friendly_http_error(exc)) from exc

    cal = resp.get("calendars", {}).get("primary", {})
    if cal.get("errors"):
        raise CalendarError(f"Calendar errors: {cal['errors']}")

    out: list[tuple[datetime, datetime]] = []
    for b in cal.get("busy", []):
        out.append(
            (
                datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
            )
        )
    return out


def find_slots(
    session_id: str,
    duration_minutes: int,
    days_ahead: int = 3,
    earliest_hour: int = 9,
    latest_hour: int = 18,
    timezone_name: str = "UTC",
    max_results: int = 5,
    now: datetime | None = None,
) -> list[FreeSlot]:
    """High-level helper: pull busy intervals then run the slot finder."""
    now = now or datetime.now(UTC)
    horizon = now + timedelta(days=days_ahead + 1)
    busy = list_busy_intervals(session_id, now, horizon)
    return find_free_slots(
        busy=busy,
        duration_minutes=duration_minutes,
        now=now,
        days_ahead=days_ahead,
        earliest_hour=earliest_hour,
        latest_hour=latest_hour,
        tz_name=timezone_name,
        max_results=max_results,
    )


# ---------- Writes ------------------------------------------------------------


def _recheck_slot_is_free(
    session_id: str, start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """Query freebusy for exactly the proposed window and return any conflicts.

    This is the booking-race fix. Between the moment ``find_free_slots`` ran
    and the moment we call ``events.insert``, a conflicting event may have
    landed in the slot. If we skip this check we will happily double-book.
    The recheck is one extra freebusy call (~50 ms) and turns a silent
    double-booking into a structured ``SlotConflict`` error the LLM can
    apologise for and immediately propose an alternative.
    """
    try:
        return list_busy_intervals(session_id, start, end)
    except CalendarError:
        # If the recheck itself fails (network blip, 429), we propagate the
        # original error rather than guessing. Better to surface "we could
        # not verify the slot is still free" than to book blindly.
        raise


def create_event(
    session_id: str,
    title: str,
    start: datetime,
    end: datetime,
    description: str | None = None,
    attendees: Iterable[str] | None = None,
    timezone_name: str = "UTC",
    skip_conflict_recheck: bool = False,
) -> CreatedEvent:
    """Insert a calendar event after re-verifying the slot is still free.

    Set ``skip_conflict_recheck=True`` only if the caller has already
    verified the slot is free within the last few milliseconds (e.g. an
    integration test).
    """
    if not skip_conflict_recheck:
        conflicts = _recheck_slot_is_free(session_id, start, end)
        # Defensive: freebusy returns intervals that overlap the queried
        # window, but exact-edge touches (end == start) don't count as
        # conflicts.
        real_conflicts = [(s, e) for (s, e) in conflicts if not (e <= start or s >= end)]
        if real_conflicts:
            log.warning(
                "Pre-insert recheck found %d conflict(s) in [%s, %s] for session=%s",
                len(real_conflicts),
                start.isoformat(),
                end.isoformat(),
                session_id[:8],
            )
            raise SlotConflict(
                "That slot was taken between when I offered it and now. "
                "Pick another from the list or ask me to find new slots.",
                real_conflicts,
            )

    svc = _client_for(session_id)
    body: dict = {
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": timezone_name},
        "end": {"dateTime": end.isoformat(), "timeZone": timezone_name},
    }
    if description:
        body["description"] = description
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]

    try:
        created = svc.events().insert(calendarId="primary", body=body).execute()
    except HttpError as exc:
        log.exception("Google event creation failed")
        raise CalendarError(_friendly_http_error(exc)) from exc

    log.info("Created event %s for session=%s", created.get("id"), session_id[:8])
    return CreatedEvent(
        id=created["id"],
        htmlLink=created.get("htmlLink"),
        summary=created.get("summary", title),
        start=start,
        end=end,
    )

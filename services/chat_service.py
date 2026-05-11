"""Groq-powered conversation engine.

The LLM is given two tools (``find_free_slots`` and ``create_event``) it can
invoke to interact with the user's Google Calendar. The LLM owns the dialogue:
it greets the user, collects the meeting details in natural language, picks
when to query the calendar, presents slots, and confirms the booking.

The bridge between LLM intent and the calendar lives in
``_execute_tool_call`` below - that's the only place tool output is shaped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from groq import APIError, BadRequestError, Groq

from models.schemas import CreatedEvent, FreeSlot
from services import calendar_service
from utils.config import get_settings
from utils.logger import get_logger

log = get_logger(__name__)


SYSTEM_PROMPT = """You are MNGR's calendar scheduling assistant.

MNGR (mngr.club) is an operating system for networked work for independent
creative professionals - coordinating projects, files, tasks, calendars,
deals, contacts and finance on a single permission layer. You help users
schedule meetings on their Google Calendar through a short, focused
conversation.

How to behave:
- Be concise, warm, and professional. Two short sentences per turn is ideal.
- Greet the user on the first message and ask what they'd like to schedule.
- Collect, one missing piece at a time: meeting title, duration (in minutes),
  preferred time window (days ahead, earliest hour, latest hour), and the
  user's timezone if they haven't mentioned one. Reasonable defaults: 30 min
  duration, look 3 days ahead, 09:00-18:00, timezone "UTC" unless the user
  states otherwise.
- When you have enough information, call the `find_free_slots` tool. The
  tool returns each slot with a pre-formatted `label` field (e.g.
  "Tue 12 May, 13:00 to 13:30 JST") - **present each slot to the user
  using that exact `label` string** in a numbered list. Do not invent your
  own time format and never omit the day, date, or timezone.
- After the user picks a slot, call the `create_event` tool with that slot's
  start/end and the meeting title. Pass through the user's timezone.
- After a successful booking, confirm with a one-line summary including the
  date, time and the event link.
- If `find_free_slots` returns no slots, suggest widening the window
  (more days ahead, broader hours) and offer to try again.
- If a tool returns an `auth_required` error, tell the user you need them to
  connect their Google Calendar first (the UI shows a "Connect Google
  Calendar" button). On the user's NEXT message, attempt the calendar tool
  again - the user may have completed authorization in the meantime. Do not
  refuse to retry based on memory of an earlier failure.
- If a tool returns any other error, apologise briefly and ask if they'd like
  to try again, then actually retry on the next message.
- Never invent calendar slots or pretend an event was created. Only trust
  tool results.

Always emit ISO-8601 datetimes with timezone offset when calling tools.
"""


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "find_free_slots",
            "description": (
                "Find available time slots on the user's primary Google Calendar "
                "in the near future."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "duration_minutes": {
                        "type": "integer",
                        "minimum": 5,
                        "maximum": 480,
                        "description": "Length of the meeting in minutes.",
                    },
                    "days_ahead": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 14,
                        "description": (
                            "Number of days to search, COUNTING TODAY. "
                            "Use 1 for 'today only', 2 for 'today and tomorrow', "
                            "3 for 'next three days' (default). Never pass 0."
                        ),
                    },
                    "earliest_hour": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 23,
                        "description": "Earliest hour of day to suggest (24h).",
                    },
                    "latest_hour": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 24,
                        "description": "Latest hour of day to end by (24h).",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone name, e.g. 'Europe/London' or 'UTC'.",
                    },
                },
                "required": ["duration_minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": "Create an event on the user's primary Google Calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Event title."},
                    "start": {
                        "type": "string",
                        "description": "Start in ISO-8601 with offset, e.g. 2026-05-12T14:00:00+01:00.",
                    },
                    "end": {
                        "type": "string",
                        "description": "End in ISO-8601 with offset.",
                    },
                    "description": {"type": "string"},
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Email addresses to invite (optional).",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone for the event (default UTC).",
                    },
                },
                "required": ["title", "start", "end"],
            },
        },
    },
]


# ---------- Conversation state -----------------------------------------------


@dataclass
class ChatTurnResult:
    reply: str
    conversation: list[dict[str, Any]]
    requires_auth: bool = False
    event_created: CreatedEvent | None = None


def new_conversation() -> list[dict[str, Any]]:
    """Return a fresh conversation seeded with the system prompt."""
    return [{"role": "system", "content": SYSTEM_PROMPT}]


# ---------- Tool execution ----------------------------------------------------


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    """Coerce ``value`` to an int and clamp it into [low, high]; fall back to default."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = default
    return max(low, min(high, v))


def _execute_tool_call(
    session_id: str, name: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], CreatedEvent | None, bool]:
    """Run a tool. Returns (json-serialisable result, event_created, auth_required)."""
    try:
        if name == "find_free_slots":
            duration = _clamp(arguments.get("duration_minutes"), 30, 5, 480)
            days_ahead = _clamp(arguments.get("days_ahead"), 3, 1, 14)
            earliest = _clamp(arguments.get("earliest_hour"), 9, 0, 23)
            latest = _clamp(arguments.get("latest_hour"), 18, 1, 24)
            # If the LLM gave latest <= earliest, widen to a sensible window
            # so we don't silently return zero slots from a bad argument.
            if latest <= earliest:
                latest = min(24, earliest + 1)
            slots: list[FreeSlot] = calendar_service.find_slots(
                session_id=session_id,
                duration_minutes=duration,
                days_ahead=days_ahead,
                earliest_hour=earliest,
                latest_hour=latest,
                timezone_name=arguments.get("timezone", "UTC"),
            )
            return (
                {
                    "slots": [
                        {
                            "start": s.start.isoformat(),
                            "end": s.end.isoformat(),
                            "label": s.label(),
                        }
                        for s in slots
                    ]
                },
                None,
                False,
            )

        if name == "create_event":
            created = calendar_service.create_event(
                session_id=session_id,
                title=arguments["title"],
                start=_parse_iso(arguments["start"]),
                end=_parse_iso(arguments["end"]),
                description=arguments.get("description"),
                attendees=arguments.get("attendees"),
                timezone_name=arguments.get("timezone", "UTC"),
            )
            return (
                {
                    "id": created.id,
                    "summary": created.summary,
                    "start": created.start.isoformat(),
                    "end": created.end.isoformat(),
                    "htmlLink": created.htmlLink,
                },
                created,
                False,
            )

        return ({"error": f"Unknown tool '{name}'"}, None, False)

    except calendar_service.NotAuthenticated as exc:
        log.info("Tool '%s' blocked: not authenticated", name)
        return ({"error": "auth_required", "message": str(exc)}, None, True)
    except calendar_service.CalendarError as exc:
        log.warning("Tool '%s' calendar error: %s", name, exc)
        return ({"error": "calendar_error", "message": str(exc)}, None, False)
    except Exception as exc:  # noqa: BLE001
        log.exception("Tool '%s' unexpected error", name)
        return ({"error": "internal_error", "message": str(exc)}, None, False)


# ---------- LLM driver --------------------------------------------------------


def _client() -> Groq:
    s = get_settings()
    if not s.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not set. See .env.example.")
    return Groq(api_key=s.groq_api_key)


def handle_user_message(
    session_id: str,
    conversation: list[dict[str, Any]],
    user_message: str,
    is_authenticated: bool = False,
    user_timezone: str | None = None,
    max_tool_iterations: int = 4,
) -> ChatTurnResult:
    """Drive one user turn through Groq, executing any tool calls it makes."""
    s = get_settings()
    client = _client()

    conversation = list(conversation) if conversation else new_conversation()
    today_hint = (
        f"(Today is {datetime.now(timezone.utc).strftime('%A %d %B %Y')} (UTC). "
        "Use this for relative dates the user gives.)"
    )
    tz_hint = (
        f"(User timezone: {user_timezone}. Pass this as the `timezone` "
        "argument when calling tools unless the user explicitly states a "
        "different timezone.)"
        if user_timezone
        else "(User timezone unknown; default to UTC.)"
    )
    auth_hint = (
        "(System: the user IS currently authenticated to Google Calendar - "
        "you may call calendar tools freely.)"
        if is_authenticated
        else "(System: the user is NOT yet authenticated to Google Calendar. "
        "Do not call calendar tools; ask them to click 'Connect Google Calendar'.)"
    )
    conversation.append(
        {
            "role": "user",
            "content": f"{user_message}\n\n{auth_hint}\n{tz_hint}\n{today_hint}",
        }
    )
    log.info(
        "chat turn: session=%s authenticated=%s convo_len=%d",
        session_id[:8],
        is_authenticated,
        len(conversation),
    )

    requires_auth = False
    event_created: CreatedEvent | None = None

    tool_validation_retries_left = 1

    for _ in range(max_tool_iterations):
        try:
            resp = client.chat.completions.create(
                model=s.groq_model,
                messages=conversation,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.3,
                max_tokens=800,
            )
        except BadRequestError as exc:
            # Groq validates tool-call arguments server-side against our JSON
            # schema. When the LLM gets a constraint wrong (e.g. days_ahead=0),
            # we inject a corrective hint and let it retry once.
            err_msg = str(exc)
            log.warning("Groq tool-use validation failed: %s", err_msg)
            if tool_validation_retries_left > 0 and "tool_use_failed" in err_msg:
                tool_validation_retries_left -= 1
                conversation.append(
                    {
                        "role": "system",
                        "content": (
                            "Your previous tool call had invalid arguments and "
                            "was rejected: " + err_msg + "\n"
                            "Read the tool schema carefully and try again. "
                            "Remember: days_ahead must be at least 1 (1 = today)."
                        ),
                    }
                )
                continue
            log.exception("Groq API error (non-recoverable)")
            reply = (
                "I tried to look that up but my tool call was malformed. "
                "Could you rephrase the request?"
            )
            conversation.append({"role": "assistant", "content": reply})
            return ChatTurnResult(reply=reply, conversation=conversation)
        except APIError as exc:
            log.exception("Groq API error")
            reply = (
                "I'm having trouble reaching my language backend right now. "
                "Please try again in a moment."
            )
            conversation.append({"role": "assistant", "content": reply})
            return ChatTurnResult(reply=reply, conversation=conversation)

        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if not tool_calls:
            reply = msg.content or "(no response)"
            log.info(
                "LLM responded with no tool calls (authenticated=%s). reply preview: %s",
                is_authenticated,
                reply[:120],
            )
            conversation.append({"role": "assistant", "content": reply})
            return ChatTurnResult(
                reply=reply,
                conversation=conversation,
                requires_auth=requires_auth,
                event_created=event_created,
            )

        log.info(
            "LLM called tools: %s",
            [tc.function.name for tc in tool_calls],
        )

        conversation.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result, created_evt, needs_auth = _execute_tool_call(
                session_id, tc.function.name, args
            )
            if needs_auth:
                requires_auth = True
            if created_evt:
                event_created = created_evt
            conversation.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.function.name,
                    "content": json.dumps(result),
                }
            )

    # Safety net: ran out of tool iterations.
    fallback = (
        "Sorry, I got stuck juggling calendar lookups. Could you rephrase what "
        "you'd like to schedule?"
    )
    conversation.append({"role": "assistant", "content": fallback})
    return ChatTurnResult(
        reply=fallback,
        conversation=conversation,
        requires_auth=requires_auth,
        event_created=event_created,
    )

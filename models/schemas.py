"""Pydantic request / response models used across the API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------- Chat ----------


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    timezone: str | None = Field(
        default=None,
        description="IANA timezone of the user's browser (e.g. 'Asia/Tokyo').",
    )


class ChatResponse(BaseModel):
    reply: str
    requires_auth: bool = False
    event_created: dict | None = None


# ---------- Calendar ----------


class FreeSlot(BaseModel):
    start: datetime
    end: datetime

    def label(self) -> str:
        """Human friendly label, e.g. 'Tue 12 May, 13:00 to 13:30 JST'.

        The timezone abbreviation comes from whatever tz the start datetime
        carries - find_free_slots emits slots already converted to the
        user's requested timezone.
        """
        date_part = self.start.strftime("%a %d %b")
        tz_abbr = (self.start.tzname() or "").strip()
        suffix = f" {tz_abbr}" if tz_abbr else ""
        if self.start.date() == self.end.date():
            return (
                f"{date_part}, {self.start.strftime('%H:%M')} to "
                f"{self.end.strftime('%H:%M')}{suffix}"
            )
        return (
            f"{date_part} {self.start.strftime('%H:%M')} to "
            f"{self.end.strftime('%a %d %b %H:%M')}{suffix}"
        )


class FindSlotsRequest(BaseModel):
    duration_minutes: int = Field(30, ge=5, le=480)
    days_ahead: int = Field(3, ge=1, le=14)
    earliest_hour: int = Field(9, ge=0, le=23)
    latest_hour: int = Field(18, ge=1, le=24)
    timezone: str = "UTC"


class CreateEventRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    start: datetime
    end: datetime
    description: str | None = None
    attendees: list[str] | None = None
    timezone: str = "UTC"


class CreatedEvent(BaseModel):
    id: str
    htmlLink: str | None = None
    summary: str
    start: datetime
    end: datetime


# ---------- Auth ----------


class AuthStatus(BaseModel):
    authenticated: bool
    email: str | None = None

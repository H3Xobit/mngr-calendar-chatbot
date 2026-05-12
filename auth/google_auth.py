"""Google OAuth2 flow for Calendar access.

Design notes
------------
* We use the **installed/web-app OAuth2 flow** with ``GOOGLE_CLIENT_ID`` /
  ``GOOGLE_CLIENT_SECRET`` taken from environment variables. No
  ``credentials.json`` checked into the repo.
* Tokens are stored **per session** in ``tokens/<session_id>.json`` so any
  Google account can connect - the MNGR reviewer's account, mine, multiple
  testers in parallel. The session id lives in a signed cookie, never in the
  page DOM.
* Refresh tokens are persisted; ``Credentials.refresh()`` is called
  transparently the next time we need a calendar client.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from utils.config import get_settings
from utils.logger import get_logger

log = get_logger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


# ---------- Token persistence -------------------------------------------------


def _token_path(session_id: str) -> Path:
    return get_settings().token_dir / f"{session_id}.json"


def save_credentials(session_id: str, creds: Credentials) -> None:
    path = _token_path(session_id)
    path.write_text(creds.to_json(), encoding="utf-8")
    log.info("Saved Google credentials for session=%s", session_id[:8])


def load_credentials(session_id: str) -> Credentials | None:
    path = _token_path(session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        creds = Credentials.from_authorized_user_info(data, SCOPES)
    except Exception as exc:
        log.warning("Could not load token for session=%s: %s", session_id[:8], exc)
        return None

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            save_credentials(session_id, creds)
        except Exception as exc:
            log.warning("Token refresh failed for session=%s: %s", session_id[:8], exc)
            return None
    return creds


def clear_credentials(session_id: str) -> None:
    path = _token_path(session_id)
    if path.exists():
        path.unlink()
        log.info("Cleared Google credentials for session=%s", session_id[:8])


# ---------- OAuth2 flow -------------------------------------------------------


def _client_config() -> dict[str, Any]:
    s = get_settings()
    if not s.google_client_id or not s.google_client_secret:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set. "
            "See .env.example for how to create OAuth credentials."
        )
    return {
        "web": {
            "client_id": s.google_client_id,
            "client_secret": s.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [s.google_redirect_uri],
        }
    }


def build_flow(state: str | None = None) -> Flow:
    s = get_settings()
    flow = Flow.from_client_config(
        _client_config(), scopes=SCOPES, state=state
    )
    flow.redirect_uri = s.google_redirect_uri
    return flow


def new_state() -> str:
    """One-time CSRF state for the OAuth handshake."""
    return uuid.uuid4().hex


def authorization_url(state: str) -> tuple[str, str]:
    """Return (authorization_url, redirect_uri_being_sent_to_google)."""
    flow = build_flow(state=state)
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return url, flow.redirect_uri


def exchange_code(state: str, authorization_response_url: str) -> Credentials:
    flow = build_flow(state=state)
    flow.fetch_token(authorization_response=authorization_response_url)
    return flow.credentials

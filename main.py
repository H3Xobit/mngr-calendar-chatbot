"""FastAPI entry point for the MNGR calendar scheduling chatbot."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from auth import google_auth
from models.schemas import AuthStatus, ChatRequest, ChatResponse
from services import calendar_service, chat_service
from utils.config import get_settings
from utils.logger import (
    bind_request_id,
    current_request_id,
    get_logger,
    new_request_id,
    unbind_request_id,
)

log = get_logger(__name__)
settings = get_settings()

app = FastAPI(
    title="MNGR Calendar Chatbot",
    version="0.1.0",
    description="Schedule meetings on your Google Calendar via natural language.",
)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Stamp every request with a short uuid for log correlation.

    Honours an inbound ``X-Request-ID`` header if provided (handy when this
    service sits behind a gateway that already issues them) and echoes the
    id back on the response. Internal logs use the same id via the
    `request_id` contextvar in ``utils.logger``.
    """

    async def dispatch(self, request, call_next):
        rid = request.headers.get("x-request-id") or new_request_id()
        token = bind_request_id(rid)
        start = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.info(
                "http %s %s -> %s in %sms",
                request.method,
                request.url.path,
                getattr(response, "status_code", "?"),
                duration_ms,
            )
            unbind_request_id(token)
        response.headers["x-request-id"] = rid
        return response


app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="mngr_session",
    same_site="lax",
    https_only=False,  # local dev; flip on in production
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Per-session conversation history (in-memory; fine for a single-instance app).
_CONVERSATIONS: dict[str, list[dict[str, Any]]] = {}


# ---------- Helpers -----------------------------------------------------------


def _session_id(request: Request) -> str:
    """Return a stable session id, creating one on first request."""
    sid = request.session.get("sid")
    if not sid:
        sid = uuid.uuid4().hex
        request.session["sid"] = sid
    return sid


# ---------- Pages -------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def index(request: Request) -> FileResponse:
    _session_id(request)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness probe. Cheap. Returns 200 if the process is responding."""
    return {"status": "ok"}


@app.get("/healthz/deep")
async def healthz_deep() -> JSONResponse:
    """Deep readiness probe: pings the two upstream services we depend on.

    Why this exists:
    - ``/healthz`` says "the process is up", which is useless for catching
      the most common production failure mode: a stale config in which the
      Groq API key was rotated, or Google Calendar API was disabled in the
      Cloud project after launch.
    - ``/healthz/deep`` actually performs a lightweight check against each
      dependency and returns a 200 only when both succeed. A 503 surfaces
      which dependency is unhappy so a Kubernetes readiness probe (or a
      monitoring dashboard) can stop sending traffic before users notice.

    Cost: ~150-300 ms total. Safe to call from a probe every 30s.
    """
    checks: dict[str, dict] = {}
    overall_ok = True
    # Groq: a /models GET is the cheapest way to verify the key is valid
    # AND the service is reachable.
    if not settings.groq_api_key:
        checks["groq"] = {"ok": False, "detail": "GROQ_API_KEY not configured"}
        overall_ok = False
    else:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                )
            ok = 200 <= resp.status_code < 300
            checks["groq"] = {
                "ok": ok,
                "status_code": resp.status_code,
                "detail": "reachable" if ok else f"http {resp.status_code}",
            }
            overall_ok = overall_ok and ok
        except (httpx.HTTPError, OSError) as exc:
            checks["groq"] = {"ok": False, "detail": f"unreachable: {exc.__class__.__name__}"}
            overall_ok = False

    # Google: just check the discovery doc is reachable. We do NOT exercise
    # any per-user OAuth (would need a session) - that's covered by
    # /auth/status. Here we only verify Google's API host responds.
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://www.googleapis.com/discovery/v1/apis/calendar/v3/rest"
            )
        ok = 200 <= resp.status_code < 300
        checks["google_calendar_api"] = {
            "ok": ok,
            "status_code": resp.status_code,
            "detail": "reachable" if ok else f"http {resp.status_code}",
        }
        overall_ok = overall_ok and ok
    except (httpx.HTTPError, OSError) as exc:
        checks["google_calendar_api"] = {
            "ok": False,
            "detail": f"unreachable: {exc.__class__.__name__}",
        }
        overall_ok = False

    body = {
        "status": "ok" if overall_ok else "degraded",
        "request_id": current_request_id(),
        "checks": checks,
    }
    return JSONResponse(body, status_code=200 if overall_ok else 503)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    # The page itself supplies an inline SVG favicon; this just keeps the
    # access log clean for clients that request /favicon.ico directly.
    return Response(status_code=204)


# ---------- Auth --------------------------------------------------------------


@app.get("/auth/status", response_model=AuthStatus)
async def auth_status(request: Request) -> AuthStatus:
    sid = _session_id(request)
    creds = google_auth.load_credentials(sid)
    authed = bool(creds and creds.valid)
    log.info("/auth/status: session=%s authenticated=%s", sid[:8], authed)
    if not authed:
        return AuthStatus(authenticated=False)
    email = calendar_service.get_user_email(sid)
    return AuthStatus(authenticated=True, email=email)


_MAX_INFLIGHT_STATES = 5


@app.get("/auth/google/login")
async def google_login(request: Request) -> RedirectResponse:
    sid = _session_id(request)
    try:
        state = google_auth.new_state()
        # Keep a short list of in-flight states so the user can click "Connect"
        # multiple times (or open the login URL in a second tab) without
        # invalidating a flow that was already in progress.
        inflight = request.session.get("oauth_states", [])
        if not isinstance(inflight, list):
            inflight = []
        inflight.append(state)
        request.session["oauth_states"] = inflight[-_MAX_INFLIGHT_STATES:]
        url, redirect_uri = google_auth.authorization_url(state)
    except RuntimeError as exc:
        log.error("OAuth not configured: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    log.info(
        "OAuth LOGIN  session=%s  state_stored=%s  inflight_count=%d  cookie_in=%s",
        sid[:8],
        state,
        len(request.session["oauth_states"]),
        "yes" if request.cookies.get("mngr_session") else "NO",
    )
    log.info("OAuth redirect_uri being sent to Google: %s", redirect_uri)
    return RedirectResponse(url)


@app.get("/auth/google/callback")
async def google_callback(request: Request) -> RedirectResponse:
    sid = _session_id(request)
    inflight = request.session.get("oauth_states") or []
    # Backwards compat: tolerate the old single-state key too.
    if "oauth_state" in request.session and request.session["oauth_state"] not in inflight:
        inflight = [*inflight, request.session["oauth_state"]]
    received_state = request.query_params.get("state")
    log.info(
        "OAuth CALLBACK session=%s  cookie_in=%s  inflight_states=%s  state_in_url=%s",
        sid[:8],
        "yes" if request.cookies.get("mngr_session") else "NO",
        inflight,
        received_state,
    )
    if not received_state or received_state not in inflight:
        log.warning(
            "OAuth state mismatch - likely causes: (1) session cookie wasn't sent "
            "back (cookie_in=NO above), or (2) you completed a stale Google "
            "consent URL whose state was rotated out of the inflight buffer."
        )
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")
    if "error" in request.query_params:
        log.warning("OAuth error: %s", request.query_params.get("error"))
        return RedirectResponse("/?auth=denied")

    try:
        creds = google_auth.exchange_code(received_state, str(request.url))
    except Exception as exc:
        log.exception("OAuth code exchange failed")
        raise HTTPException(status_code=400, detail=f"OAuth failed: {exc}") from exc

    google_auth.save_credentials(sid, creds)
    request.session.pop("oauth_states", None)
    request.session.pop("oauth_state", None)
    # Wipe any prior conversation so the LLM doesn't carry over a stale
    # "auth_required" memory from before the user connected. The frontend
    # already shows a "Google Calendar connected" banner on /?auth=ok.
    _CONVERSATIONS.pop(sid, None)
    log.info("OAuth complete for session=%s; conversation reset", sid[:8])
    return RedirectResponse("/?auth=ok")


@app.post("/auth/logout")
async def auth_logout(request: Request) -> dict[str, bool]:
    sid = _session_id(request)
    google_auth.clear_credentials(sid)
    _CONVERSATIONS.pop(sid, None)
    return {"ok": True}


# ---------- Chat --------------------------------------------------------------


@app.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    sid = _session_id(request)
    convo = _CONVERSATIONS.get(sid) or chat_service.new_conversation()
    is_auth = calendar_service.has_valid_credentials(sid)
    log.info(
        "/chat: session=%s authenticated=%s convo_len=%d msg=%r",
        sid[:8],
        is_auth,
        len(convo),
        body.message[:80],
    )

    try:
        result = chat_service.handle_user_message(
            session_id=sid,
            conversation=convo,
            user_message=body.message,
            is_authenticated=is_auth,
            user_timezone=body.timezone,
        )
    except RuntimeError as exc:
        log.error("Chat error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    _CONVERSATIONS[sid] = result.conversation
    return ChatResponse(
        reply=result.reply,
        requires_auth=result.requires_auth,
        event_created=(
            {
                "id": result.event_created.id,
                "summary": result.event_created.summary,
                "start": result.event_created.start.isoformat(),
                "end": result.event_created.end.isoformat(),
                "htmlLink": result.event_created.htmlLink,
            }
            if result.event_created
            else None
        ),
    )


@app.post("/chat/reset")
async def chat_reset(request: Request) -> dict[str, bool]:
    sid = _session_id(request)
    _CONVERSATIONS.pop(sid, None)
    return {"ok": True}


# ---------- Local dev entrypoint ----------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )

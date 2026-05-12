# MNGR Calendar Chatbot

A small, opinionated calendar-scheduling chatbot built for the MNGR technical
task. You chat with it in natural language ("Book a 30-minute design review
tomorrow afternoon"), it reads your Google Calendar to find real free slots,
and creates the event when you pick one.

> **Stack:** FastAPI · Groq `llama-3.3-70b-versatile` · Google Calendar API (OAuth2) · vanilla HTML/CSS/JS

[![CI](https://github.com/H3Xobit/mngr-calendar-chatbot/actions/workflows/ci.yml/badge.svg)](https://github.com/H3Xobit/mngr-calendar-chatbot/actions/workflows/ci.yml)

## Highlights at a glance

- **36 unit tests, two Python versions (3.11 & 3.12), green CI on every push.**
- **Pre-insert freebusy race check.** Closes the documented booking-race: if a
  conflict appears between `find_free_slots` and `events.insert`, the chat
  service surfaces a structured `slot_conflict` error and the LLM apologises +
  re-fetches availability instead of silently double-booking.
- **Deep `/healthz/deep` probe** that actually pings Groq AND the Google
  Calendar API host, so a Kubernetes probe (or the UI's status pill) knows
  *which* upstream is unhealthy.
- **Structured JSON logs** opt-in via `LOG_FORMAT=json` + a per-request
  correlation id stamped on every log line and echoed in the `X-Request-ID`
  response header.
- **UI polish:** suggested-prompt chips, a Retry button on failed messages,
  a live upstream-health pill, and request-id surfaced in the dev console for
  support traceability.
- **Defence in depth & friendly errors:** every Google API error code the app
  has seen in the wild is translated to an actionable user message
  (`accessNotConfigured`, `401`, `403/forbidden`, ...).

---

## 1. Overview and architecture

```
                                ┌────────────────────────────┐
                                │  static/index.html         │
                                │  (one-page chat UI)        │
                                └─────────────┬──────────────┘
                                              │ fetch() JSON
                                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         FastAPI (main.py)                            │
│  /chat      /auth/google/login   /auth/google/callback   /auth/...   │
└──┬───────────────┬─────────────────────────────┬─────────────────────┘
   │               │                             │
   ▼               ▼                             ▼
chat_service   calendar_service             auth/google_auth
  │              │                                │
  │              ├─► scheduler_service            │
  │              │   (pure slot-finding math)     │
  │              ▼                                ▼
  └──► Groq API   Google Calendar API     OAuth2 + token store
                                          (tokens/<session_id>.json)
```

### Component responsibilities

| Module                       | Job                                                                                       |
| ---------------------------- | ----------------------------------------------------------------------------------------- |
| `main.py`                    | FastAPI app: routing, session middleware, wiring services together.                       |
| `auth/google_auth.py`        | Builds the OAuth2 flow, exchanges codes, persists/refreshes per-session credentials.      |
| `services/calendar_service.py` | The only module that talks to the Google Calendar HTTP API.                              |
| `services/scheduler_service.py` | Pure slot-finding logic (no I/O, no Google client). Easy to unit test.                  |
| `services/chat_service.py`   | Drives Groq with tool calling: the LLM owns the dialogue, calls `find_free_slots` / `create_event`. |
| `models/schemas.py`          | Pydantic request/response schemas.                                                        |
| `utils/logger.py`            | Rotating file logger to `logs/app.log` + stdout.                                          |
| `utils/config.py`            | `pydantic-settings`-based env config loaded once at startup.                               |
| `static/index.html`          | Single-page chat UI, no framework, no build step.                                          |
| `tests/test_calendar.py`     | Unit tests for the slot finder (no network calls).                                        |

---

## 2. Why Groq over OpenAI

- **Latency.** Groq runs `llama-3.3-70b-versatile` at hundreds of tokens/sec
  on its LPU hardware. For a scheduling assistant the user is sitting and
  waiting; conversational latency under ~500 ms makes the UX feel like a
  fast colleague rather than a chatbot.
- **Cost.** Groq's per-token pricing for the 70B Llama is materially cheaper
  than GPT-4-class models. For a B2B product like MNGR, the cost of the
  calendar assistant should be invisible against the seat price.
- **Open-weights model + portable API.** Groq exposes the OpenAI-compatible
  chat completions API. If Groq ever becomes a problem, swapping the SDK to
  Together / Fireworks / a self-hosted vLLM is a one-line change. No
  proprietary lock-in.
- **Reliability for tool-calling.** Llama 3.3 70B reliably emits well-formed
  tool calls, which is what we rely on (`find_free_slots`, `create_event`).

---

## 3. Why FastAPI

- **Async-first**, lightweight, type-driven - matches MNGR's "one permission
  layer, lots of integrations" philosophy where the backend is mostly I/O.
- **Pydantic everywhere.** Request/response schemas are validated and
  self-documenting (`/docs` ships for free).
- **Tiny surface area.** Single `main.py`, easy to read in five minutes,
  easy to embed inside a larger Python service later.
- **Starlette session middleware** gives us signed-cookie sessions out of
  the box, which is enough state for this single-instance demo.

---

## 4. OAuth2 design decision

The brief explicitly says: *any Google account should connect*. The
implications:

1. **No personal credentials shipped.** OAuth client ID and secret come
   from environment variables, not `credentials.json` committed to the repo.
2. **Per-session token storage.** Tokens are saved as
   `tokens/<session_id>.json`, keyed by a signed cookie. Multiple testers
   (Karan, me, anyone) can each connect their own calendar in parallel from
   different browsers without colliding.
3. **Refresh tokens are persisted** and refreshed transparently on the
   server, so users don't have to re-auth every hour.
4. **CSRF state** is generated per login attempt and verified on callback -
   no cross-site OAuth replays.
5. **Tokens never reach the browser.** The frontend only sees a session
   cookie. All Google API calls happen server-side.
6. **Scope minimalism.** Only `calendar.readonly` + `calendar.events` + an
   `openid email` to display "Connected as you@..." - nothing else.

---

## 5. Failure modes

| Failure                                    | What happens                                                                                                                                                  |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No slots in the requested window           | LLM tells the user and offers to widen the window (more days, broader hours). No "I'll get back to you" silence.                                              |
| **Slot is taken between offer and booking (race)** | `create_event` does a freebusy recheck on exactly the proposed window before calling `events.insert`. If a conflict landed in that gap, `SlotConflict` is raised, the LLM apologises and immediately re-runs `find_free_slots`. Booking never happens silently. |
| Google API error / network blip            | `CalendarError` raised in `calendar_service`, caught in `chat_service`, returned to the LLM as `{"error":"calendar_error","message":...}`. LLM apologises and asks if they want to retry. Full traceback logged to `logs/app.log`. The most common 403 / 401 / `accessNotConfigured` cases are translated to actionable text by `_friendly_http_error`. |
| User input is ambiguous                    | The system prompt instructs the LLM to ask one clarifying question at a time rather than guessing.                                                              |
| Access / refresh token expired             | `load_credentials` runs `Credentials.refresh()` automatically if a refresh token exists.                                                                       |
| Refresh fails / scope changed              | Tool returns `{"error":"auth_required"}`. The HTTP response sets `requires_auth: true`. The UI shows "Connect Google Calendar" again.                          |
| Groq API down                              | Caught in `chat_service.handle_user_message`, the user sees a friendly "language backend down, try again" message, error logged. `/healthz/deep` reports `groq.ok=false` so a monitoring probe catches it without a user complaining first. |
| **LLM emits an invalid tool argument**     | `BadRequestError` from Groq is caught once; the offending tool message is fed back to the LLM as a system note and the call is retried. Stops the LLM looping on `days_ahead: 0` style mistakes. |
| LLM loops on tool calls                    | Hard cap of `max_tool_iterations=4` per user turn; falls back to a friendly "I got stuck, can you rephrase?" message.                                          |
| Bad OAuth state on callback                | The inflight-state buffer holds the last few states, so legitimate double-clicks during consent don't 400. Genuine mismatches still fail closed with a 400 and a clear log entry. |

---

## 6. Trade-offs

| Trade-off                                                  | Why                                                                                                                                         |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| **In-memory conversation history** (per-session dict)      | Plenty for a demo on one instance. For production, this is a 10-line swap to Redis / Postgres.                                              |
| **Single primary calendar** (not multi-calendar)           | The task is "schedule a meeting", not "audit 8 calendars". Keeps the LLM prompt simple and avoids accidentally booking onto someone's hobby calendar. |
| **No real OAuth user accounts**, just signed sessions      | The MNGR reviewer wants to connect their calendar in 30 seconds, not register an account first.                                             |
| **LLM-driven slot picking** (rather than form-filling UI)  | The brief asked for "scheduling preferences through natural language conversation" - pure chat. The LLM does the disambiguation.            |
| **Pure-function `scheduler_service`**                      | Lets me unit-test slot finding without mocking the Google client. Costs one extra module of code.                                           |
| **Tool calling instead of JSON-mode parsing**              | More robust against malformed output and lets the LLM call the calendar mid-conversation, not just at the end.                              |
| **No event invites/attendees in UI**                       | The tool supports `attendees`, the UI doesn't surface it. Could be added once the basic flow is proven.                                     |

---

## 7. Setup (step by step)

> Requires **Python 3.11+**.

```bash
git clone <repo-url> mngr-calendar-chatbot
cd mngr-calendar-chatbot

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and fill in GROQ_API_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
# and a random SESSION_SECRET.

uvicorn main:app --reload --port 8000
```

Then open <http://localhost:8000>.

### Run the tests

```bash
pytest -v
```

The **36 included tests** cover:

- Slot-finding, busy-interval merging, timezone handling, "no availability"
  edge cases, round-up-to-next-15-minutes, free-slot label formatting.
- The chat-service tool dispatcher, including defensive argument clamping
  (`days_ahead=0` -> `1`, etc.) and the working-window widening logic.
- The **pre-insert freebusy race fix**: a conflict appearing in the window
  blocks `events.insert` from firing, edge-touching intervals are correctly
  treated as non-conflicts, and a failed recheck propagates rather than
  blindly booking.
- The **friendly Google error translator** for the three commonest
  production failures (`accessNotConfigured`, `401`, `403/forbidden`).
- The **`/healthz` and `/healthz/deep` endpoints** in three states: both
  upstreams healthy, Groq unreachable, Groq key missing.
- The **request-ID middleware**: header is generated and echoed, an inbound
  `X-Request-ID` is honoured for gateway tracing.

No network is touched in any test - all HTTP calls are mocked at the seam.
CI runs the suite on both Python 3.11 and 3.12.

---

## 8. Connecting your own Google Calendar (for the MNGR reviewer)

You don't need access to anyone else's project - you'll create your own
Google Cloud OAuth client. Takes 3 minutes.

1. Go to <https://console.cloud.google.com/>.
2. Create (or pick) a project. Name doesn't matter.
3. **Enable Google Calendar API**: APIs & Services → Library → search
   "Google Calendar API" → click it → **Enable**.
   (Easy to skip - OAuth will work without it, but the first API call
   returns `accessNotConfigured` 403. The app surfaces a clickable
   "enable it here" link if that happens.)
4. **OAuth consent screen**: APIs & Services → OAuth consent screen.
   - User type: **External**.
   - App name: anything (e.g. "MNGR Calendar Chatbot Local").
   - Add your own Google account as a **Test user** (this is the magic step
     - Google lets the app work for test users without app verification).
   - Scopes can be left default; the app requests them at runtime.
5. **Create credentials**: APIs & Services → Credentials → Create
   credentials → **OAuth client ID**.
   - Application type: **Web application**.
   - Authorized redirect URI:
     `http://localhost:8000/auth/google/callback`.
   - Hit Create. Copy the **Client ID** and **Client secret**.
6. Paste them into `.env`:
   ```dotenv
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   GOOGLE_REDIRECT_URI=http://localhost:8000/auth/google/callback
   GROQ_API_KEY=...
   SESSION_SECRET=any-long-random-string
   OAUTHLIB_INSECURE_TRANSPORT=1
   ```
7. `uvicorn main:app --reload --port 8000` and visit <http://localhost:8000>.
8. Click **Connect Google Calendar** → sign in with the Google account you
   added as a test user → consent.
9. Chat away. Try:
   - "Book a 30 minute design review with the team tomorrow afternoon."
   - "Find me a 1-hour slot on Wednesday in the morning."
   - "I'm in Europe/London - any free time today after 4pm?"

The created events appear on the calendar of whichever Google account you
signed in with. Different browsers (or incognito windows) get independent
sessions, so you can test from multiple accounts in parallel.

---

## 9. Known limitations

- **In-memory conversation state.** Restarting the server clears all
  in-flight conversations. Tokens persist on disk.
- **Single user per browser session.** "Switching" Google accounts means
  clicking *Reconnect* and signing in again.
- **Working hours are simple bounds.** No support for "I never take
  meetings Mondays" rules; would need a small preferences model.
- **No attendees in the UI** (the API supports it, the chat UI doesn't yet
  surface invitee email entry).
- **No streaming output yet.** The final assistant reply is sent as a
  single JSON response. Streaming via SSE is on the roadmap (see §10),
  but is genuinely fiddly given the tool-call/loop pattern - any
  intermediate token could turn out to be a tool call.
- **Timezone autodetection is best-effort.** The frontend sends the
  browser's `Intl` timezone in the request body, but the LLM still falls
  back to UTC if the message doesn't mention one.

---

## 10. What I'd build next

Given another couple of days and a brief about real users:

1. **Streaming responses** - server-sent events from `/chat` so the final
   reply (after any tool calls resolve) renders token-by-token. Groq's
   streaming is genuinely fast and a clear UX win.
2. **Multi-calendar awareness** - read all calendars the user owns, not just
   "primary", so events on shared workspace calendars don't double-book them.
3. **Attendee handling end-to-end** - let the user say "with sam@...",
   surface it in the UI, send invites, optionally check attendees' freebusy.
4. **Smarter slot ranking** - prefer slots that don't fragment the day,
   avoid back-to-back-to-back meetings, respect user "focus hours".
5. **Persistence layer** - Postgres for conversation history, tokens, and
   user preferences; multi-instance ready.
6. **MNGR-side integration** - wire the chatbot into the MNGR project
   sidebar: pre-fill the project's stakeholders as attendees, file events
   under the right project automatically, surface them in the project
   timeline.
7. **LLM evaluation harness** - recorded conversations + scripted tool stubs
   to regression-test the LLM's behaviour when the prompt or model changes.
8. **Other calendar backends** - Outlook/Microsoft 365 via Microsoft Graph,
   so MNGR users on either side of the corporate/indie divide get the same
   experience.

---

## Project layout

```
mngr-calendar-chatbot/
├── .github/
│   └── workflows/ci.yml          Pytest + ruff on every push (3.11 & 3.12).
├── main.py                       FastAPI entrypoint, routes, sessions,
│                                 RequestIDMiddleware, /healthz/deep.
├── auth/
│   └── google_auth.py            OAuth2 flow + per-session token store.
├── services/
│   ├── calendar_service.py       Google Calendar reads/writes,
│   │                             pre-insert freebusy race-check,
│   │                             friendly HTTP error translator.
│   ├── chat_service.py           Groq + tool calling orchestration,
│   │                             SlotConflict handling.
│   └── scheduler_service.py      Pure slot-finding algorithm.
├── models/
│   └── schemas.py                Pydantic models.
├── utils/
│   ├── config.py                 Env config (pydantic-settings).
│   └── logger.py                 Rotating logger; text or JSON mode
│                                 (LOG_FORMAT=json), request-id contextvar.
├── static/
│   └── index.html                Single-page chat UI: suggested-prompt
│                                 chips, retry button, upstream-health pill.
├── tests/
│   ├── test_calendar.py          Slot-finder unit tests.
│   ├── test_calendar_service.py  Race-fix and friendly-error translator.
│   ├── test_chat_service.py      LLM tool dispatcher (incl. SlotConflict).
│   └── test_healthz.py           /healthz, /healthz/deep, request-id.
├── pyproject.toml                Ruff + pytest config.
├── tokens/                       Per-session OAuth tokens (gitignored).
├── logs/                         logs/app.log (gitignored).
├── .env.example                  Env template.
├── requirements.txt
└── README.md
```

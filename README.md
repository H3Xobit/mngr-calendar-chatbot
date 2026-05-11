# MNGR Calendar Chatbot

A small, opinionated calendar-scheduling chatbot built for the MNGR technical
task. You chat with it in natural language ("Book a 30-minute design review
tomorrow afternoon"), it reads your Google Calendar to find real free slots,
and creates the event when you pick one.

> **Stack:** FastAPI · Groq `llama-3.3-70b-versatile` · Google Calendar API (OAuth2) · vanilla HTML/CSS/JS

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
| Google API error / network blip            | `CalendarError` raised in `calendar_service`, caught in `chat_service`, returned to the LLM as `{"error":"calendar_error","message":...}`. LLM apologises and asks if they want to retry. Full traceback logged to `logs/app.log`. |
| User input is ambiguous                    | The system prompt instructs the LLM to ask one clarifying question at a time rather than guessing.                                                              |
| Access / refresh token expired             | `load_credentials` runs `Credentials.refresh()` automatically if a refresh token exists.                                                                       |
| Refresh fails / scope changed              | Tool returns `{"error":"auth_required"}`. The HTTP response sets `requires_auth: true`. The UI shows "Connect Google Calendar" again.                          |
| Groq API down                              | Caught in `chat_service.handle_user_message`, the user sees a friendly "language backend down, try again" message, error logged.                               |
| LLM loops on tool calls                    | Hard cap of `max_tool_iterations=4` per user turn; falls back to a friendly "I got stuck, can you rephrase?" message.                                          |
| Bad OAuth state on callback                | 400 response; user is redirected to home and asked to retry.                                                                                                   |

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

The 21 included tests cover slot-finding, busy-interval merging, timezone
handling, "no availability" edge cases, the round-up-to-next-15-minutes
behaviour, the chat-service tool dispatcher, defensive argument clamping
(`days_ahead=0` -> `1`, etc.), and the working-window widening logic.
No network is touched.

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
  in-flight conversations. Tokens persist (they're on disk).
- **Single user per browser session.** "Switching" Google accounts means
  clicking *Reconnect* and signing in again.
- **Working hours are simple bounds.** No support for "I never take
  meetings Mondays" rules; would need a small preferences model.
- **No conflict re-check at booking time.** We pick from slots returned by
  `freebusy`; if a conflicting event lands in the few seconds between
  proposal and booking, the event still gets created. A pre-insert
  `freebusy` recheck would close that race.
- **No attendees in the UI** (the API supports it, the chat UI doesn't yet
  surface invitee email entry).
- **No tests for the LLM layer.** Tool-calling integration is exercised
  manually; mocking Groq for deterministic tests is a follow-up.
- **No timezone autodetection.** The LLM defaults to UTC unless the user
  states a timezone. A real product should read the browser timezone and
  pass it through.

---

## 10. What I'd build next

Given another couple of days and a brief about real users:

1. **Multi-calendar awareness** - read all calendars the user owns, not just
   "primary", so events on shared workspace calendars don't double-book them.
2. **Pre-insert race check** - re-query `freebusy` immediately before
   `events.insert` and fail-safe if a conflict appeared.
3. **Attendee handling end-to-end** - let the user say "with sam@...", surface
   it in the UI, send invites, optionally check attendees' freebusy.
4. **Smarter slot ranking** - prefer slots that don't fragment the day,
   avoid back-to-back-to-back meetings, respect user "focus hours".
5. **Persistence layer** - Postgres for conversation history, tokens, and
   user preferences; multi-instance ready.
6. **Streaming responses** - server-sent events from `/chat` so replies
   render token-by-token. With Groq this is genuinely fast and a big UX win.
7. **MNGR-side integration** - wire the chatbot into the MNGR project
   sidebar: pre-fill the project's stakeholders as attendees, file events
   under the right project automatically, surface them in the project
   timeline.
8. **Evaluation harness** - recorded conversations + scripted tool stubs to
   regression-test the LLM's behaviour when the prompt or model changes.
9. **Other calendar backends** - Outlook/Microsoft 365 via Microsoft Graph,
   so MNGR users on either side of the corporate/indie divide get the same
   experience.

---

## Project layout

```
mngr-calendar-chatbot/
├── main.py                       FastAPI entrypoint, routes, sessions.
├── auth/
│   └── google_auth.py            OAuth2 flow + per-session token store.
├── services/
│   ├── calendar_service.py       Google Calendar reads/writes.
│   ├── chat_service.py           Groq + tool calling orchestration.
│   └── scheduler_service.py      Pure slot-finding algorithm.
├── models/
│   └── schemas.py                Pydantic models.
├── utils/
│   ├── config.py                 Env config (pydantic-settings).
│   └── logger.py                 Rotating file + stdout logger.
├── static/
│   └── index.html                Single-page chat UI.
├── tests/
│   ├── test_calendar.py          Unit tests for the slot finder.
│   └── test_chat_service.py      Unit tests for the LLM tool dispatcher.
├── tokens/                       Per-session OAuth tokens (gitignored).
├── logs/                         logs/app.log (gitignored).
├── .env.example                  Env template.
├── requirements.txt
└── README.md
```

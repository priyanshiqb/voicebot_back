# Scheduling & Booking — Deployment / Ops Checklist

Everything the scheduling module needs to go live. The code is fully implemented
and tested (79 scheduling tests, Postgres). What remains is **operational**:
dependencies, migrations, env vars, background workers, and three external service
setups (Google Calendar, Google webhooks, Firebase). Each external piece **degrades
gracefully** — the system runs without it, that feature just stays off.

## 0. What's built

| Phase | Capability | Migrations |
|---|---|---|
| 1 | Event types, availability, bookings with DB-enforced no-double-booking, holds, `.ics` email | `0020`, `0021` |
| 2 | Emergency-leave auto-reassign / flag-for-reschedule, reminders, hold cleanup | `0022` |
| — | COLLECTIVE (one Booking per host) | `0023` |
| 3 | Google Calendar: encrypted OAuth, free/busy, event push/delete, token refresh | `0024` |
| 3.5 | Webhook incremental sync (watch channels, echo guard, reassignment re-sync) | `0025` |
| 4 | FCM push alerts, public single-use rebooking | `0026` |
| + | **Microsoft 365 / Outlook** (Graph): OAuth, free/busy, event push/delete, delta sync, webhook | **none** (reuses `OUTLOOK` provider value) |

---

## 1. Dependencies

Python packages are already in `pyproject.toml` / `uv.lock`:
`cryptography`, `google-auth`, `google-auth-oauthlib`, `google-api-python-client`,
`firebase-admin`, `msal` (Outlook / Microsoft Graph OAuth). Install with:

```bash
uv sync
```

**Postgres extension:** migration `0020` runs `CREATE EXTENSION IF NOT EXISTS btree_gist`
(required by the booking overlap exclusion constraint). The migrating DB role needs
permission to create extensions (superuser, or granted). One-time per database.

---

## 2. Migrations

```bash
uv run python manage.py migrate
```

Applies `core` migrations `0020`–`0026`. All additive (new tables/fields); no
backfill. Reversible.

---

## 3. Environment variables

Add to your `.env` / secrets. All have safe defaults (blank ⇒ feature off) **except**
the encryption key, which is required before any calendar is connected.

| Variable | Required for | Example / default |
|---|---|---|
| `SCHEDULING_TOKEN_ENCRYPTION_KEY` | Calendar OAuth (token encryption) | Fernet key (see §4.1) |
| `GOOGLE_CALENDAR_CLIENT_ID` | Google Calendar | from Google Cloud |
| `GOOGLE_CALENDAR_CLIENT_SECRET` | Google Calendar | from Google Cloud |
| `GOOGLE_CALENDAR_REDIRECT_URI` | Google Calendar | `https://api.example.com/api/v1/core/calendar-integrations/callback/` |
| `GOOGLE_CALENDAR_WEBHOOK_URL` | Webhook sync (optional) | `https://api.example.com/api/v1/core/calendar-webhook/google/` |
| `MICROSOFT_CALENDAR_CLIENT_ID` | Outlook / M365 | from Azure (Entra) app registration |
| `MICROSOFT_CALENDAR_CLIENT_SECRET` | Outlook / M365 | from Azure app registration |
| `MICROSOFT_CALENDAR_REDIRECT_URI` | Outlook / M365 | `https://api.example.com/api/v1/core/calendar-integrations/callback/` (same callback as Google) |
| `MICROSOFT_CALENDAR_AUTHORITY` | Outlook / M365 (optional) | `https://login.microsoftonline.com/common` (default) |
| `MICROSOFT_CALENDAR_WEBHOOK_URL` | Outlook webhook sync (optional) | `https://api.example.com/api/v1/core/calendar-webhook/microsoft/` |
| `FIREBASE_CREDENTIALS` | FCM push (optional) | `/run/secrets/firebase.json` |
| `BOOKING_HOLD_TTL_SECONDS` | — | `300` |
| `BOOKING_REMINDER_OFFSETS_MINUTES` | — | `1440,60` |
| `BOOKING_RESCHEDULE_TOKEN_TTL_HOURS` | — | `72` |
| `EXTERNAL_FREEBUSY_CACHE_SECONDS` | — | `60` |

> `GOOGLE_CALENDAR_SCOPES` and `MICROSOFT_CALENDAR_SCOPES` are code settings, not env vars.
> Google needs no migration for Outlook — the `OUTLOOK` provider value already exists.

---

## 4. External service setup

### 4.1 Encryption key (required)

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the output in `SCHEDULING_TOKEN_ENCRYPTION_KEY`. Rotating keys later: set
`SCHEDULING_TOKEN_ENCRYPTION_KEYS` (list, newest first) — decryption falls back
through them (MultiFernet).

### 4.2 Google Calendar OAuth

1. Google Cloud Console → create/select a project → **enable the Google Calendar API**.
2. **OAuth consent screen:** External; add app name, support email, homepage +
   privacy-policy URLs, and the two scopes above (both are *sensitive*).
3. **Credentials → OAuth client ID → Web application**; add the exact redirect URI
   (`GOOGLE_CALENDAR_REDIRECT_URI`). Copy Client ID + Secret to env.
4. **Testing vs Production:** while the app is in *Testing* you can add up to 100 test
   users and skip verification — but external users' refresh tokens **expire after 7
   days** (they must reconnect). Submit for **verification** before public launch.

### 4.3 Google webhooks (optional — otherwise on-demand free/busy is used)

- Set `GOOGLE_CALENDAR_WEBHOOK_URL` to the public callback (must be **HTTPS on a
  verified domain** — Google won't push to localhost; use a tunnel like ngrok in dev).
- No extra Google config beyond the Calendar API. Watch channels are created on
  connect and renewed hourly by the Beat task.

### 4.4 Microsoft 365 / Outlook OAuth (Microsoft Graph)

1. **Entra ID (Azure AD) → App registrations → New registration.** Supported account
   types: *Accounts in any organizational directory and personal Microsoft accounts*
   (multi-tenant + consumer). Record the **Application (client) ID** →
   `MICROSOFT_CALENDAR_CLIENT_ID`.
2. **Redirect URI (Web):** the callback URL (`MICROSOFT_CALENDAR_REDIRECT_URI`) — this can be
   the **same** callback the Google flow uses; the provider is carried in the signed OAuth
   `state`.
3. **API permissions → Microsoft Graph → Delegated:** `Calendars.ReadWrite`, `offline_access`,
   `User.Read`, `openid`, `email`, `profile`. (`offline_access` is **mandatory** — without it
   there's no refresh token and the integration dies at first token expiry. Admin consent may
   be required per customer org.)
4. **Certificates & secrets → New client secret** → copy the value to
   `MICROSOFT_CALENDAR_CLIENT_SECRET`.
5. Authority defaults to `https://login.microsoftonline.com/common` (work + personal). Use
   `.../organizations` for work-only, or a tenant id to lock to one org.

> No token-revoke endpoint exists on Graph — disconnect stops the watch and soft-deletes
> locally (tokens dropped). This is expected, not an error.

### 4.5 Microsoft Graph webhooks (optional — otherwise on-demand free/busy is used)

- Set `MICROSOFT_CALENDAR_WEBHOOK_URL` to the public callback. **It must be reachable HTTPS at
  the moment a subscription is created** — Graph validates it synchronously (echoes a
  `validationToken`). Use a tunnel (VS Code Dev Tunnels / ngrok) in dev, or leave blank to run
  on-demand free/busy only.
- Subscriptions for `/me/events` expire in ~70 h; the hourly `renew_calendar_watches` Beat task
  renews them (shared with Google — no extra config).

### 4.6 Firebase Cloud Messaging (optional)

1. Firebase console → project settings → **Service accounts → generate private key**.
2. Mount the JSON and set `FIREBASE_CREDENTIALS` to its path.
3. Clients register device tokens via `POST /api/v1/core/device-tokens/`.

---

## 5. Background workers

The scheduling module needs a **Celery worker** (async email/calendar/push, reschedule
task) and **Celery Beat** (periodic jobs). Broker/result backend is Redis DB `/2`
(already configured).

```bash
# Worker (Linux / Docker)
uv run celery -A config worker -l info

# Worker (Windows dev): prefork pool doesn't work — use solo
uv run celery -A config worker -l info --pool=solo

# Beat scheduler (run exactly one instance)
uv run celery -A config beat -l info
```

Beat schedule (defined in settings, no `django-celery-beat` needed):

| Task | Interval | Purpose |
|---|---|---|
| `send_due_reminders` | 5 min | Upcoming-booking reminders (idempotent) |
| `cleanup_expired_holds` | 1 min | Release lapsed holds |
| `refresh_calendar_tokens` | 15 min | Refresh Google + Outlook access tokens near expiry |
| `renew_calendar_watches` | hourly | Renew Google + Outlook watch channels before they expire |

---

## 6. Endpoints

All under `/api/v1/core/`. Authenticated unless marked public.

**Authenticated:** `event-templates/`, `event-skills/`, `availability-profiles/`,
`availability-exceptions/`, `availability/available-slots/`,
`bookings/` (+ `hold/`, `{id}/confirm|cancel|complete|no-show/`),
`calendar-integrations/` (+ `connect/?provider=GOOGLE|OUTLOOK`, `callback/`, DELETE=disconnect),
`device-tokens/` (+ `deactivate/`).

**Public (unauthenticated):**
- `calendar-webhook/google/` — Google pushes calendar changes here (validated by
  channel token).
- `calendar-webhook/microsoft/` — Microsoft Graph pushes calendar changes here
  (validation-token handshake + `clientState`-validated notifications).
- `public/reschedule/<uuid:token>/` — customer rebooking (GET slots, POST rebook);
  single-use, expiring, throttled 30/min.

---

## 7. Tests

The default test settings use SQLite, which can't host the exclusion constraint (and
is already broken by a pre-existing `ArrayField`). Run against Postgres:

```bash
uv run python manage.py test core --settings=config.settings.test_postgres
```

Scheduling suite lives in `core/tests/scheduling/` (79 tests).

---

## 8. Suggested rollout order

1. Deploy code + run migrations; set `SCHEDULING_TOKEN_ENCRYPTION_KEY`. Start worker +
   beat. → Phases 1–2 fully functional (internal bookings, reminders, leave/reschedule,
   emails). Calendar/push stay off (blank env).
2. Add Google OAuth creds → hosts can connect calendars (free/busy + event mirroring).
3. Set `GOOGLE_CALENDAR_WEBHOOK_URL` (verified domain) → near-real-time sync.
4. Add `FIREBASE_CREDENTIALS` → internal push alerts.
5. Build front-end pages for the OAuth connect flow and the public reschedule link.

---

## 9. Operational notes

- **Graceful degradation:** calendar API errors never block booking (free/busy fails
  open); push/calendar tasks no-op when unconfigured; webhook validates channel token
  and always 200s.
- **Idempotency:** notifications (`NotificationLog` dedup), calendar push
  (`external_event_id`), reschedule (status guards), webhook sync (echo guard).
- **Watch monitoring:** alert on `CalendarIntegration.sync_status = REAUTH_REQUIRED`
  (token refresh failed → host must reconnect).
- **Beat singleton:** run only one Beat process to avoid duplicate periodic runs
  (workers can scale freely).
```

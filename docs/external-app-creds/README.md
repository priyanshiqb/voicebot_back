# External App Credentials

Step-by-step guides to obtain every third-party credential this backend needs, with an exact
**field-level mapping** from each provider's console value to the `.env` variable and the Django
setting that reads it.

Two integrations require external setup. **Both degrade gracefully** — a blank credential
turns that feature off rather than crashing (except the shared encryption key, which is
required before *any* calendar is connected).

| # | App | Powers | Guide |
|---|-----|--------|-------|
| 1 | **Firebase Cloud Messaging** | Internal push alerts (reassignment, needs-reschedule) | [01-firebase-fcm.md](01-firebase-fcm.md) |
| 2 | **Google Calendar** | Host free/busy + booking mirror + webhook sync | [02-google-calendar.md](02-google-calendar.md) |

---

## 0. Shared prerequisite — calendar token encryption key (REQUIRED)

Calendar OAuth tokens are stored **encrypted** (Fernet). You must set this **once** before
connecting any calendar — the connect endpoint refuses to start without it.

Generate a key:
```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

| Value | `.env` variable | Django setting | Read by |
|-------|-----------------|----------------|---------|
| Fernet key (44-char base64) | `SCHEDULING_TOKEN_ENCRYPTION_KEY` | `SCHEDULING_TOKEN_ENCRYPTION_KEY` | [core/services/crypto.py](../../core/services/crypto.py) |
| Rotation (optional, newest first) | `SCHEDULING_TOKEN_ENCRYPTION_KEYS` (list) | `SCHEDULING_TOKEN_ENCRYPTION_KEYS` | crypto.py (MultiFernet) |

> **Rotation:** to rotate, prepend a new key to `SCHEDULING_TOKEN_ENCRYPTION_KEYS` (comma list,
> newest first). Decryption falls back through all keys; new writes use the first. Never drop a
> key that still has live tokens encrypted under it.

---

## Consolidated `.env` block

Copy this, then fill each value using the per-app guides. Replace `api.example.com` with your
real backend host and `app.example.com` with your frontend.

```dotenv
# ── Shared: calendar OAuth token encryption (REQUIRED before any calendar connect) ──
SCHEDULING_TOKEN_ENCRYPTION_KEY=paste-fernet-key-here

# ── 1. Firebase Cloud Messaging (push) ──
FIREBASE_CREDENTIALS=/run/secrets/firebase-service-account.json

# ── 2. Google Calendar ──
GOOGLE_CALENDAR_CLIENT_ID=1234567890-abcdef.apps.googleusercontent.com
GOOGLE_CALENDAR_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxx
GOOGLE_CALENDAR_REDIRECT_URI=https://api.example.com/api/v1/core/calendar-integrations/callback/
GOOGLE_CALENDAR_WEBHOOK_URL=https://api.example.com/api/v1/core/calendar-webhook/google/
```

---

## Master variable → setting map

| `.env` variable | Required? | App | Django setting | Consumed by |
|-----------------|-----------|-----|----------------|-------------|
| `SCHEDULING_TOKEN_ENCRYPTION_KEY` | ✅ (for any calendar) | Shared | `SCHEDULING_TOKEN_ENCRYPTION_KEY` | crypto.py |
| `FIREBASE_CREDENTIALS` | Optional (push off if blank) | FCM | `FIREBASE_CREDENTIALS` | push.py |
| `GOOGLE_CALENDAR_CLIENT_ID` | ✅ (for Google) | Google | `GOOGLE_CALENDAR_CLIENT_ID` | calendars/google.py |
| `GOOGLE_CALENDAR_CLIENT_SECRET` | ✅ (for Google) | Google | `GOOGLE_CALENDAR_CLIENT_SECRET` | calendars/google.py |
| `GOOGLE_CALENDAR_REDIRECT_URI` | ✅ (for Google) | Google | `GOOGLE_CALENDAR_REDIRECT_URI` | calendars/google.py |
| `GOOGLE_CALENDAR_WEBHOOK_URL` | Optional (on-demand if blank) | Google | `GOOGLE_CALENDAR_WEBHOOK_URL` | calendar_sync.py |

> **Code-only settings (not env vars):** `GOOGLE_CALENDAR_SCOPES` lives in
> `config/settings/base.py`. Related but general:
> `FRONTEND_PROJECT_URL` (the OAuth flow redirects the browser back to
> `{FRONTEND_PROJECT_URL}/settings/calendar?connected=1` after a successful connect).

See also the [scheduling deployment checklist](../scheduling-deployment-checklist.md) for the
full go-live runbook.

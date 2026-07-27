# 2. Google Calendar

**Powers:** subtracting a host's real Google Calendar busy times from availability, mirroring
bookings onto their calendar, and (optionally) near-real-time webhook sync. Consumed by
[core/services/calendars/google.py](../../core/services/calendars/google.py).

**What the backend needs:** an **OAuth 2.0 Web client** (Client ID + Secret), the exact
**redirect URI**, and — for webhooks — a public **notification URL**. Plus the shared
[encryption key](README.md#0-shared-prerequisite--calendar-token-encryption-key-required).

---

## Step by step — obtain the OAuth client

1. Open the **[Google Cloud Console](https://console.cloud.google.com/)** → create or select a
   **project** (top project dropdown).
2. **APIs & Services → Library** → search **"Google Calendar API"** → **Enable**.
3. **APIs & Services → OAuth consent screen:**
   - **User type: External** (unless every host is in your Google Workspace org, then Internal).
   - Fill app name, user-support email, developer contact. Add homepage + privacy-policy URLs.
   - **Scopes:** add the two the app uses (both are *sensitive*):
     - `https://www.googleapis.com/auth/calendar.events`
     - `https://www.googleapis.com/auth/calendar.freebusy`
   - Save.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID:**
   - **Application type: Web application.**
   - **Authorized redirect URIs → Add URI:** paste your callback **exactly** (must match
     `GOOGLE_CALENDAR_REDIRECT_URI` character-for-character, including the trailing slash):
     `https://api.example.com/api/v1/core/calendar-integrations/callback/`
   - Create. A dialog shows **Client ID** and **Client secret** — copy both.
5. (Webhook only) no extra Google setup — the watch channel is created on connect. You just need
   `GOOGLE_CALENDAR_WEBHOOK_URL` to be a **public HTTPS URL on a verified domain** (below).

---

## Field-level mapping

| What you get | Where in console | `.env` variable | Django setting | Consumed by |
|--------------|------------------|-----------------|----------------|-------------|
| **Client ID** (`…apps.googleusercontent.com`) | Credentials → OAuth client ID | `GOOGLE_CALENDAR_CLIENT_ID` | `GOOGLE_CALENDAR_CLIENT_ID` | google.py `_flow()`, `_credentials()` |
| **Client secret** (`GOCSPX-…`) | Credentials → OAuth client ID | `GOOGLE_CALENDAR_CLIENT_SECRET` | `GOOGLE_CALENDAR_CLIENT_SECRET` | google.py `_flow()`, `_credentials()` |
| **Authorized redirect URI** (you choose it) | Credentials → OAuth client → Redirect URIs | `GOOGLE_CALENDAR_REDIRECT_URI` | `GOOGLE_CALENDAR_REDIRECT_URI` | google.py `_flow()` |
| **Public webhook URL** (your endpoint) | — (your infra) | `GOOGLE_CALENDAR_WEBHOOK_URL` | `GOOGLE_CALENDAR_WEBHOOK_URL` | calendar_sync.py `setup_watch()` |
| Encryption key | see [shared](README.md#0-shared-prerequisite--calendar-token-encryption-key-required) | `SCHEDULING_TOKEN_ENCRYPTION_KEY` | same | crypto.py |

> **Scopes are code, not env:** `GOOGLE_CALENDAR_SCOPES` is defined in `config/settings/base.py`.
> The scopes you list on the consent screen must match those.

### `.env`
```dotenv
GOOGLE_CALENDAR_CLIENT_ID=1234567890-abcdef.apps.googleusercontent.com
GOOGLE_CALENDAR_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxx
GOOGLE_CALENDAR_REDIRECT_URI=https://api.example.com/api/v1/core/calendar-integrations/callback/
# Optional — leave blank for on-demand free/busy instead of webhooks:
GOOGLE_CALENDAR_WEBHOOK_URL=https://api.example.com/api/v1/core/calendar-webhook/google/
```

---

## The two URLs, precisely

| URL | Points at | Rule |
|-----|-----------|------|
| **Redirect URI** (`GOOGLE_CALENDAR_REDIRECT_URI`) | `…/calendar-integrations/callback/` | Must be **registered in the console** and match the env value **exactly** (trailing slash included), else Google returns `redirect_uri_mismatch`. |
| **Webhook URL** (`GOOGLE_CALENDAR_WEBHOOK_URL`) | `…/calendar-webhook/google/` | Must be **public HTTPS on a domain Google can resolve**. Google won't push to `localhost` — use a tunnel (ngrok) in dev. |

---

## Gotchas

- **Testing vs Production consent screen:** while the app is in **Testing**, you may add up to 100
  test users without verification — **but external users' refresh tokens expire after 7 days**
  (they must reconnect). Submit for **verification** before public launch.
- **`redirect_uri_mismatch`** is almost always a trailing-slash or scheme (`http` vs `https`)
  difference between the console entry and `GOOGLE_CALENDAR_REDIRECT_URI`.
- **Same callback for all providers:** this redirect URI is shared with Outlook — the provider is
  carried in the signed OAuth `state`, so you register the same path in both consoles.
- **Watch renewal is automatic:** channels are created on connect and renewed hourly by the
  `renew_calendar_watches` Beat task — no cron of your own.

See the [Google Calendar sync flow](../flows/04-google-calendar-sync.md) for the runtime picture.

# 3. Microsoft 365 / Outlook (Microsoft Graph)

**Powers:** subtracting a host's Outlook / M365 busy times from availability, mirroring bookings
onto their calendar, and (optionally) real-time webhook (change-notification) sync. Consumed by
[core/services/calendars/microsoft.py](../../core/services/calendars/microsoft.py).

**What the backend needs:** an **Entra ID (Azure AD) app registration** — Application (client) ID
+ a client secret, the exact **redirect URI**, and — for webhooks — a public **notification URL**.
Plus the shared
[encryption key](README.md#0-shared-prerequisite--calendar-token-encryption-key-required).

> The **Microsoft Graph API is free** — no per-call or per-user charge. Only the host needs an
> Outlook mailbox (free Outlook.com or an M365 subscription they already have).

---

## Step by step — register the app in Entra ID

1. Open the **[Azure / Entra admin center](https://entra.microsoft.com/)** (or
   portal.azure.com → **Microsoft Entra ID**) → **App registrations → New registration**.
2. **Name:** anything (e.g. "Voicebot Calendar"). **Supported account types:**
   *Accounts in any organizational directory **and** personal Microsoft accounts* (this gives you
   work + personal / Outlook.com, matching the `/common` authority).
3. **Redirect URI:** platform **Web**, value = your callback (must equal
   `MICROSOFT_CALENDAR_REDIRECT_URI` exactly):
   `https://api.example.com/api/v1/core/calendar-integrations/callback/`
   → **Register**.
4. On the app's **Overview**, copy the **Application (client) ID** → `MICROSOFT_CALENDAR_CLIENT_ID`.
5. **API permissions → Add a permission → Microsoft Graph → Delegated permissions** → add:
   - `Calendars.ReadWrite`
   - `offline_access`  ← **mandatory** (grants the refresh token)
   - `User.Read`, `openid`, `email`, `profile`
   - (If your customers are orgs with admin-consent policies, click **Grant admin consent**.)
6. **Certificates & secrets → Client secrets → New client secret** → set an expiry → **Add** →
   copy the **Value** (not the Secret ID) immediately → `MICROSOFT_CALENDAR_CLIENT_SECRET`.
   *(The value is shown only once.)*
7. (Webhook only) ensure `MICROSOFT_CALENDAR_WEBHOOK_URL` is **public HTTPS reachable at
   subscription-creation time** — Graph validates it synchronously (below).

---

## Field-level mapping

| What you get | Where in Entra | `.env` variable | Django setting | Consumed by |
|--------------|----------------|-----------------|----------------|-------------|
| **Application (client) ID** (GUID) | App → Overview | `MICROSOFT_CALENDAR_CLIENT_ID` | `MICROSOFT_CALENDAR_CLIENT_ID` | microsoft.py `_msal_app()` |
| **Client secret → Value** | Certificates & secrets | `MICROSOFT_CALENDAR_CLIENT_SECRET` | `MICROSOFT_CALENDAR_CLIENT_SECRET` | microsoft.py `_msal_app()` |
| **Redirect URI** (you choose it) | App registration → Redirect URIs | `MICROSOFT_CALENDAR_REDIRECT_URI` | `MICROSOFT_CALENDAR_REDIRECT_URI` | microsoft.py `authorization_url()`, `exchange_code()` |
| **Authority** (account-type scope) | — (default is fine) | `MICROSOFT_CALENDAR_AUTHORITY` | `MICROSOFT_CALENDAR_AUTHORITY` | microsoft.py `_msal_app()` |
| **Public webhook URL** (your endpoint) | — (your infra) | `MICROSOFT_CALENDAR_WEBHOOK_URL` | `MICROSOFT_CALENDAR_WEBHOOK_URL` | calendar_sync.py `setup_watch()` |
| Encryption key | see [shared](README.md#0-shared-prerequisite--calendar-token-encryption-key-required) | `SCHEDULING_TOKEN_ENCRYPTION_KEY` | same | crypto.py |

> **Scopes / Graph base are code, not env:** `MICROSOFT_CALENDAR_SCOPES` and
> `MICROSOFT_GRAPH_BASE_URL` live in `config/settings/base.py`. The delegated permissions you add
> in Entra must cover those scopes.

### `.env`
```dotenv
MICROSOFT_CALENDAR_CLIENT_ID=00000000-0000-0000-0000-000000000000
MICROSOFT_CALENDAR_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
MICROSOFT_CALENDAR_REDIRECT_URI=https://api.example.com/api/v1/core/calendar-integrations/callback/
# Optional (default = work + personal):
MICROSOFT_CALENDAR_AUTHORITY=https://login.microsoftonline.com/common
# Optional — leave blank for on-demand free/busy instead of webhooks:
MICROSOFT_CALENDAR_WEBHOOK_URL=https://api.example.com/api/v1/core/calendar-webhook/microsoft/
```

### Authority options
| Value | Who can connect |
|-------|-----------------|
| `https://login.microsoftonline.com/common` (default) | Work **and** personal Microsoft accounts |
| `https://login.microsoftonline.com/organizations` | Work/school accounts only |
| `https://login.microsoftonline.com/<tenant-id>` | One specific org only |

---

## The two URLs, precisely

| URL | Points at | Rule |
|-----|-----------|------|
| **Redirect URI** (`MICROSOFT_CALENDAR_REDIRECT_URI`) | `…/calendar-integrations/callback/` | Must be **registered in Entra** and match the env value exactly (trailing slash included). Can be the **same** callback as Google — provider is carried in the signed `state`. |
| **Webhook URL** (`MICROSOFT_CALENDAR_WEBHOOK_URL`) | `…/calendar-webhook/microsoft/` | Must be **public HTTPS reachable when a subscription is created** — Graph immediately POSTs a `validationToken` your endpoint must echo within 10 s. Use a tunnel (VS Code Dev Tunnels / ngrok) in dev. |

---

## Gotchas

- **`offline_access` is mandatory.** Omit it and there's no refresh token → the integration dies
  at first access-token expiry (~1 h) with `REAUTH_REQUIRED`.
- **Copy the secret Value, not the Secret ID**, and do it immediately — Entra hides the value
  after you leave the page.
- **Secret expiry:** client secrets expire (max ~24 months). Diarise renewal, or connections
  break at expiry. (Certificate credentials are an alternative if you want longer-lived auth.)
- **Synchronous webhook validation:** unlike Google, the notification URL is validated *during*
  subscription creation. If it isn't reachable, the watch silently fails to establish and the
  host falls back to on-demand free/busy (still correct, just not real-time).
- **No token revoke on Graph:** disconnect stops the watch and soft-deletes locally — there's no
  Microsoft revoke endpoint. This is expected.
- **Subscriptions expire in ~70 h** for `/me/events`; the hourly `renew_calendar_watches` Beat
  task renews them (shared with Google).

See the [Outlook implementation plan](../outlook-calendar-implementation-plan.md) for the full
provider design and the [Google Calendar sync flow](../flows/04-google-calendar-sync.md) (same
runtime shape, Graph terms).

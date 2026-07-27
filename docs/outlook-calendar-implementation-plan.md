# Outlook / Microsoft 365 Calendar — Implementation Plan

**Status:** ✅ IMPLEMENTED (base provider, §1–8) · 📝 §11 multi-calendar-per-host still planned
**Implemented:** `msal` dep + `MICROSOFT_CALENDAR_*` settings; `MicrosoftCalendarProvider`
(`core/services/calendars/microsoft.py`, all 9 methods) registered in the provider registry;
provider-aware `connect`/`callback` (`?provider=OUTLOOK`); `MicrosoftCalendarWebhookView` +
route; per-provider webhook-URL gating; 17 tests (`core/tests/scheduling/test_calendar_microsoft.py`),
full scheduling suite green (96 tests), **no migration** (`makemigrations --check` clean).
**Goal:** Add **Microsoft 365 / Outlook** as a second, first-class calendar provider alongside
Google, using the **Microsoft Graph API** — free/busy subtraction, event mirroring, OAuth
connect, and real-time change-notification sync — reusing the existing
`CalendarProvider` abstraction with **zero changes to the booking/availability core**.

> **Design principle:** Google is already wired through a provider-agnostic seam
> (`calendars.get_provider_for(integration)`). Microsoft is therefore **mostly a new
> provider class + OAuth/webhook wiring**, *not* a change to bookings, availability,
> reschedule, or notifications. Do not touch those systems.

---

## 0. What already exists (and does NOT change)

The following are **already provider-agnostic** and require **no changes** (verified against
the current code):

| Layer | File | Why it already works |
|-------|------|----------------------|
| Model | [core/models/calendar_integration.py](../core/models/calendar_integration.py) | `Provider.OUTLOOK` is **already in the enum** → **no migration**. Encrypted tokens, `sync_token`, `watch_*`, `watch_token` fields are all generic. |
| Sync service | [core/services/calendar_sync.py](../core/services/calendar_sync.py) | `run_incremental_sync`, `setup_watch`, `stop_watch` all call `get_provider_for()` — provider-neutral. |
| Tasks | [core/tasks.py](../core/tasks.py) | `push_booking_to_calendar`, `delete_booking_calendar_event`, `delete_calendar_event_for_host`, `refresh_calendar_tokens`, `incremental_calendar_sync`, `renew_calendar_watches` all dispatch through `get_provider_for()`. |
| Availability | `core/services/availability.py` | Reads `ExternalBusyBlock` — populated identically regardless of provider. |
| Interface | [core/services/calendars/base.py](../core/services/calendars/base.py) | The 9-method `CalendarProvider` contract Microsoft will satisfy. |

**Only four places are Google-specific and must be generalized or extended:**

1. `core/services/calendars/__init__.py` — provider registry (Google-only today).
2. `core/views/calendar_integration.py` — `connect` / `callback` hardcode `GoogleCalendarProvider` + `Provider.GOOGLE`.
3. `core/views/calendar_webhook.py` — Google's `X-Goog-*` header model (Microsoft is completely different).
4. `core/services/calendar_sync.py::setup_watch` + `core/tasks.py::establish_calendar_watch` — gate on `GOOGLE_CALENDAR_WEBHOOK_URL`.

---

## 1. Google vs. Microsoft Graph — capability map

Every row maps to a method on `CalendarProvider`. This is the spec for `microsoft.py`.

| Interface method | Google (done) | **Microsoft Graph (to build)** | Notes / gotchas |
|------------------|---------------|--------------------------------|-----------------|
| `authorization_url` / `exchange_code` | google-auth-oauthlib `Flow` | **MSAL** `ConfidentialClientApplication` auth-code flow, authority `https://login.microsoftonline.com/common` | `/common` = work **and** personal accounts. Scopes must include `offline_access` to get a refresh token. |
| `_account_email` | Calendar `primary` id | `GET /me` → `mail` or `userPrincipalName` | |
| `get_busy` | `freebusy().query` | `POST /me/calendar/getSchedule` with `schedules:[email]`, `availabilityViewInterval:15` → map `scheduleItems` where `status != free` | The true free/busy equivalent. |
| `push_event` | `events().insert` | `POST /me/events` → return `id` | Body uses `subject`, `start/end = {dateTime, timeZone:"UTC"}`. |
| `update_event` | `events().update` | `PATCH /me/events/{id}` | |
| `delete_event` | `events().delete` (ignore 404/410) | `DELETE /me/events/{id}` (ignore 404/410) | |
| `refresh_access_token` | google-auth refresh | MSAL `acquire_token_by_refresh_token` (or OAuth2 refresh grant) | On failure → set `sync_status = REAUTH_REQUIRED` (existing behaviour in `refresh_calendar_tokens`). |
| `revoke` | POST to Google revoke URI | **No per-token revoke endpoint** — local no-op (drop tokens); optionally `POST /me/revokeSignInSessions` (heavy, skip) | **Key difference — document it.** Disconnect still stops the watch + soft-deletes. |
| `setup_watch` | `events().watch` (header-based) | `POST /subscriptions` (`resource:"/me/events"`, `changeType:"created,updated,deleted"`, `clientState`, `expirationDateTime`) | **Synchronous validation handshake** — see §4. Max expiry for `/me/events` ≈ **4230 min (~70 h)**. |
| `stop_watch` | `channels().stop` | `DELETE /subscriptions/{id}` | |
| `sync_events` | `events().list` + `syncToken`, 410→full scan | `GET /me/calendarView/delta` → follow `@odata.nextLink`, store `@odata.deltaLink` as `sync_token`; `@removed` = cancelled | Store the **full deltaLink URL** in `sync_token` (TextField — fine). Send `Prefer: outlook.timezone="UTC"`. |

### The one genuinely different subsystem: the webhook

| | Google | **Microsoft** |
|--|--------|---------------|
| Validation | `X-Goog-Resource-State: sync` handshake ping (ignore) | On `POST /subscriptions`, Graph **immediately calls your `notificationUrl`** with `?validationToken=…`; you must **echo it back as `text/plain`, 200, within 10 s**, or the subscription is rejected. |
| Notification transport | HTTP **headers** (`X-Goog-Channel-ID/-Token/-Resource-State`) | HTTP **JSON body**: `{ "value": [ { subscriptionId, clientState, changeType, resourceData:{ id } } ] }` |
| Secret validation | `X-Goog-Channel-Token` == `watch_token` | `clientState` == `watch_token` |
| Endpoint | `GoogleCalendarWebhookView` | **new** `MicrosoftCalendarWebhookView` |

**Consequence:** the `notificationUrl` must be a **public HTTPS URL reachable at the moment
`setup_watch` runs** (it's validated synchronously). In local dev use a tunnel (VS Code Dev
Tunnels / ngrok). This is stricter than Google, which validates lazily.

---

## 2. Dependencies & configuration

### Phase 1a — dependencies
Add to the project deps (mirror how google-* are declared):
- `msal` — Microsoft's official auth library (auth-code exchange + refresh; handles MS tenant quirks).
- `requests` — already present; used for Graph REST calls.

> Keep **all** `msal`/`requests` imports **lazy (inside methods)**, exactly like `google.py`,
> so the module and tests load without the library reachable.

### Phase 1b — settings ([config/settings/base.py](../config/settings/base.py), beside the `GOOGLE_CALENDAR_*` block)
```python
MICROSOFT_CALENDAR_CLIENT_ID     = env.str("MICROSOFT_CALENDAR_CLIENT_ID", default="")
MICROSOFT_CALENDAR_CLIENT_SECRET = env.str("MICROSOFT_CALENDAR_CLIENT_SECRET", default="")
MICROSOFT_CALENDAR_REDIRECT_URI  = env.str("MICROSOFT_CALENDAR_REDIRECT_URI", default="")
# "common" (work + personal), "organizations" (work only), or a specific tenant id.
MICROSOFT_CALENDAR_AUTHORITY     = env.str(
    "MICROSOFT_CALENDAR_AUTHORITY", default="https://login.microsoftonline.com/common"
)
MICROSOFT_CALENDAR_WEBHOOK_URL   = env.str("MICROSOFT_CALENDAR_WEBHOOK_URL", default="")
MICROSOFT_CALENDAR_SCOPES = [
    "offline_access", "openid", "email", "profile",
    "https://graph.microsoft.com/Calendars.ReadWrite",
    "https://graph.microsoft.com/User.Read",
]
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
```

The `.env` additions are also appended to the **deployment checklist** (Phase 7).

### Phase 0 — Azure app registration (ops, no code, prerequisite)
Document in the deployment checklist:
1. **Entra ID → App registrations → New registration.** Supported account types:
   *Accounts in any org directory and personal Microsoft accounts* (multi-tenant + consumer).
2. **Redirect URI** (Web): the callback URL → `…/api/v1/core/calendar-integrations/callback/`
   (can be the **same** callback the Google flow uses — the signed `state` carries the provider).
3. **API permissions → Microsoft Graph → Delegated:** `Calendars.ReadWrite`, `offline_access`,
   `User.Read`, `openid`, `email`, `profile`. (Admin consent may be needed per customer org.)
4. **Certificates & secrets → New client secret** → record value → `MICROSOFT_CALENDAR_CLIENT_SECRET`.
5. Record **Application (client) ID** → `MICROSOFT_CALENDAR_CLIENT_ID`.
6. Ensure `MICROSOFT_CALENDAR_WEBHOOK_URL` is **public HTTPS** (webhook validation is synchronous).

---

## 3. Phase 2 — the provider (`core/services/calendars/microsoft.py`)

New file implementing `CalendarProvider` (`name = "OUTLOOK"`), all imports lazy. Skeleton:

```python
class MicrosoftCalendarProvider(CalendarProvider):
    name = "OUTLOOK"

    # --- OAuth (MSAL) --------------------------------------------------------
    @classmethod
    def _msal_app(cls): ...          # ConfidentialClientApplication(client_id, secret, authority)
    @classmethod
    def authorization_url(cls, state):
        # app.get_authorization_request_url(scopes, state=state, redirect_uri=...)
        ...
    @classmethod
    def exchange_code(cls, code):
        # app.acquire_token_by_authorization_code(...) -> OAuthTokens(...)
        # account_email via GET /me
        ...

    # --- Graph REST helpers --------------------------------------------------
    def _headers(self, integration):     # {"Authorization": f"Bearer {integration.get_access_token()}"}
    def _get(self, integration, url, **kw) / _post / _patch / _delete   # requests + 429 backoff

    # --- per-integration operations -----------------------------------------
    def get_busy(self, integration, window_start, window_end):
        # POST /me/calendar/getSchedule -> [BusyInterval for status != "free"]
    def push_event(self, integration, booking):   # POST /me/events -> id
    def update_event(self, integration, booking):  # PATCH /me/events/{external_event_id}
    def delete_event(self, integration, external_event_id):  # DELETE, ignore 404/410
    def refresh_access_token(self, integration):   # MSAL refresh -> set_tokens + token_expires_at
    def revoke(self, integration):                 # local no-op (Graph has no token-revoke)

    # --- webhook / incremental sync -----------------------------------------
    def setup_watch(self, integration, webhook_url, token):
        # POST /subscriptions -> WatchChannel(channel_id=sub.id, resource_id="/me/events",
        #                                      expiration=sub.expirationDateTime)
    def stop_watch(self, integration):             # DELETE /subscriptions/{watch_channel_id}
    def sync_events(self, integration, window_start, window_end):
        # GET deltaLink (sync_token) OR /me/calendarView/delta?startDateTime&endDateTime
        # follow @odata.nextLink; collect ChangedEvent; return SyncResult(changes, deltaLink)
```

**Implementation notes (parity with `google.py`):**
- `get_busy` → `getSchedule`: treat `status ∈ {busy, tentative, oof, workingElsewhere}` as busy;
  skip `free`. Parse `scheduleItems[].start/end.dateTime` as UTC.
- `_event_body(booking)` → `{"subject": booking.title, "start": {...UTC...}, "end": {...UTC...}}`.
- `sync_events` delta: for each item, `cancelled = "@removed" in item`; `external_id = item["id"]`;
  parse `start/end` when not removed. Persist the **full** `@odata.deltaLink` URL as the new
  `sync_token`. On a `410 Gone` / invalid deltaLink, **discard the token and restart** with a
  fresh window scan (mirror Google's 410 handling).
- Every Graph call: on `429`/`503`, honor `Retry-After` with a bounded retry (small helper).
- **Echo guard is automatic** — `run_incremental_sync` already filters `external_event_id`s that
  belong to our own bookings; no provider work needed.

### Phase 2b — register the provider ([core/services/calendars/__init__.py](../core/services/calendars/__init__.py))
```python
from core.services.calendars.microsoft import MicrosoftCalendarProvider
_PROVIDERS = {
    GoogleCalendarProvider.name: GoogleCalendarProvider,
    MicrosoftCalendarProvider.name: MicrosoftCalendarProvider,
}
```
Add `MicrosoftCalendarProvider` to `__all__`.

---

## 4. Phase 3 — OAuth wiring (make connect/callback provider-aware)

In [core/views/calendar_integration.py](../core/views/calendar_integration.py):

- **`connect`**: accept `?provider=GOOGLE|OUTLOOK` (default `GOOGLE`, back-compatible).
  Resolve the provider class via a small map, put `provider` **into the signed state**, and
  return that provider's `authorization_url(state)`.
- **`callback`**: read `provider` from the decoded state, dispatch `exchange_code` and the
  `CalendarIntegration` upsert against **that** provider value (replace the hardcoded
  `Provider.GOOGLE`). Everything else (state salt, `establish_calendar_watch.delay`, redirect)
  is unchanged. The **same callback URL serves both providers** because the provider is in the
  state.

> No new endpoint for connect/callback — just parameterize the two existing actions.

---

## 5. Phase 4 — the Microsoft webhook (the different part)

### 5a — new view `core/views/calendar_webhook.py::MicrosoftCalendarWebhookView`
```python
class MicrosoftCalendarWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        # 1) VALIDATION HANDSHAKE: Graph sends ?validationToken=... on subscription create.
        token = request.query_params.get("validationToken")
        if token:
            return HttpResponse(token, content_type="text/plain", status=200)  # echo within 10s

        # 2) NOTIFICATION: JSON body {"value":[{subscriptionId, clientState, resourceData:{id}}]}
        for note in (request.data.get("value") or []):
            integ = CalendarIntegration.objects.filter(
                watch_channel_id=note.get("subscriptionId"), is_deleted=False
            ).first()
            if integ is None:
                continue
            if integ.watch_token and integ.watch_token != note.get("clientState"):
                continue  # secret mismatch -> ignore
            incremental_calendar_sync.delay(str(integ.id))
        return Response(status=202)
```
- Reuse the **existing** `incremental_calendar_sync` task and `run_incremental_sync` — no change.
- Always `2xx` so Graph stops retrying; unknown subscription / bad `clientState` → silently skip.
- Handle validation on **both** `GET` and `POST` if you want to be defensive (Graph uses POST).

### 5b — route ([core/urls/calendar_webhook.py](../core/urls/calendar_webhook.py))
Add `path("calendar-webhook/microsoft/", MicrosoftCalendarWebhookView.as_view(), ...)`
next to the Google one.

### 5c — generalize the webhook-URL resolution
- `core/services/calendar_sync.py::setup_watch`: replace the hardcoded
  `getattr(settings, "GOOGLE_CALENDAR_WEBHOOK_URL", "")` with a **per-provider lookup**
  (e.g. a `{ "GOOGLE": GOOGLE_CALENDAR_WEBHOOK_URL, "OUTLOOK": MICROSOFT_CALENDAR_WEBHOOK_URL }`
  map keyed by `integration.provider`). Still returns `False` when that provider has no URL.
- `core/tasks.py::establish_calendar_watch`: replace the Google-only early-return gate with the
  same per-provider check (or simply delegate the gate to `setup_watch`, which already returns
  `False` cleanly).
- `renew_calendar_watches` needs **no change** — it already iterates all integrations with a
  live `watch_channel_id` and re-calls `setup_watch`.

---

## 6. Phase 5 — tasks & beat (mostly free)

- `refresh_calendar_tokens` — **no change**; it calls `provider.refresh_access_token` and already
  sets `REAUTH_REQUIRED` on failure. Confirm the MSAL refresh path sets `REAUTH_REQUIRED` when
  the refresh token is invalid/expired.
- `push_booking_to_calendar` / `delete_*` — **no change** (provider-agnostic).
- `incremental_calendar_sync` — **no change**.
- Beat schedule (`CELERY_BEAT_SCHEDULE`) — **no change**; `refresh_calendar_tokens` and
  `renew_calendar_watches` already cover both providers.

---

## 7. Phase 6 — tests (mirror the Google suite, on Postgres)

Add `core/tests/test_calendar_microsoft.py` (+ a `FakeGraph`/`responses`-style mock; keep the
provider's SDK/HTTP lazy so tests inject a fake, exactly like the Google tests). Cover:

1. **OAuth**: `connect?provider=OUTLOOK` returns a login.microsoftonline.com URL with the signed
   state; `callback` upserts a `CalendarIntegration(provider=OUTLOOK)` with encrypted tokens and
   `sync_status=CONNECTED`.
2. **get_busy** mapping: `scheduleItems` statuses → `BusyInterval`s; `free` excluded.
3. **push/update/delete_event**: correct Graph verbs/paths; `delete` swallows 404/410.
4. **refresh_access_token**: success updates token + expiry; failure → `REAUTH_REQUIRED`.
5. **Webhook validation**: `?validationToken=abc` → `200 text/plain` body `abc`.
6. **Webhook notification**: valid `clientState` enqueues `incremental_calendar_sync`; wrong
   `clientState` / unknown `subscriptionId` does **not**.
7. **Delta sync**: `@removed` deletes the `ExternalBusyBlock`; normal items upsert it;
   `@odata.deltaLink` stored as `sync_token`; **echo guard** skips our own `external_event_id`s.
8. **Watch lifecycle**: `setup_watch` stores `subscriptionId`/expiry/clientState; `renew_calendar_watches`
   picks up near-expiry Outlook integrations; `stop_watch`/disconnect clears them.

Run with the Postgres test settings (SQLite path is broken by a pre-existing ArrayField):
```
uv run python manage.py test core.tests.test_calendar_microsoft --settings=config.settings.test_postgres
```
Also run `makemigrations --check` — expected **clean** (no model change).

---

## 8. Phase 7 — docs & rollout

- [docs/apiDocs/core/calendar_integration.md](apiDocs/core/calendar_integration.md): document the
  `?provider=OUTLOOK` option on `connect`, and add a **Microsoft webhook** section
  (`/calendar-webhook/microsoft/`, validation handshake, JSON body, `clientState`).
- [docs/flows/04-google-calendar-sync.md](flows/04-google-calendar-sync.md): add an Outlook lane
  (or a sibling note) — same flow, Graph terms.
- [docs/scheduling-deployment-checklist.md](scheduling-deployment-checklist.md): add the Azure
  app-registration steps and the `MICROSOFT_CALENDAR_*` env-var rows.
- [core/models/calendar_integration.py](../core/models/calendar_integration.py): update the
  `OUTLOOK = "OUTLOOK", "Outlook"  # reserved; not implemented yet` comment → implemented; and
  the `watch_token` help_text (currently Google-specific) to mention Graph `clientState` too.

---

## 9. Cross-cutting gotchas (call these out during implementation)

1. **Synchronous webhook validation** — `notificationUrl` must be public HTTPS **when
   `setup_watch` runs**. Locally, run a tunnel; otherwise the subscription creation fails and the
   host silently has no real-time sync (on-demand free/busy still works, so it degrades safely).
2. **No token revoke on Graph** — `revoke()` is a local no-op; disconnect still stops the watch
   and soft-deletes. Don't block disconnect on a revoke call.
3. **`offline_access` scope is mandatory** — without it there's no refresh token and the
   integration dies at first token expiry.
4. **Subscription expiry ≈ 70 h** for `/me/events` — shorter than Google; the existing
   `renew_calendar_watches` beat (already scheduled) handles it, but verify the renewal window is
   shorter than 70 h.
5. **`sync_token` holds a full URL** for Outlook (the deltaLink) vs an opaque token for Google —
   the `TextField` accommodates both; just never assume a short token.
6. **Throttling (`429`)** — wrap Graph calls with a bounded `Retry-After` backoff; don't let a
   transient 429 mark the integration `ERROR`.
7. **Personal (outlook.com) vs work accounts** — `/common` authority supports both; verify
   change-notification subscriptions on consumer accounts during testing (work accounts are the
   primary target).
8. **Timezone discipline** — always send/parse Graph `dateTime` as UTC (`timeZone:"UTC"`, and
   `Prefer: outlook.timezone="UTC"` on delta reads) so `ExternalBusyBlock` rows stay UTC-aware.

---

## 10. Definition of done

- [ ] `MicrosoftCalendarProvider` implements all 9 `CalendarProvider` methods.
- [ ] Registered in the provider registry; `Provider.OUTLOOK` comment flipped to implemented.
- [ ] `connect?provider=OUTLOOK` → consent → `callback` creates a working integration.
- [ ] Booking creates/updates/deletes mirror to Outlook; host busy times subtract from availability.
- [ ] Microsoft webhook validates + drives incremental delta sync (echo-guarded).
- [ ] Token refresh + watch renewal run under the existing beat tasks.
- [ ] New test module green on Postgres; `makemigrations --check` clean; full core suite still green.
- [ ] Docs + deployment checklist updated; Azure app-registration steps recorded.

### Effort estimate
| Phase | Scope | Rough size |
|-------|-------|-----------|
| 0–1 | Azure reg + settings/deps | ~½ day (mostly ops) |
| 2 | `microsoft.py` provider | ~1–1.5 days |
| 3 | connect/callback provider-aware | ~½ day |
| 4 | Microsoft webhook + URL generalization | ~1 day |
| 5 | task/beat generalization | ~¼ day |
| 6 | tests | ~1 day |
| 7 | docs | ~½ day |

> No booking/availability/reschedule/notification code is touched — the blast radius is the
> calendar subsystem only.

---

## 11. Multi-calendar per host (enhancement — do **after** the base provider)

> **Motivation:** a single host may legitimately run **two calendars at once** (e.g. work
> Google + personal Outlook). Both must be honored: their busy times subtracted from
> availability, and our bookings reflected onto them. This is a **deliberate follow-on** — it
> improves **both** Google and Outlook, and unlike §1–8 it **does require a migration**.

### 11.1 Non-negotiable invariant: our DB is the source of truth

The whole design hangs on one rule — **truth flows one way, DB → calendars:**

1. **`Booking` (our DB) is the sole authority** for what is scheduled. External calendars are
   **downstream mirrors**, never authoritative.
2. **`BookingCalendarMirror`** (new) records *where* each booking has been reflected
   (which calendar, which external event id). It is **derived state** — fully rebuildable by
   re-pushing from `Booking`.
3. **`ExternalBusyBlock`** stays what it is: a **read-only cache of external, user-created
   events** (things the host put on their calendar *outside* our system), used **only** to
   subtract availability. It is **never** treated as a source of bookings.
4. **Inbound sync can only ever write to `ExternalBusyBlock`**, and is **echo-guarded** so our
   own pushed events never round-trip back in and masquerade as external busy.
5. If a mirrored event is **edited or deleted directly on the external calendar**, we do **not**
   mutate the `Booking`. A **reconciliation pass re-asserts the DB state** (re-push). **DB wins,
   always.**

### 11.2 Current limitation (recap)

Both hot paths pick exactly one integration and ignore the rest:
- Availability read — `_external_busy_intervals` → `.filter(...).first()` ([availability.py:103-108](../core/services/availability.py#L103-L108)).
- Event mirror — `_connected_integration` → `.filter(...).first()` ([tasks.py:89-97](../core/tasks.py#L89-L97)).

And `Booking.external_event_id` / `external_provider` are **single-valued**, so a booking can
only remember *one* mirror. Two independent halves must change: **READ (union)** and
**WRITE (fan-out)**.

### 11.3 Data-model changes (the migration)

**a. New model `BookingCalendarMirror` (Tier 1 `TenantBaseModelCore`, like `ExternalBusyBlock`)**

| Field | Purpose |
|-------|---------|
| `booking` (FK → Booking, `related_name="calendar_mirrors"`) | The authoritative booking being mirrored |
| `integration` (FK → CalendarIntegration) | Which calendar it was pushed to |
| `external_event_id` (Char, indexed) | The provider event id on *that* calendar |
| `provider` (Char) | Denormalized `integration.provider` for fast echo-guard lookups |
| `status` (Char: `SYNCED` / `PENDING` / `FAILED`) | Per-calendar mirror health |
| **Constraint** | `UniqueConstraint(booking, integration)` — one mirror row per (booking, calendar) |

> This **supersedes** `Booking.external_event_id` / `external_provider` as the mirror record.
> Keep the two `Booking` columns as a **denormalized pointer to the *primary* mirror** for
> backward-compatibility (or drop them in a later cleanup) — but the mirror **table** is
> authoritative.

**b. `CalendarIntegration` gains two flags**

| Field | Purpose |
|-------|---------|
| `is_primary` (bool) | The host's default write target. **Partial unique**: `UniqueConstraint(tenant, user, condition=Q(is_primary=True, is_deleted=False))` → at most one primary per host. First calendar connected becomes primary. |
| `mirror_events` (bool, default `True`) | Whether this calendar **receives** booking copies. `False` = **busy-read-only** (subtract its busy times, but don't clutter it with our events). |

**Push policy (recommended default):** *read busy from **all** connected calendars; mirror
events to **every** calendar with `mirror_events=True`.* A host who wants their personal Outlook
to only *block* time (not show work bookings) simply sets `mirror_events=False` on it.

> ⚠️ **This is the migration** the base plan avoided. Adding `BookingCalendarMirror` + the two
> flags is one migration (e.g. `00XX_multicalendar`). Data migration: seed one
> `BookingCalendarMirror` per existing `Booking` that has a non-empty `external_event_id`, and
> set `is_primary=True` on each host's current single integration.

### 11.4 Read path — union busy across all calendars

Rework `_external_busy_intervals(tenant, host_id, window_start, window_end)` to:

1. Fetch **all** `CONNECTED`, non-deleted integrations for the host (not `.first()`).
2. For each: if a **watch is live**, read its `ExternalBusyBlock` rows; else do an **on-demand
   `get_busy`** (still cached per-integration under the existing `freebusy:{integration.id}:…`
   key). Failures on one calendar degrade to `[]` for *that* calendar only — never block booking.
3. **Union** all intervals and return. (The `(tenant, user, start, end)` index on
   `ExternalBusyBlock` already supports a single cross-integration query for the watch-live set.)

No storage change here — `ExternalBusyBlock` is *already* per-integration.

### 11.5 Write path — fan-out mirroring

- `push_booking_to_calendar`: replace the single `_connected_integration` with **all**
  `mirror_events=True` integrations for the host. For each, upsert a `BookingCalendarMirror`:
  `PATCH` if a row with an `external_event_id` exists, else `POST` and store the returned id +
  `status=SYNCED` (mark `FAILED` on error, and let the task retry that calendar independently).
- `delete_booking_calendar_event` / reassignment delete: iterate the booking's
  `BookingCalendarMirror` rows and `delete_event` on each, then delete the mirror rows.
- Reassignment (host change): delete mirrors on the **old** host's calendars, create on the
  **new** host's — the existing `delete_calendar_event_for_host` pattern generalizes to
  per-mirror.

### 11.6 Echo guard — make it per-calendar-correct

Today `_our_event_ids(integration)` reads `Booking.external_event_id` for the host
([calendar_sync.py:20-29](../core/services/calendar_sync.py#L20-L29)). With fan-out, change it to
read **`BookingCalendarMirror.external_event_id` filtered by that `integration`** — so sync of
calendar A skips exactly the events *we* created on calendar A (not a global set). This keeps our
mirrored events out of `ExternalBusyBlock` on every calendar independently.

### 11.7 Reconciliation task (keeps DB the source of truth)

Add a periodic (beat) `reconcile_calendar_mirrors` task:
- For active bookings, ensure a `SYNCED` mirror exists on each `mirror_events` calendar of the
  current host — **re-push** any missing/`FAILED`/drifted mirror (DB → calendar).
- Optionally detect mirrors whose external event was deleted on the calendar and **re-create**
  them (never the reverse). This is what enforces "DB wins" over time.

### 11.8 Tests (add to the multi-calendar suite)

1. Host with Google **and** Outlook both `CONNECTED` → availability subtracts **union** of both
   calendars' busy.
2. New booking fans out → a `BookingCalendarMirror` per `mirror_events` calendar, each with its
   own `external_event_id`.
3. `mirror_events=False` calendar → **not** written to, but its busy **is** subtracted.
4. Cancel/reassign → mirrors removed from the right calendars; DB booking unchanged as truth.
5. Echo guard per calendar → our event on A doesn't reappear as busy after A's sync.
6. `is_primary` partial-unique enforced (can't have two primaries per host).
7. Reconciliation re-pushes a mirror deleted directly on the calendar (DB wins).

### 11.9 Sequencing

> Ship **§1–8 (base Outlook) first** — it needs no migration and delivers the mixed-fleet case
> (different hosts on different providers), which is the common real-world need. Then do **§11**
> as a focused, migration-bearing enhancement that upgrades *both* providers to true
> multi-calendar-per-host. Estimated ~2–3 days (model + migration + read/write rework +
> reconciliation + tests).

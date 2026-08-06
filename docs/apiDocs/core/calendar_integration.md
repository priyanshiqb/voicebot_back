# Calendar Integration API Documentation

Connect a host's **Google Calendar** via OAuth, scoped to the authenticated tenant. Once
connected, the host's busy times are subtracted from availability and new bookings are mirrored
to their calendar. **Token fields are never exposed** by the API.

One integration per host: the unique constraint is `(tenant, user)`, so reconnecting updates the
existing record rather than adding a second one.

## Endpoints Summary
- `GET /api/v1/core/calendar-integrations/connect/` - Start OAuth (returns consent URL)
- `GET /api/v1/core/calendar-integrations/callback/` - OAuth redirect target (system; provider carried in signed state)
- `GET /api/v1/core/calendar-integrations/` - List integrations (paginated)
- `GET /api/v1/core/calendar-integrations/{id}/` - Retrieve an integration
- `DELETE /api/v1/core/calendar-integrations/{id}/` - Disconnect (revoke + stop watch)
- `GET /api/v1/core/calendar-integrations/dropdown/` - Get label/value list (label = account email)
- `POST /api/v1/core/calendar-webhook/google/` - Google push notifications (system, public)

---

## Endpoint Details

### 1. Start Connect (OAuth)

**`GET /api/v1/core/calendar-integrations/connect/`**

Step one of two. Returns the provider's consent URL; **it does not connect anything yet** — no
integration row exists until the host approves and the callback below runs.

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `provider` | enum | Which calendar to connect. | Optional, default `GOOGLE` — currently the only supported value. Anything else is a **400**. |

```json
{
  "status": true,
  "message": "Authorization URL generated",
  "data": { "authorization_url": "https://accounts.google.com/o/oauth2/auth?client_id=…&state=…" },
  "errors": null
}
```

**Flow:** redirect the host's browser to `authorization_url`. After they consent, Google redirects
to the callback below, which finishes the job and bounces them back to your front-end.

| Status | When | `message` |
|---|---|---|
| **200** | URL generated. | `Authorization URL generated` |
| **400** | Unsupported `provider`. `OUTLOOK` has its own message. | `Outlook is no longer supported. Only GOOGLE is available.` |
| **400** | The provider SDK could not build a URL — usually missing client id/secret. | `Failed to start calendar connect.` |
| **401** | Missing or invalid token. | — |
| **500** | `SCHEDULING_TOKEN_ENCRYPTION_KEY` is not configured on the server. | `Missing SCHEDULING_TOKEN_ENCRYPTION_KEY config.` |

> The **500** is a deployment error, not a caller error: without that key, OAuth tokens could not
> be encrypted at rest, so the flow refuses to start rather than storing them in the clear. See
> the [deployment checklist](../../scheduling-deployment-checklist.md).

**The `state` parameter is security-critical.** It is signed, carries the user, tenant, and
provider, and embeds a single-use nonce cached server-side. That makes the callback safe to expose
without a bearer token, and stops a leaked callback URL being replayed.

---

### 2. OAuth Callback (system)

**`GET /api/v1/core/calendar-integrations/callback/?code=…&state=…`**

Google's redirect target. **Your code never calls this** — a browser does, after consent.

- **Auth**: none. Trust comes from the signed `state` minted at connect.
- On success: exchanges the code, stores **encrypted** tokens, sets `sync_status = CONNECTED`,
  and establishes a push watch if `GOOGLE_CALENDAR_WEBHOOK_URL` is configured.
- Then **redirects the browser** to `{FRONTEND_PROJECT_URL}/integrations` — so the host lands back
  in your app, not on a JSON page. If `FRONTEND_PROJECT_URL` is unset it returns
  `200 "Calendar connected successfully."` instead.

| Status | When | `message` |
|---|---|---|
| **302** | Connected; redirecting to the front-end. | — |
| **200** | Connected, but no `FRONTEND_PROJECT_URL` configured. | `Calendar connected successfully.` |
| **400** | `code` or `state` missing from the redirect. | `Missing code or state.` |
| **400** | `state` signature invalid or older than its max age. | `Invalid or expired state.` |
| **400** | `state` nonce already consumed — a replay, or a double-submitted callback. | `State token already used or invalid.` |
| **400** | The tenant in the state no longer exists. | `Unknown tenant.` |
| **400** | Token exchange with the provider failed — expired code, revoked consent, or a scope mismatch. | `Failed to connect calendar.` |
| **500** | Encryption key not configured. | `Missing SCHEDULING_TOKEN_ENCRYPTION_KEY config.` |

> The errors are deliberately terse and do not echo the provider's response, since this endpoint is
> unauthenticated. When a host reports a failed connect, check the server logs — the underlying
> cause is logged with a stack trace.
>
> **Reconnecting is safe.** One integration per `(tenant, user)`, so running the flow again updates
> the existing row rather than creating a duplicate. This is the fix for `REAUTH_REQUIRED`.

---

### 3. List / Retrieve Integrations

**`GET /api/v1/core/calendar-integrations/`** · **`GET /api/v1/core/calendar-integrations/{id}/`**

Read-only. **There is no create or update endpoint** — integrations are born from the OAuth flow
and only ever removed. `POST`/`PUT`/`PATCH` return **405**.

| Parameter | Type | Description |
|---|---|---|
| `page`, `page_size` | integer | Pagination (list only). |
| `user` | UUID | Only this host's integration. |
| `provider` | enum | Currently only `GOOGLE`. |
| `sync_status` | enum | `CONNECTED`, `REAUTH_REQUIRED`, `ERROR`, `DISCONNECTED`. Filtering on the first two is how you build a "needs attention" view. |

**Token fields are never serialized.** `access_token`, `refresh_token`, and the encryption details
are absent from every response by design — there is no endpoint that returns them. The whole
serializer is read-only, so even a field that *is* returned cannot be written back.

`user` is returned as a **bare UUID**, not an expanded object — unlike most resources here. Fetch
the user separately if you need their name or email.

| Field | Meaning |
|---|---|
| `account_email` | The Google account that was connected. Show this so a host can tell *which* of their accounts is linked. |
| `sync_status` | See the table below — the field worth surfacing prominently. |
| `token_expires_at` | When the access token lapses. Refreshed automatically every 15 minutes by a background task; not something a client acts on. |
| `watch_expires_at` | When the push subscription lapses (~7 days). Renewed hourly by a background task. `null` means webhooks are not configured, and free/busy is fetched on demand instead. |
| `last_synced_at` | Last successful incremental sync. A stale value alongside `CONNECTED` suggests the watch has lapsed. |
| `last_error` | Most recent provider error, truncated. Diagnostic history — **not** cleared on recovery. |

- **Success Response (200 OK)** (retrieve):
  ```json
  {
    "status": true,
    "message": "Retrieved successfully",
    "data": {
      "id": "c1aa…-integration",
      "tenant": { "id": "7bf2…", "name": "Alpha Corp", "subdomain": "alpha-corp" },
      "user": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
      "provider": "GOOGLE",
      "account_email": "host@gmail.com",
      "sync_status": "CONNECTED",
      "token_expires_at": "2026-07-05T15:00:00Z",
      "watch_expires_at": "2026-07-12T00:00:00Z",
      "last_synced_at": "2026-07-05T14:00:00Z",
      "last_error": "",
      "is_active": true,
      "created_by": { "id": "3fa8...", "name": "Emma" },
      "updated_by": null,
      "deleted_by": null,
      "created_at": "2026-07-01T10:00:00Z",
      "updated_at": "2026-07-05T14:00:00Z",
      "deleted_at": null
    },
    "errors": null
  }
  ```
#### `sync_status` — what each value means and what to do

This is the field a UI should surface: it is how a host learns their calendar has stopped working.

| Value | Meaning | What the host must do | Effect on availability |
|---|---|---|---|
| `CONNECTED` | Healthy. Free/busy is read and bookings are mirrored. | nothing | External busy times are subtracted from slots. |
| `REAUTH_REQUIRED` | The provider rejected our credentials — access revoked, password changed, or the app's access withdrawn in the Google account settings. **Not retried**; retrying a revoked token never succeeds. | **Reconnect** via `connect/`. | The integration is skipped entirely, so the host's slots stop reflecting their external calendar. |
| `ERROR` | The provider refused a request for a reason that will not fix itself — a malformed calendar, a permissions change, a deleted calendar. `last_error` carries the provider's message. | Inspect `last_error`; often reconnecting or fixing the calendar in Google resolves it. | Same as above. |
| `DISCONNECTED` | Deliberately removed by the host. | nothing | The host is treated as having no external calendar. |

**How a status gets set.** Provider failures are classified rather than blindly retried:

| Provider response | Classified as | Result |
|---|---|---|
| `401`, `403` | permanent — credentials are gone | `REAUTH_REQUIRED`, no retry |
| `404`, `410` | permanent — the remote event no longer exists | the stored event link is cleared so the next push **re-creates** the event; the integration stays `CONNECTED` |
| other `4xx` | permanent — the request itself is wrong | `ERROR` with `last_error` |
| `408`, `429` | transient — timeout or rate limit | retried with backoff; status unchanged |
| `5xx`, network errors | transient | retried with backoff; status unchanged |

The `404` case is worth calling out: it used to be retried three times and then abandoned, leaving
a stored pointer to a deleted event so **every later update failed forever**. Now the pointer is
cleared and the event is simply re-created.

**`last_error`** holds the most recent provider message (truncated to 500 chars). It is not
cleared automatically on recovery, so treat it as diagnostic history rather than current state —
`sync_status` is the authoritative signal.

> **Availability degrades open, and says so.** If a provider is unreachable while slots are being
> computed, the slot is still offered but flagged `external_check_failed: true` — an outage should
> not empty a calendar. Attempting to *book* such a slot returns **503** rather than risking a
> double-booking. See [Available Slots](./availability_slots.md).

#### Dropdown
- **Path**: `GET /api/v1/core/calendar-integrations/dropdown/` — `{label, value}` pairs where
  `label` is the connected `account_email`. Supports `?limit` (≤ 1000) + the List filters
  (`user`, `provider`, `sync_status`).
- **Note**: `bulk_delete` is **not** available on this resource (POST is disabled → `405`);
  remove an integration with `DELETE /{id}/`.

---

### 4. Disconnect

**`DELETE /api/v1/core/calendar-integrations/{id}/`**

Unlinks the host's calendar. Four things happen, in order:

1. the push watch is stopped, so Google stops sending change notifications;
2. the OAuth grant is revoked at Google, so our tokens stop working immediately;
3. `sync_status` is set to `DISCONNECTED`;
4. the row is soft-deleted.

**Steps 1 and 2 are best-effort.** If Google is unreachable or the grant was already revoked from
the Google account page, those failures are logged and the disconnect proceeds — a provider outage
must not leave a host unable to disconnect. The local state is always correct afterwards.

| Status | When | `message` |
|---|---|---|
| **200** | Disconnected. | `Deleted successfully` |
| **404** | No such integration in your tenant. | — |

**Effect on scheduling:** the host reverts to non-calendar-aware immediately. Their external busy
times stop being subtracted from availability, so slots they are privately busy for **will** be
offered. Bookings already mirrored to their Google calendar are **left in place** — disconnecting
does not clean up past events.

To reconnect, run the [connect flow](#1-start-connect-oauth) again; it creates a fresh integration
for the same `(tenant, user)` pair.

---

### 5. Google Webhook (system, public)

**`POST /api/v1/core/calendar-webhook/google/`**

Google calls this when a connected calendar changes. **Nothing in your application should ever
call it** — it is documented so the endpoint is not mistaken for an unprotected write.

- **Auth**: none, by necessity — Google's servers hold no bearer token. Trust comes from the
  per-channel `X-Goog-Channel-Token`, a secret minted when the watch was created and known only to
  us and Google.

| Header (sent by Google) | Purpose |
|---|---|
| `X-Goog-Channel-ID` | Which watch subscription fired. |
| `X-Goog-Channel-Token` | The shared secret validated against the stored integration. |
| `X-Goog-Resource-ID` | The calendar resource that changed. |
| `X-Goog-Resource-State` | `sync` for the initial handshake, otherwise a change notification. |

**Behaviour:** on a valid non-handshake notification, an incremental sync is queued that refreshes
the host's cached busy blocks. Events *we* created are ignored — without that echo guard, mirroring
a booking to Google would bounce back as a change and re-trigger the sync in a loop.

**Always responds `200`, even for unknown or invalid channels.** That is deliberate: Google retries
non-2xx responses with escalating backoff, so returning `403` for a stale channel (from an
integration disconnected weeks ago) would generate retry traffic forever. Invalid notifications are
logged and dropped.

| Status | When |
|---|---|
| **200** | Always — valid notification processed, or invalid one silently ignored. |

If webhooks are not configured (`GOOGLE_CALENDAR_WEBHOOK_URL` blank), no watch is ever created and
this endpoint simply never fires. Free/busy is then fetched on demand at slot-generation time
instead, cached briefly — correct, but slower and heavier on the provider's quota.

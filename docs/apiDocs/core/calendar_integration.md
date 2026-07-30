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
- **Path**: `GET /api/v1/core/calendar-integrations/connect/`
- **Headers**: `Authorization: Bearer <token>`
- **Query Parameters** (optional):
  - `provider`: `GOOGLE` (the default, and currently the only supported value). Any other value
    returns `400`; `OUTLOOK` returns `400` with `"Outlook is no longer supported. Only GOOGLE is
    available."` The provider is embedded in the signed OAuth `state`, so the single callback
    endpoint below serves any provider added later.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Authorization URL generated",
    "data": { "authorization_url": "https://accounts.google.com/o/oauth2/auth?client_id=...&state=..." },
    "errors": null
  }
  ```
- **Flow**: The front-end redirects the host to `authorization_url`. After consent, the
  provider redirects to the callback below. See the
  [Google Calendar sync flow](../../flows/04-google-calendar-sync.md).

---

### 2. OAuth Callback (system)
- **Path**: `GET /api/v1/core/calendar-integrations/callback/?code=<code>&state=<signed-state>`
- **Auth**: None — trusted via the signed `state` minted at connect (no bearer token).
- **Behaviour**: Exchanges the code, stores encrypted tokens, sets `sync_status = CONNECTED`,
  establishes a push watch (if `GOOGLE_CALENDAR_WEBHOOK_URL` is configured), then redirects
  the browser back to the front-end (`{FRONTEND_PROJECT_URL}/settings/calendar?connected=1`).
- **Error Response (400 Bad Request)**: invalid/expired `state`, or the code exchange failed.

---

### 3. List / Retrieve Integrations
- **Path**: `GET /api/v1/core/calendar-integrations/` — Query filters: `user`, `provider`, `sync_status`.
- **Success Response (200 OK)** (retrieve):
  ```json
  {
    "status": true,
    "message": "Retrieved successfully",
    "data": {
      "id": "c1aa...-integration",
      "tenant": { "id": "7bf2...", "name": "Alpha Corp" },
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
- **`sync_status` values**: `CONNECTED`, `REAUTH_REQUIRED` (token refresh failed → host must
  reconnect), `ERROR`, `DISCONNECTED`.

#### Dropdown
- **Path**: `GET /api/v1/core/calendar-integrations/dropdown/` — `{label, value}` pairs where
  `label` is the connected `account_email`. Supports `?limit` (≤ 1000) + the List filters
  (`user`, `provider`, `sync_status`).
- **Note**: `bulk_delete` is **not** available on this resource (POST is disabled → `405`);
  remove an integration with `DELETE /{id}/`.

---

### 4. Disconnect
- **Path**: `DELETE /api/v1/core/calendar-integrations/{id}/`
- **Behaviour**: Stops the push watch, best-effort revokes the Google grant, and soft-deletes
  the integration. Availability reverts to non-calendar-aware for that host.
- **Success Response (200 OK)**: `{ "status": true, "message": "Deleted successfully", "data": null, "errors": null }`

---

### 5. Google Webhook (system, public)
- **Path**: `POST /api/v1/core/calendar-webhook/google/`
- **Auth**: None — Google's browser-less callback; validated against the per-channel
  `X-Goog-Channel-Token` set when the watch was created.
- **Headers (sent by Google)**: `X-Goog-Channel-ID`, `X-Goog-Channel-Token`,
  `X-Goog-Resource-ID`, `X-Goog-Resource-State`.
- **Behaviour**: On a valid non-handshake notification, enqueues an incremental sync that
  refreshes the host's cached busy blocks (ignoring events we created — echo guard). Always
  responds `200` (unknown/invalid channels are ignored) so Google stops retrying.
- **Response**: `200 OK` (empty body).

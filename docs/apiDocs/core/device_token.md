# Device Token API Documentation

Registers a device's **FCM registration token** so your team can receive push notifications —
internal-team alerts such as "a booking needs to be rescheduled". This is staff-facing plumbing,
not a customer feature: the tokens belong to the people using your dashboard or mobile app.

Three things shape every endpoint on this page:

- **You only ever see your own tokens.** The list, dropdown, retrieve, delete and bulk-delete are
  all filtered to the authenticated user. There is no way for one user to enumerate or delete
  another's devices. (Django superusers are the exception and see everything.)
- **Registration is an upsert, not an insert.** Re-posting a token you already registered
  reactivates it and refreshes its metadata instead of failing on the unique constraint. Clients
  are expected to POST on every app start — that is the intended usage, not a wasteful one.
- **Push is best-effort and silent.** If Firebase is not configured on the server, or a token has
  gone stale, sends are skipped without surfacing an error to anyone. Registration succeeding tells
  you the token was *stored*, never that a notification will actually arrive.

> ⏳ **Re-registering is not optional — it is how a device stays reachable.** A token with no
> registration inside `DEVICE_TOKEN_STALE_DAYS` (default **60 days**) stops receiving pushes and is
> deactivated by a nightly sweep, then deleted after `DEVICE_TOKEN_RETENTION_DAYS` (default **90**).
> The token itself may still be perfectly valid at Google; we stop trusting it because the client
> went quiet. On **web there is no rotation callback at all** (the JS SDK v9+ removed
> `onTokenRefresh`), so a client that skips the app-start POST goes dark permanently with no error
> anywhere. See [FCM End-to-End](../../flows/push-notifications.md).

> **VB_AGENT access:** none. `vb_agent_allowed_actions` is empty for this viewset, so every action
> returns **403 `This endpoint is not available to agent tokens.`** Device tokens belong to human
> operators; a machine caller has no device.
> See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

## Endpoints Summary
- `GET /api/v1/core/device-tokens/` - List your registered devices (paginated)
- `GET /api/v1/core/device-tokens/{id}/` - Retrieve one of your devices
- `POST /api/v1/core/device-tokens/` - Register (upsert) a device token
- `POST /api/v1/core/device-tokens/deactivate/` - Stop pushes to one token, keep the row
- `POST /api/v1/core/device-tokens/test/` - Send yourself a test push and report what happened
- `DELETE /api/v1/core/device-tokens/{id}/` - Soft-delete a device token
- `GET /api/v1/core/device-tokens/dropdown/` - Get label/value list (your tokens)
- `POST /api/v1/core/device-tokens/bulk_delete/` - Bulk soft-delete your tokens

**`PUT` and `PATCH` are not routed** — the viewset allows only `GET`, `POST`, `DELETE`. Attempting
either returns **405 Method Not Allowed**. To change a device's platform, re-register it (§2); to
turn it off, use `deactivate` (§4) rather than an update.

---

## Endpoint Details

### 1. List Device Tokens

Every device the authenticated user has registered in the current tenant, newest first. Useful for
a "your signed-in devices" screen, and for spotting stale entries — a token with
`is_active: false` was either deactivated deliberately or rejected by Firebase.

- **Path**: `GET /api/v1/core/device-tokens/`
- **Headers**: `Authorization: Bearer <token>`
- **Query Parameters**:

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `page` | integer | Page number. Default `1`. | Out-of-range page → **404**. |
| `page_size` | integer | Rows per page. Default `10`. | Capped at `100`; larger values are clamped, not rejected. |
| `platform` | string | Filter by device platform. | One of `ANDROID`, `IOS`, `WEB`. An unknown value → **400**. |
| `is_active` | boolean | Filter live vs. deactivated tokens. | `true` / `false`. |
| `ordering` | string | Sort field. Default `-created_at`. | Only `created_at` and `updated_at` (optionally `-` prefixed) are permitted; anything else is ignored. |

> **There is no `?search=`.** No search fields are configured for this resource, so a `search`
> parameter is accepted by the URL and silently does nothing. Filter by `platform` instead.

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "d1aa85f64-5717-4562-b3fc-2c963f66afb2",
        "user": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
        "platform": "WEB",
        "is_active": true,
        "last_seen_at": "2026-07-05T14:00:00Z",
        "last_error": "",
        "created_at": "2026-07-01T10:00:00Z"
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

> **The list view deliberately omits `token`.** The raw FCM string is only ever returned by the
> register call (§2) that produced it, and by retrieve (§3). Do not build a UI that expects
> `token` in the list payload.

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more rows. An empty `data` array with `count: 0` is the normal response for a user who has never registered a device — not an error. | `success` |
| **400** | Unknown `platform` value, or a non-boolean `is_active`. | filter error |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | Your user has no active tenant mapping. | `Your user account is not associated with any active tenant.` |
| **403** | Agent (`VB_AGENT`) token. | `This endpoint is not available to agent tokens.` |
| **404** | `page` beyond the last page. | `Invalid page.` |

---

### 2. Register Device Token (Upsert)

Called by the client app whenever Firebase hands it a registration token — on first launch, after a
token refresh, and on every subsequent start. **Safe to call repeatedly**; that is the design.

- **Path**: `POST /api/v1/core/device-tokens/`
- **Headers**: `Authorization: Bearer <token>`
- **Request Payload**:
  ```json
  { "token": "fcm-registration-token-abc123", "platform": "WEB" }
  ```

### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `token` * | string | The FCM registration token issued to this device by the Firebase SDK. Opaque to this API — it is stored and handed to Firebase verbatim. | Required and non-blank; a value that is empty or only whitespace → **400 `token is required.`** Surrounding whitespace is stripped before storing. Max **512** characters. |
| `platform` | string | Which client produced the token. Determines nothing about delivery today; it exists so you can tell devices apart in the list and target a platform later. | Optional, default `WEB`. One of `ANDROID`, `IOS`, `WEB`. Case-sensitive — `"web"` is **400**. |

**Everything else is server-assigned and ignored if you send it:** `id`, `tenant`, `user`,
`is_active`, `last_seen_at`, `created_at`, `updated_at`, `deleted_at`. In particular you cannot
register a token *on behalf of* another user — the owner is always the authenticated caller.

### What "upsert" actually means

The server looks for a non-deleted token with the **same string in the same tenant**. If it finds
one, that row is updated rather than a new one created:

| Field | On re-registration |
|---|---|
| `user` | **Reassigned to the caller.** |
| `platform` | Overwritten with the submitted value (or reset to `WEB` if you omit it). |
| `is_active` | Forced back to `true` — this is how a device that Firebase previously rejected comes back to life. |
| `last_seen_at` | Set to now. **This is what resets the staleness clock.** |
| `last_error` | Cleared. |
| `failure_count` | Reset to `0` (internal; not returned). |
| `id`, `created_at` | Unchanged — the original row is kept. |

> **Registration is the self-heal path, so it clears the send-side verdict too.** A token that
> accumulated failures months ago starts clean; otherwise one unrelated failure after the client
> came back healthy would be enough to retire it.

### Limits on registration

| Limit | Value | Behaviour when exceeded |
|---|---|---|
| **Rate** | `DEVICE_TOKEN_REGISTER_RATE`, default `60/hour` per user | **429 Too Many Requests.** Applies to this call only — reading your device list is not throttled |
| **Live devices per user** | `DEVICE_TOKEN_MAX_PER_USER`, default `20` per tenant | Silently succeeds; the **oldest** live tokens beyond the cap are deactivated. The device you just registered is never the one retired |

Neither is something a normal client hits. A human has a handful of devices, and 60 app starts
per hour is not a real usage pattern — both exist to bound a looping or compromised client.

> **Re-registration can transfer ownership between users.** The lookup is scoped to the *tenant*,
> not to the caller, so if user A registered a token and user B later registers the same string,
> the row moves to B and A stops receiving pushes on it. This is intentional: it is what should
> happen on a shared browser or a re-issued company handset. It also means a **201 does not
> guarantee a new device was added** to your own list.

> **`last_seen_at` is "last registered at", not "last active".** Nothing else in the system
> updates it — not opening the app, not receiving a push. It moves only when this endpoint is
> called. Treat it as a staleness hint, not a presence indicator.

- **Success Response (201 Created)**:
  ```json
  {
    "status": true,
    "message": "Device registered",
    "data": {
      "id": "d1aa85f64-5717-4562-b3fc-2c963f66afb2",
      "tenant": { "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c", "name": "Alpha Corp", "subdomain": "alpha-corp" },
      "user": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
      "token": "fcm-registration-token-abc123",
      "platform": "WEB",
      "is_active": true,
      "last_seen_at": "2026-07-05T14:00:00Z",
      "last_error": "",
      "created_at": "2026-07-05T14:00:00Z",
      "updated_at": "2026-07-05T14:00:00Z",
      "deleted_at": null
    },
    "errors": null
  }
  ```

> **Always `201`, even when nothing was created.** The update branch returns `201 Device
> registered` exactly like the insert branch. Do not use the status code to decide whether this was
> a new device — compare `created_at` against `updated_at`, or check whether the `id` is one you
> already knew.

> **`tenant` reads back as an object, `user` as a bare UUID.** They are different field types, not
> an inconsistency to code around: `tenant` is expanded to `{id, name}`, `user` stays a plain id.

### Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **201** | Token stored (new row) or refreshed (existing row reactivated). | `Device registered` | `null` |
| **400** | `token` missing, empty, or whitespace-only. | `token is required.` | `{"token": [...]}` |
| **400** | `token` longer than 512 characters. | max-length message | `{"token": [...]}` |
| **400** | `platform` not one of the three choices. | `"x" is not a valid choice.` | `{"platform": [...]}` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": ...}` |
| **403** | Your user has no active tenant mapping. | `Your user account is not associated with any active tenant.` | `{"detail": ...}` |
| **403** | Agent (`VB_AGENT`) token. | `This endpoint is not available to agent tokens.` | `{"detail": ...}` |
| **429** | More than `DEVICE_TOKEN_REGISTER_RATE` registrations in the window. | throttle message with `Retry-After` | `{"detail": ...}` |

> **Safe under concurrency.** `(tenant, token)` is unique among non-deleted rows at the database
> level, and the upsert reads before it writes — so two *simultaneous* registrations of the same
> token, as happens when an app fires the call twice on launch, do race. The loser catches the
> constraint violation, re-reads the row the winner just committed, and applies its own update to
> it. Both requests return **201** and exactly one row exists. No client-side retry is needed.

---

### 3. Retrieve a Device Token

- **Path**: `GET /api/v1/core/device-tokens/{id}/`
- **Headers**: `Authorization: Bearer <token>`
- **Success Response (200 OK)**: same object shape as §2, wrapped with `"message": "Retrieved successfully"`.

| Status | When | `message` |
|---|---|---|
| **200** | Found. Includes the full `token` string. | `Retrieved successfully` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | Agent token, or no tenant mapping. | as above |
| **404** | No such id, it is soft-deleted, it belongs to another user, or it belongs to another tenant. | — |

> **404 covers "not yours" as well as "not there".** Another user's device id is indistinguishable
> from a nonexistent one, which is deliberate — it prevents probing for which devices exist.

---

### 4. Deactivate a Device Token

Stops pushes to one device **without deleting the row**. Use this on sign-out: the record and its
history survive, and the next successful registration (§2) flips it back on.

- **Path**: `POST /api/v1/core/device-tokens/deactivate/`
- **Headers**: `Authorization: Bearer <token>`
- **Request Payload**:
  ```json
  { "token": "fcm-registration-token-abc123" }
  ```

### Request fields

| Field | Type | Description | Validation |
|---|---|---|---|
| `token` * | string | The exact FCM token string to switch off. Identified by **value, not by id** — the client holds the string from Firebase and need not remember the row id. | Required and truthy. Missing, empty, `null`, or `""` → **400 `token is required`**. Matched exactly, with no whitespace stripping — unlike registration, so pass the string verbatim. |

- **Success Response (200 OK)**:
  ```json
  { "status": true, "message": "Device deactivated", "data": null, "errors": null }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | Matched and deactivated. | `Device deactivated` |
| **200** | **Nothing matched** — unknown token, already deactivated, already soft-deleted, or owned by a different user. | `Device deactivated` |
| **400** | `token` missing or empty. | `token is required` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | Your user has no active tenant mapping. | `Your user account is not associated with any active tenant.` |
| **403** | Agent (`VB_AGENT`) token. | `This endpoint is not available to agent tokens.` |

> **A `200` here does not prove anything was switched off.** The endpoint is a scoped bulk update
> and does not report how many rows it touched, so an unknown or already-inactive token is
> indistinguishable from a real hit. That makes it safely idempotent — sign-out never fails on a
> stale token — but it is useless as a confirmation. Re-read the list (§1) if you need certainty.

> **Scoped to `tenant` + `user` + `token`.** You cannot deactivate someone else's device even by
> guessing the string; the call simply no-ops with a `200`.

### Seven ways a token gets deactivated

Only the first is an API call. `is_active: false` no longer means one thing — **read
`last_error` to find out which happened**:

| Cause | `last_error` reads roughly | Trigger |
|---|---|---|
| You called `deactivate` (§4) | `""` (unchanged) | This endpoint |
| You logged out and named the device | `Signed out on this device` | `POST /auth/logout/` with `device_token` |
| **The login behind it ended** | `Session ended` | Logout, a revoke from the session list, or **the refresh token expiring** |
| Firebase disowned the token | `FCM rejected the token: UnregisteredError` | A real send returning `UNREGISTERED` / `SENDER_ID_MISMATCH` |
| Repeated ambiguous failures | `INVALID_ARGUMENT: … x3` | `DEVICE_TOKEN_FAILURE_STRIKES` consecutive strikes |
| It went stale | `Not re-registered in 60 days` | The nightly sweep |
| It fell off the cap | `Exceeded 20 active devices` | A registration that pushed past `DEVICE_TOKEN_MAX_PER_USER` |

> **A device is tied to the login it registered under.** When that session dies the device
> stops receiving pushes — including when the refresh token simply *expires*, which is the
> one case a client cannot report itself (it has no credentials left to call anything with).
> Re-registering after signing in again relinks and reactivates it. This also means
> `device_token` on logout is now a **fast path, not the only mechanism**: forget it and the
> device is still silenced, just via `revoke()` rather than by name.

> **A transient failure never deactivates anything.** Quota errors, FCM outages, and APNs
> credential problems are logged and leave the token completely alone — counting them would retire
> healthy devices during an incident, which is precisely when push matters most.

Consequence for clients: a device can go inactive between sessions without the user doing anything.
**Re-registering on every app start (§2) is what heals it** — and it heals all six cases,
clearing `last_error` and the strike count along with the flag.

---

### 4b. Send a Test Notification

**The only call that answers "will I actually receive notifications?"** Registration returning
`201` proves the token was *stored* — never that a push will arrive. This sends a real
notification to your own devices and reports what happened to each one.

- **Path**: `POST /api/v1/core/device-tokens/test/`
- **Headers**: `Authorization: Bearer <token>`
- **Request Payload** (all optional):
  ```json
  { "token": "fcm-registration-token-abc123" }
  ```

| Field | Type | Description | Validation |
|---|---|---|---|
| `token` | string | Test **one** device instead of all of yours. | Optional. Must be a token you registered — another user's is treated as not found (`400`), never as an error that reveals it exists. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Test notification accepted by FCM for 1 of 2 device(s).",
    "data": {
      "push_enabled": true,
      "configuration_error": null,
      "registered": 3,
      "targeted": 2,
      "accepted": 1,
      "results": [
        { "id": "d1aa…", "platform": "WEB", "last_seen_at": "2026-08-13T09:00:00Z",
          "accepted": true,  "detail": null, "deactivated": false },
        { "id": "e2bb…", "platform": "IOS", "last_seen_at": "2026-08-01T09:00:00Z",
          "accepted": false, "detail": "UnregisteredError: Requested entity was not found.",
          "deactivated": true }
      ]
    },
    "errors": null
  }
  ```

### Reading the counts

They differ on purpose, and the gaps are the diagnosis:

| Field | Meaning |
|---|---|
| `registered` | Every non-deleted device you have in this tenant — **including** inactive and stale ones |
| `targeted` | How many we actually attempted: active, not deleted, **and** re-registered within `DEVICE_TOKEN_STALE_DAYS` |
| `accepted` | How many FCM took |

`registered: 3, targeted: 2` means one device is inactive or has aged out. `targeted: 2,
accepted: 1` means FCM rejected one — `results[].detail` says why, and `deactivated: true`
means that attempt retired it on the spot.

### Why the failures are separate status codes

A single "0 sent" response would be useless — it cannot distinguish a server with push
switched off from a broken handset, and would send you debugging the wrong thing:

| Status | Meaning | Who fixes it |
|---|---|---|
| **503** | `push_enabled: false` — `FIREBASE_CREDENTIALS` is unset on this server | Operator |
| **503** | `configuration_error` is set — credentials **are** configured but could not be loaded. The reason is echoed back | Operator |
| **400** | You have no deliverable device. `registered: 0` means none at all; `registered > 0` means they are all inactive or stale — open the app to re-register | You |
| **200** | An attempt was made. `results` says what each device did | — |

> **Not rate-limited.** Unlike registration, this endpoint has no throttle — a limit bites
> hardest while push is still being wired up, which is exactly when you need to press it
> repeatedly.

### The same two checks from the Django admin

The endpoint only ever targets **your own** devices, which is the right scope for a
user-facing button and useless when the question is why *somebody else's* handset is silent.
The `Device Token` admin carries two actions for that, still tenant-scoped:

| Action | What it does |
|---|---|
| **Send a test notification** | A real send to the selected rows — identical message to the endpoint, shared code, so the two cannot drift |
| **Validate the token(s) with FCM** | A validate-only probe. **Delivers nothing** and reports alive / dead / inconclusive |

Use them together when a test is *accepted* but never arrives. "Accepted" only means FCM took
the message; **"alive" means FCM still recognises the device**, which is the stronger claim
and the one that separates a server-side problem from a client-side one:

| Send | Validate | Where the fault is |
|---|---|---|
| accepted | alive | **The client.** Service worker, foreground handling, origin, or OS notification settings |
| accepted | dead | The token is stale — re-register on the device |
| not accepted | — | `detail` on the result says why |

> **`accepted: true` is not proof of delivery.** It means FCM took the message. The send API
> issues no per-device receipt, so a device that is switched off, has notifications muted, or
> whose app never renders the foreground message will still show as accepted.

> **The test push is deliberately un-collapsed and short-lived.** No collapse key, so pressing
> the button twice produces two notifications rather than the second silently replacing the
> first; and a 5-minute TTL, so a test cannot arrive hours later looking like a real alert.

---

### 5. Delete a Device Token

- **Path**: `DELETE /api/v1/core/device-tokens/{id}/`
- **Headers**: `Authorization: Bearer <token>`
- **Success Response (200 OK)**: `{ "status": true, "message": "Deleted successfully", "data": null, "errors": null }`

| Status | When | `message` |
|---|---|---|
| **200** | Soft-deleted. | `Deleted successfully` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | Agent token, or no tenant mapping. | as above |
| **404** | No such id, already deleted, or not yours. | — |

**Soft delete**, so the row stays in the database with `is_deleted: true` and drops out of every
list. Because the uniqueness rule only applies to non-deleted rows, **the same token string can be
registered again afterwards** and gets a fresh `id`.

> **Delete vs. deactivate.** Deleting is for "this device is gone for good"; deactivating is for
> "signed out, may return". Deactivating preserves the row so the same `id` comes back on next
> launch, which keeps your device list stable.

---

### 6. Dropdown

- **Path**: `GET /api/v1/core/device-tokens/dropdown/`
- **Headers**: `Authorization: Bearer <token>`
- **Query Parameters**: `limit` (optional) plus the same `platform` / `is_active` filters as List.

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `limit` | integer | Cap the number of options returned. | Must be ≤ **1000**; a larger number is **400 `Limit cannot exceed 1000`**. A non-numeric value is **ignored**, not rejected. Omitted → capped at 1000. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [ { "label": "WEB", "value": "d1aa85f64-5717-4562-b3fc-2c963f66afb2" } ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

> **The `label` is the platform, so labels repeat.** Three Chrome devices all render as `WEB`,
> indistinguishable to a user picking from the list. This dropdown is only meaningful for a person
> with one device per platform; for a real device picker, use List (§1) and build your own label
> from `platform` + `created_at`.

| Status | When | `message` |
|---|---|---|
| **200** | Options returned (paginated). | `Dropdown options retrieved successfully` |
| **400** | `limit` greater than 1000. | `Limit cannot exceed 1000` |
| **401** / **403** | As elsewhere on this page. | — |

---

### 7. Bulk Delete

- **Path**: `POST /api/v1/core/device-tokens/bulk_delete/`
- **Headers**: `Authorization: Bearer <token>`
- **Request Payload**:
  ```json
  { "ids": ["d1aa85f64-5717-4562-b3fc-2c963f66afb2", "e2bb85f64-5717-4562-b3fc-2c963f66afc3"] }
  ```

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Device tokens to soft-delete. | Non-empty list. **Every** id must exist, be undeleted, and belong to **you** in your tenant. All-or-nothing. |

| Status | When | `message` |
|---|---|---|
| **200** | All listed tokens soft-deleted. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | Any id is unknown, already deleted, another user's, or another tenant's — **nothing is deleted**. | `Some ids are invalid` |
| **401** / **403** | As elsewhere on this page. | — |

> **Duplicate ids fail the whole request.** Validation compares the number of matched rows against
> the length of `ids`, so sending the same id twice looks like one missing row and returns
> `Some ids are invalid`. De-duplicate client-side before posting.

> **All-or-nothing, and it runs in a transaction.** A partial success is not possible; if you get a
> `400`, every listed device is still registered.

---

## What actually gets pushed

Registering a token does not subscribe you to anything by name — there are no topics or preference
flags on this API. Delivery is decided entirely by the sending code:

| Audience | Who receives it |
|---|---|
| A specific user | Every **deliverable** token that user has **in that tenant**. |
| Tenant admins | Every **deliverable** token belonging to a user whose type is `ADMIN` in that tenant. |

"Deliverable" means `is_active`, not soft-deleted, **and last registered within
`DEVICE_TOKEN_STALE_DAYS`**. The staleness clause is the one that surprises people: a token can be
`is_active: true` right up until the nightly sweep and still be skipped by a send, because the send
path applies the age rule directly rather than waiting for the sweep to catch up.

Three implications worth designing around:

- **Pushes fan out to all of a user's live devices**, not just the most recent. Deactivate the ones
  you do not want ringing.
- **Notifications expire.** Every push carries a TTL (`PUSH_TTL_SECONDS`, default 24h) and a
  collapse key. A device offline past the TTL gets nothing rather than a stale alert, and repeated
  alerts about the same booking collapse into one.
- **If Firebase credentials are not configured on the server, nothing is sent at all** and the send
  is logged and skipped. Registration still returns `201` in that environment, so a successful
  register in staging tells you nothing about whether push works there. See
  [Firebase / FCM setup](../../external-app-creds/01-firebase-fcm.md).

### Handling a notification

Every push carries a `data.route` — a path relative to your app root, e.g. `bookings/<id>`. Your
handler should navigate there on tap. On web, the same route is additionally set as the
notification's click target, but **only when `FRONTEND_PROJECT_URL` is HTTPS** (FCM rejects
non-HTTPS links), so in local development you get the notification without the click-through.

**Foreground messages are not rendered for you.** FCM does not display a notification while your
app is in the foreground — implement `onMessage` (web) / `onMessageReceived` (mobile) and render
your own toast, or your users will report push as broken when the dashboard tab is active.

## Related

- [FCM End-to-End Flow](../../flows/push-notifications.md) — **when the client should call these
  endpoints**, token expiry and rotation, and the reaping/retention model
- [Firebase / FCM credentials setup](../../external-app-creds/01-firebase-fcm.md)
- [Booking API](./booking.md) — the reschedule alerts that use these tokens
- [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md)

# User Sessions API Documentation

Your active logins, one row per device. This is what powers a "signed-in devices" screen and the
"sign out everywhere" button.

A session is created at [login](./auth.md#1-login) and holds the JWT ids for that device, so
revoking a session **immediately** invalidates its access token rather than waiting up to an hour
for expiry.

**Base path:** `/api/v1/auth/sessions/`

Three things to know:

- **You only ever see your own sessions.** There is no cross-user view, not even for staff.
- **The list is already filtered to live sessions.** Revoked and expired rows never appear —
  which makes the `is_active` filter inert (see §1).
- **Revoking is not deleting.** `DELETE` marks the session revoked and blacklists its access token
  in the cache; the row survives.

> **VB_AGENT access:** none. This viewset declares no `vb_agent_allowed_actions`, so agent tokens
> get **403**. Machine callers have no interactive sessions.

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/auth/sessions/` | List your active sessions |
| `GET` | `/auth/sessions/{id}/` | Retrieve one session |
| `DELETE` | `/auth/sessions/{id}/` | Revoke one session (sign out that device) |
| `POST` | `/auth/sessions/revoke-all/` | Revoke every session **except the current one** |
| `GET` | `/auth/sessions/dropdown/` | Label/value picker options |

**Not available:** `PUT` and `PATCH` are unrouted (**405**) — a session is a record of a login, not
something you edit.

---

## Endpoint Details

### 1. List Active Sessions

Every live session for the authenticated user, most recently updated first.

- **Path**: `GET /api/v1/auth/sessions/`
- **Headers**: `Authorization: Bearer <access_token>`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `page` | integer | Page number. Default `1`. | Beyond the last page → **404**. |
| `page_size` | integer | Rows per page. Default `10`. | Capped at `100`. |
| `search` | string | Partial, case-insensitive match on `device_name` and `ip_address`. | Words split on spaces and **AND**ed. |
| `ordering` | string | Sort. Default `-updated_at`. | Only `created_at` and `updated_at`, optionally `-` prefixed. |
| `is_active` | boolean | **Effectively inert** — see below. | `true` / `false`. |

> **`?is_active=false` always returns an empty list.** The queryset is pre-filtered to
> `is_active=True` and `expires_at` in the future, so the filter can only ever narrow *within* live
> sessions. There is no way to view revoked or expired sessions through this API.

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "9f1c…",
        "device_name": "Chrome on Windows",
        "ip_address": "203.0.113.42",
        "is_active": true,
        "last_used": "2026-08-06T09:12:44Z",
        "created_at": "2026-08-01T08:00:00Z",
        "updated_at": "2026-08-06T09:12:44Z",
        "is_current": true
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

| Field | Meaning |
|---|---|
| `device_name` | Parsed from the `User-Agent` at login. `null` if it could not be read. |
| `ip_address` | The client IP at login. **Not** updated as the device moves networks. |
| `last_used` | When this session last made a request — **throttled to one write per 60 seconds**, so it can lag by up to a minute. |
| `is_current` | `true` for the session making this request. Computed by comparing the request's access-token `jti`. |

> **Use `is_current` to disable the "sign out" button for the row the user is on**, rather than
> comparing ids yourself — the id is a session id, not a token id.

> **`is_current` is `false` for everything when called with a non-JWT credential.** It depends on
> reading a `jti` from the request's auth object.

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more sessions. There is always at least one — the one making the call. | `success` |
| **401** | Missing, expired, or revoked access token. | `Authentication credentials were not provided.` |
| **403** | Agent token. | `This endpoint is not available to agent tokens.` |
| **404** | `page` beyond the last page. | `Invalid page.` |

---

### 2. Retrieve a Session

- **Path**: `GET /api/v1/auth/sessions/{id}/`
- **Success Response (200 OK)**: as §1, plus `expires_at`, `deleted_at`, and `is_deleted`.

| Status | When | `message` |
|---|---|---|
| **200** | Found. | `Retrieved successfully` |
| **401** / **403** | As in §1. | — |
| **404** | No such session, not yours, already revoked, or expired. | `Not found.` |

> **A revoked session returns 404, not a record showing it revoked.** Once you sign a device out it
> disappears entirely; there is no audit view of past sessions here.

---

### 3. Revoke a Session

Signs one device out.

- **Path**: `DELETE /api/v1/auth/sessions/{id}/`
- **Success Response (200 OK)**: `{ "status": true, "message": "Deleted successfully", "data": null, "errors": null }`

| Status | When | `message` |
|---|---|---|
| **200** | Revoked. That device's next request fails. | `Deleted successfully` |
| **401** / **403** | As in §1. | — |
| **404** | No such session, not yours, or already revoked. | `Not found.` |

**What revoking does:**

- Sets `is_active = false` on the session row — the row is **not** deleted.
- Marks the session's access-token `jti` as revoked in the cache for the remainder of its lifetime,
  so the access token stops working **immediately** rather than at its 60-minute expiry.
- The refresh token is **not** blacklisted, but a refresh attempt fails anyway because
  [refresh](./auth.md#3-refresh) requires an active session.
- **Push notifications to that device stop.** A registered FCM device token records the session it
  was registered under, so revoking the session deactivates it. The device goes quiet immediately
  rather than continuing to show alerts to whoever is holding it. See
  [Device Tokens](../core/device_token.md).

> **You can revoke the session you are currently using.** Nothing prevents it, and the effect is an
> immediate logout — your next request returns `401`. Use `is_current` to warn the user first.

> **Revoking is not the same as [logout](./auth.md#2-logout).** Logout additionally blacklists the
> refresh token you hand it. Revoking relies on the session check instead. Either way the device is
> signed out.

---

### 4. Revoke All Other Sessions

The "sign out everywhere else" action. **Keeps the session you are calling from.**

- **Path**: `POST /api/v1/auth/sessions/revoke-all/`
- **Request Payload**: none. Any body is ignored.

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Successfully signed out of 3 other device(s).",
    "data": null,
    "errors": null
  }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | Revoked, with the count in the message. `0` is normal when there is only one session. | `Successfully signed out of N other device(s).` |
| **401** / **403** | As in §1. | — |

> **The count is in the message text, not in `data`.** There is no structured field to read — parse
> the message, or re-fetch the list (§1) to see what remains.

> **"Current" is identified by the access-token `jti`, not the session id.** If you call this with
> a credential that carries no `jti`, no session matches as current and **every** session is
> revoked, including your own.

---

### 5. Dropdown

- **Path**: `GET /api/v1/auth/sessions/dropdown/`
- **Query Parameters**: `limit` (≤ **1000**; over that → **400 `Limit cannot exceed 1000`**), plus the List filters and `search`.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [ { "label": "Chrome on Windows (IP: 203.0.113.42)", "value": "9f1c…" } ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

`label` is `"<device_name> (IP: <ip_address>)"`, with `Unknown` substituted for either when it is
missing. Note the dropdown gives you no `is_current` flag — use List (§1) if the picker must
exclude the current device.

---

## How a session ends

| Cause | Effect on the access token |
|---|---|
| `DELETE /sessions/{id}/` | Rejected immediately — the `jti` is cached as revoked. |
| `POST /sessions/revoke-all/` | Same, for every other session. |
| [`POST /auth/logout/`](./auth.md#2-logout) | Same, plus the refresh token is blacklisted. |
| `expires_at` passes | Rejected on the next database check. |
| Password reset | **Nothing.** Sessions survive — see [auth Known gaps](./auth.md#known-gaps). |

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | `POST /auth/sessions/` is routed but cannot work. The serializer has no `user` field, and the column is non-null, so a well-formed request fails at the database. | A `500` where a `405` would be correct. Do not call it. |
| 2 | `POST /auth/sessions/bulk_delete/` soft-deletes rows instead of calling `revoke()`. | The session stops validating against the database, but its access token is **not** cached as revoked — so it can keep working until the cached "active" entry expires. Use `revoke-all/` or per-session `DELETE`. |
| 3 | Revoked and expired sessions are unreachable through the API. | There is no way to show a user their sign-in history, only their current devices. |
| 4 | `ip_address` and `device_name` are captured once, at login. | A long-lived session shows where it started, not where it is being used from — misleading on a "suspicious activity" screen. |

## Related

- [Authentication API](./auth.md) — login, logout, refresh
- [Users API](./users.md) — `session_timeout_hours` sets how long a session lasts

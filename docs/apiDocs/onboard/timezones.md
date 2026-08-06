# Timezones API Documentation

The IANA timezone table. Every timezone reference elsewhere in the system — on a
[user](../user/users.md), a tenant, an [agent](../core/agent.md), an
[availability profile](../core/availability_profile.md), a [campaign](../core/campaign.md)
schedule — is a foreign key to a row here.

**Base path:** `/api/v1/onboard/timezones/`

Three things govern this API:

- **Reads need a login; writes need a superuser.** Unlike [plans](./plans.md), listing timezones is
  **not** public.
- **Read `current_offset`, not `offset`.** `offset` is a static standard-time string stored on the
  row; `current_offset` is computed per request from the live zone. For any DST-observing zone the
  static one is wrong for roughly half the year.
- **This table is the conversion authority.** Wall-clock ↔ UTC conversion across the codebase —
  availability materialisation, campaign scheduling, agent "spoken clock" — resolves through the
  `timezone` string here.

## Endpoints Summary

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/onboard/timezones/` | List timezones (paginated) | Any authenticated user |
| `GET` | `/onboard/timezones/{id}/` | Retrieve one | Any authenticated user |
| `GET` | `/onboard/timezones/dropdown/` | Label/value picker options | Any authenticated user |
| `POST` | `/onboard/timezones/` | Create | Superuser |
| `PUT` / `PATCH` | `/onboard/timezones/{id}/` | Update | Superuser |
| `DELETE` | `/onboard/timezones/{id}/` | Soft-delete | Superuser |
| `POST` | `/onboard/timezones/bulk_delete/` | Bulk soft-delete | Superuser |

---

## `offset` vs `current_offset`

| Field | Source | When to use |
|---|---|---|
| `offset` | A stored string, e.g. `UTC+00:00`. Set when the row was seeded and **never recalculated**. | Effectively never. Kept so existing consumers do not break. |
| `current_offset` | Computed at request time from the real zone. | **Always** — this is what to display. |

> **The difference is not cosmetic.** `Europe/London` stores `UTC+00:00`, but in July it is
> actually `UTC+01:00`. A picker rendering `offset` tells a British user the wrong time for half the
> year. The dropdown label already uses the live value; make sure your custom rendering does too.

> **`current_offset` costs nothing extra on a list.** The clock is read once per response and
> cached across rows, not once per row.

---

## Endpoint Details

### 1. List Timezones

- **Path**: `GET /api/v1/onboard/timezones/`
- **Headers**: `Authorization: Bearer <access_token>`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `page` | integer | Page number. Default `1`. | Beyond the last page → **404**. |
| `page_size` | integer | Rows per page. Default `10`; there are several hundred timezones, so **raise this or paginate**. | Capped at `100`. |
| `search` | string | Partial, case-insensitive match on `timezone`, `offset`, `region`. | Words split on spaces and **AND**ed. |
| `ordering` | string | Sort. Default `timezone` (alphabetical). | Only `timezone`, `offset`, `created_at`, optionally `-` prefixed. |
| `timezone` | string | Exact match on the IANA identifier, e.g. `Asia/Kolkata`. | — |
| `offset` | string | Exact match on the **stored** offset string, e.g. `UTC+05:30`. | Matches the static column, so it will not find a zone by its *current* offset. |
| `region` | string | Exact match. `region__icontains` is also available for partial matching. | — |
| `is_active` | boolean | Exact match. | `true` / `false`. |
| `created_at__date` / `__gte` / `__lte` | date / datetime | Creation window. | — |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "offset": "UTC+05:30",
        "current_offset": "UTC+05:30",
        "region": "Asia",
        "timezone": "Asia/Kolkata",
        "is_active": true,
        "created_at": "2026-01-04T09:00:00Z"
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more timezones. | `success` |
| **400** | Malformed date or non-boolean filter value. | filter error |
| **401** | Not authenticated. | `Authentication credentials were not provided.` |
| **404** | `page` beyond the last page. | `Invalid page.` |

> **Searching by offset is a trap.** `?search=UTC+05:30` and `?offset=UTC+05:30` both match the
> static column. A zone currently at +05:30 because of DST will not be found, and one stored as
> +05:30 but currently +06:30 will be.

---

### 2. Retrieve a Timezone

- **Path**: `GET /api/v1/onboard/timezones/{id}/`
- **Success Response (200 OK)**: as §1, plus `updated_at`.

| Status | When | `message` |
|---|---|---|
| **200** | Found — **including inactive rows**. | `Retrieved successfully` |
| **401** | Not authenticated. | `Authentication credentials were not provided.` |
| **404** | No such timezone, or soft-deleted. | `Not found.` |

---

### 3. Create a Timezone

**Superuser only.** Rarely needed — the table is seeded from the IANA database.

- **Path**: `POST /api/v1/onboard/timezones/`

### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `timezone` * | string | The IANA identifier, e.g. `Europe/London`. **This is the functional value** — everything that converts time resolves through it. | Required. Must be a **real IANA zone**; an unknown identifier is rejected with `'X' is not a valid IANA timezone identifier.` Should be unique, though uniqueness is not enforced at the database level. |
| `offset` | string | Display string for standard time, e.g. `UTC+05:30`. Cosmetic only. | Optional. **Free text — not validated and not derived from `timezone`.** Nothing stops it disagreeing with reality. |
| `region` | string | Grouping label for pickers, e.g. `Asia`. | Optional. Free text. |
| `is_active` | boolean | Whether it may be offered. | Optional, default `true`. |

**Read-only:** `id`, `current_offset`, `created_at`, `updated_at`.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **201** | Created. | `Created successfully` | `null` |
| **400** | `timezone` is not a valid IANA identifier. | `'X' is not a valid IANA timezone identifier.` | `{"timezone": [...]}` |
| **400** | `timezone` missing. | `This field is required.` | `{"timezone": [...]}` |
| **401** | Not authenticated. | `Authentication credentials were not provided.` | `{"detail": …}` |
| **403** | Authenticated but not a superuser. | permission denied | `{"detail": …}` |

> **`offset` is not checked against `timezone`.** You can create `Asia/Kolkata` with an `offset` of
> `UTC-08:00` and nothing will complain — `current_offset` will simply disagree with it. There is a
> management command, `audit_timezone_offsets`, for finding rows where the stored string has
> drifted.

---

### 4. Update / Delete

- **Paths**: `PUT` / `PATCH` / `DELETE /api/v1/onboard/timezones/{id}/` — **superuser only**.

| Status | When | `message` |
|---|---|---|
| **200** | Updated or soft-deleted. | `Updated successfully` / `Deleted successfully` |
| **400** | Validation failed (update only). | as §3 |
| **401** / **403** | As in §3. | — |
| **404** | No such timezone, or already deleted. | `Not found.` |

> **Deleting a timezone in use does not fail, and does not cascade.** Users, tenants, agents,
> availability profiles, and campaigns reference this table with `PROTECT` or `SET_NULL` depending
> on the model — but this endpoint soft-deletes, which no foreign key notices. The row simply
> vanishes from pickers while existing references keep resolving. Prefer `is_active: false`.

> **Changing a row's `timezone` string silently re-times everything pointing at it.** An
> availability profile or scheduled campaign attached to that row will convert against the new
> zone. Create a new row instead.

---

### 5. Dropdown

- **Path**: `GET /api/v1/onboard/timezones/dropdown/`
- **Query Parameters**: `limit` (≤ **1000**; over that → **400 `Limit cannot exceed 1000`**), plus all List filters and `search`.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [ { "label": "Asia/Kolkata (UTC+05:30)", "value": "3fa85f64-…" } ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

`label` is `"<timezone> (<current_offset>)"` — built from the **live** offset, so a picker shows
London as `UTC+01:00` in summer.

> **Raise `limit` or you will get one page of a few hundred zones.** Pass `?is_active=true` as well.

---

### 6. Bulk Delete

- **Path**: `POST /api/v1/onboard/timezones/bulk_delete/` — **superuser only**.
- **Request Payload**: `{ "ids": ["3fa85f64-…", "…"] }`

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Timezones to soft-delete. | Non-empty list. Every id must exist and be undeleted. All-or-nothing. |

| Status | When | `message` |
|---|---|---|
| **200** | All soft-deleted. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | Any id unknown or already deleted. | `Some ids are invalid` |
| **401** / **403** | As in §3. | — |

---

## How the rest of the system uses this

| Consumer | Field | Effect |
|---|---|---|
| [User](../user/users.md) | `timezone` | Display preference for schedules and notifications. |
| Tenant | `timezone` | The default for [campaign scheduling](../core/campaign.md) when nothing more specific applies. |
| [Agent](../core/agent.md) | `timezone` | The clock the bot **speaks** in on a call. |
| [Availability Profile](../core/availability_profile.md) | `timezone` | The wall-clock frame working hours are defined in, materialised to UTC instants. |

> **`UTC` is the fallback everywhere.** If a row is missing or its identifier does not resolve,
> conversion falls back to UTC rather than failing — which is safe but silently wrong, so keep the
> table seeded.

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **`offset` is stored, never recalculated, and not validated against `timezone`.** | Any client reading `offset` shows the wrong time for DST zones for half the year. Read `current_offset`. |
| 2 | **`timezone` is not unique at the database level.** | Duplicate rows for the same zone can be created, and pickers then show it twice. |
| 3 | **Deletion has no in-use guard.** | A timezone still referenced by profiles or campaigns can be removed from pickers with no warning. |
| 4 | **Filtering and searching by offset use the static column.** | Offset-based lookups are unreliable for DST zones. |

## Related

- [Languages API](./languages.md) — the sibling reference table
- [Availability Profile API](../core/availability_profile.md) — the heaviest consumer
- [Campaign API](../core/campaign.md) — scheduling in tenant-local time
- [Users API](../user/users.md) — per-user preference

# Availability Profile API Documentation

Defines **when a user is available to be booked** — their weekly working hours, expressed in their
own timezone.

A profile is a container (`user`, `timezone`, `name`) holding a set of weekly **rules**. Each rule
is one working block on one weekday. Multiple rules on the same weekday give split shifts
(`09:00–12:00` and `14:00–17:00`), and a rule whose `end_time` is on or before its `start_time`
runs overnight into the next day.

**One active profile per user, per tenant.** Creating a second for the same user is a `400`.

> **How this reaches the booking engine.** Rules are stored as wall-clock times, but the engine
> compares absolute UTC instants. A background job converts one into the other whenever a profile
> changes — see [Availability is rebuilt asynchronously](#availability-is-rebuilt-asynchronously).
> This is why a profile created *outside* the API produces no slots until a command is run.

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/core/availability-profiles/` | List profiles (paginated) |
| `GET` | `/api/v1/core/availability-profiles/{id}/` | Retrieve one profile, with its rules |
| `POST` | `/api/v1/core/availability-profiles/` | Create a profile and its rules |
| `PUT` / `PATCH` | `/api/v1/core/availability-profiles/{id}/` | Update a profile; sending `rules` **replaces** them all |
| `DELETE` | `/api/v1/core/availability-profiles/{id}/` | Soft-delete a profile and purge its availability |
| `GET` | `/api/v1/core/availability-profiles/dropdown/` | Compact label/value list for pickers |
| `POST` | `/api/v1/core/availability-profiles/bulk_delete/` | Soft-delete several profiles at once |

---

## The rule model — read this before writing rules

A rule is `{weekday, start_time, end_time}` in the **profile's timezone**, never UTC.

| Rule | Means |
|---|---|
| `{"weekday": 0, "start_time": "09:00:00", "end_time": "17:00:00"}` | Monday 09:00–17:00 |
| `{"weekday": 0, "start_time": "22:00:00", "end_time": "06:00:00"}` | Monday 22:00 → **Tuesday** 06:00 (overnight) |
| `{"weekday": 0, "start_time": "00:00:00", "end_time": "00:00:00"}` | All of Monday — exactly 24 hours |

**`end_time <= start_time` means the block ends the next day.** This is how overnight shifts are
expressed; there is deliberately no "ends next day" flag. A consequence: a zero-length block cannot
be represented, because `00:00 → 00:00` is reserved for the full day. Seven `00:00 → 00:00` rules
give true 24/7 with no gaps.

`weekday` is always the day the block **starts** on: `0 = Monday … 6 = Sunday`, matching Python's
`weekday()`.

### Overlap is checked across the whole week, circularly

Rules are laid out on a 604 800-second week timeline and every pair is compared — including against
its neighbours a week either side. So all three of these are rejected:

| Rules | Why it is rejected |
|---|---|
| `Mon 09:00–17:00` + `Mon 16:00–18:00` | plain same-day overlap |
| `Mon 22:00→06:00` + `Tue 02:00–09:00` | the wrap runs into Tuesday morning |
| `Sun 23:00→07:00` + `Mon 02:00–10:00` | the wrap crosses the week boundary |

The last two are invisible to per-weekday checking, which is why the comparison is circular.

---

## 1. List Availability Profiles

**`GET /api/v1/core/availability-profiles/`**

Paginated list for the caller's tenant. Returns a compact shape — **rules are not included**; use
Retrieve for those.

### Query parameters

| Parameter | Type | Description |
|---|---|---|
| `page` | integer | Page number, 1-based. |
| `page_size` | integer | Rows per page. |
| `user` | UUID | Only the profile belonging to this user. |
| `is_active` | boolean | Filter by active flag. **Inactive profiles still appear here** — they are excluded from *booking*, not from listings. |
| `ordering` | string | `created_at` or `updated_at`, prefix `-` to reverse. Default `-created_at`. |

### Response — `200 OK`

```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "id": "1aa85f64-5717-4562-b3fc-2c963f66af10",
      "user": {
        "id": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
        "email": "agent@example.com",
        "first_name": "Jane",
        "last_name": "Doe"
      },
      "name": "Default",
      "timezone": { "id": "2b1c…", "timezone": "Asia/Kolkata", "offset": "UTC+05:30" },
      "is_active": true,
      "created_at": "2026-06-03T18:00:00Z"
    }
  ],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

> `timezone.offset` is a **standard-time display string**, not the offset in force today. A zone
> that observes DST is one hour off this value for half the year. Never compute with it — the
> engine doesn't.

---

## 2. Retrieve Availability Profile

**`GET /api/v1/core/availability-profiles/{id}/`**

The full profile including its rules.

### Response — `200 OK`

```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "id": "1aa85f64-5717-4562-b3fc-2c963f66af10",
    "tenant": { "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c", "name": "Alpha Corp" },
    "user": { "id": "5fa85f64…", "email": "agent@example.com", "first_name": "Jane", "last_name": "Doe" },
    "timezone": { "id": "2b1c…", "timezone": "Asia/Kolkata", "offset": "UTC+05:30" },
    "name": "Default",
    "rules": [
      { "id": "r1", "weekday": 0, "start_time": "09:00:00", "end_time": "12:00:00" },
      { "id": "r2", "weekday": 0, "start_time": "14:00:00", "end_time": "17:00:00" },
      { "id": "r3", "weekday": 1, "start_time": "22:00:00", "end_time": "06:00:00" }
    ],
    "is_active": true,
    "created_by": { "id": "3fa8…", "name": "Emma" },
    "updated_by": null,
    "deleted_by": null,
    "created_at": "2026-06-03T18:00:00Z",
    "updated_at": "2026-06-03T18:00:00Z",
    "deleted_at": null
  },
  "errors": null
}
```

| Status | When |
|---|---|
| **200** | Found. |
| **401** | Missing or invalid token. |
| **403** | Caller's account has no tenant mapping — `Your user account is not associated with any active tenant.` |
| **404** | No such profile **in your tenant** — the same response as an id that does not exist anywhere, so this never confirms another tenant's data. |

---

## 3. Create Availability Profile

**`POST /api/v1/core/availability-profiles/`**

Creates the profile and, in the same transaction, its rules. On success a background job
materializes the profile's bookable time.

### Request body

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `user` * | UUID | The person these working hours belong to. | Must belong to your tenant. Must not already have an active profile — one per user. |
| `timezone` * | UUID | FK to a `Timezone`. **Every `start_time`/`end_time` below is wall-clock in this zone.** | Must be an existing `Timezone` id. Get the list from the timezone endpoint. |
| `name` | string | Label for the profile. | Max 100 chars. Defaults to `"Default"`. |
| `rules` | array of objects | The weekly working blocks. Omit or send `[]` to create a profile with no availability yet. | See the rule table below; the whole set is validated together. |
| `is_active` | boolean | Whether these hours count for booking. | Defaults to `true`. Setting `false` makes the host unbookable and purges their materialized availability. |

#### `rules[]` object

| Field | Type | Description | Validation |
|---|---|---|---|
| `weekday` * | integer | The day the block **starts** on. `0 = Monday … 6 = Sunday`. | Must be 0–6. |
| `start_time` * | `HH:MM:SS` | Local wall-clock start. | Valid time. |
| `end_time` * | `HH:MM:SS` | Local wall-clock end. **If ≤ `start_time`, the block ends the next day.** | Valid time. No per-rule ordering requirement — the constraint is that no two rules overlap across the circular week. |

### Example request

```json
{
  "user": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
  "timezone": "2b1c85f64-5717-4562-b3fc-2c963f66afa9",
  "name": "Default",
  "rules": [
    { "weekday": 0, "start_time": "09:00:00", "end_time": "17:00:00" },
    { "weekday": 1, "start_time": "22:00:00", "end_time": "06:00:00" }
  ]
}
```

### Responses

| Status | When | `message` |
|---|---|---|
| **201** | Created. Body is the full profile (same shape as Retrieve). | `Created successfully` |
| **400** | Validation failed — see the table below. | the first error |
| **400** | Caller's account has no tenant mapping. Serializer validation runs before the tenant is injected, so this surfaces as a `400`, not a `403`. | `User is not associated with an active tenant.` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |

#### `400` causes

| Cause | `message` | `errors` key |
|---|---|---|
| Two rules overlap (same day, wrap, or across the week boundary) | `Overlapping availability rules: weekday 0 conflicts with weekday 1.` | `rules` |
| Rules total more than 168 hours | `These rules cover more than a full week, so they must overlap.` | `rules` |
| `user` belongs to another tenant | `User does not belong to your tenant.` | `user` |
| `user` already has an active profile | `This user already has an availability profile.` | `user` |
| `timezone` id unknown | `Invalid pk "…" - object does not exist.` | `timezone` |
| Required field missing | `This field is required.` | the field |

Example:

```json
{
  "status": false,
  "message": "Overlapping availability rules: weekday 0 conflicts with weekday 1.",
  "data": null,
  "errors": { "rules": ["Overlapping availability rules: weekday 0 conflicts with weekday 1."] },
  "request_id": "a3f1…"
}
```

---

## 4. Update / Patch Availability Profile

**`PUT` / `PATCH /api/v1/core/availability-profiles/{id}/`**

Same field table as Create; on `PATCH` every field is optional.

### Rule replacement semantics

| You send | Result |
|---|---|
| `rules` with a list | **The entire existing set is deleted and replaced.** There is no per-rule patch — send the complete desired week. |
| `rules: []` | All rules removed. The host keeps their profile but becomes unbookable. |
| no `rules` key | Existing rules untouched. |

Because it is a full replacement, a `PATCH` that only changes `name` must omit `rules` entirely —
sending a partial list would silently delete the rest of the week.

### Responses

| Status | When | `message` |
|---|---|---|
| **200** | Updated. Body is the full profile. | `Updated successfully` |
| **400** | Same validation table as Create. | the first error |
| **404** | No such profile in your tenant. | — |

### Availability is rebuilt asynchronously

The booking engine reads **materialized UTC intervals**, not the wall-clock rules you write here. A
background task regenerates them after the transaction commits, whenever a write changes:

- `rules`
- `timezone` — the same wall-clock hours land on different UTC instants, so every window is
  re-anchored even though no rule changed
- `is_active` — deactivating purges the profile's availability entirely

Practical consequences:

| | |
|---|---|
| **Slots can lag a write** | By the length of the Celery queue, normally under a second. A slot query issued immediately after a profile write may still reflect the previous rules. |
| **Only a rolling horizon is materialized** | 90 days by default (`AVAILABILITY_MATERIALIZE_DAYS`), automatically extended to cover any event template advertising a longer booking horizon. A daily job rolls it forward. |
| **Profiles written outside the API produce nothing** | Fixtures, shell sessions, and direct ORM writes do not trigger the rebuild. Run `manage.py materialize_availability`. |
| **DST is applied per date, not per rule** | `09:00` local stays `09:00` local across a transition; its UTC instant shifts by an hour. Nothing you send changes. |

---

## 5. Delete (Soft Delete)

**`DELETE /api/v1/core/availability-profiles/{id}/`**

Soft-deletes the profile and **hard-deletes its materialized availability**, so the host stops being
bookable immediately rather than at the next background run.

| Status | When | `message` |
|---|---|---|
| **200** | Deleted. | `Deleted successfully` |
| **404** | No such profile in your tenant. | — |

The user's history is unaffected: existing bookings, including future ones, are untouched. Deleting
a profile stops *new* bookings, it does not cancel anything.

---

## 6. Dropdown

**`GET /api/v1/core/availability-profiles/dropdown/`**

Minimal `label`/`value` pairs for a picker, skipping pagination.

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `limit` | integer | Max rows to return. | **A `limit` above 1000 is a `400`** (`Limit cannot exceed 1000`), not a silent cap. Omitting it returns at most 1000. |
| `user`, `is_active` | — | Same filters as List. | — |

```json
{
  "status": true,
  "message": "Dropdown options retrieved successfully",
  "data": [{ "label": "Default", "value": "1aa85f64-5717-4562-b3fc-2c963f66af10" }],
  "errors": null
}
```

---

## 7. Bulk Delete

**`POST /api/v1/core/availability-profiles/bulk_delete/`**

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Profiles to soft-delete. | Must be a non-empty list. Every id must exist, be **not already deleted**, and be **in your tenant**. |

**All-or-nothing.** If any id is unknown, already deleted, or belongs to another tenant, nothing is
deleted and the request fails — so a partial success can never leave you guessing which ones went.
The deletes run in one transaction.

| Status | When | `message` |
|---|---|---|
| **200** | All deleted, and their materialized availability purged. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | At least one id unknown, already deleted, or in another tenant; **nothing was deleted**. | `Some ids are invalid` |

> Like the single delete, this purges each profile's materialized availability immediately, so the
> hosts stop being bookable at once rather than at the next background run.

---

## Common scenarios

| Goal | How |
|---|---|
| Standard weekday office hours | Five rules, `weekday` 0–4, `09:00:00 → 17:00:00`. |
| Split shift with a lunch break | Two rules on the same weekday: `09:00–13:00` and `14:00–18:00`. |
| Night shift | `{"weekday": 0, "start_time": "22:00:00", "end_time": "06:00:00"}` — one rule, not two. |
| True 24/7 | Seven rules, `00:00:00 → 00:00:00`, one per weekday. |
| Temporarily stop bookings | `PATCH {"is_active": false}`. Reversible, and keeps the rules. |
| One-off day off | Not here — create an **[Availability Exception](./availability_exception.md)**. Rules are the recurring pattern; exceptions are the deviations. |
| Move a host to a new timezone | `PATCH {"timezone": "<id>"}`. Wall-clock hours stay as written and every future slot re-anchors. |

## Related

- [Available Slots API](./availability_slots.md) — what these hours produce
- [Availability Exception API](./availability_exception.md) — leave and one-off blocks
- [Setup flow](../../flows/01-setup.md)

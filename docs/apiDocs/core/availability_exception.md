# Availability Exception API Documentation

Log time off / leave / emergency absence for a user, scoped to the authenticated tenant.
**Creating an exception triggers the reschedule workflow**: any of that user's future
active bookings that overlap the window are flagged `REQUIRES_RESCHEDULE` and the
customer is emailed a self-serve reschedule link. Hosts are never swapped silently.

## Endpoints Summary
- `GET /api/v1/core/availability-exceptions/` - List exceptions (paginated)
- `GET /api/v1/core/availability-exceptions/{id}/` - Retrieve an exception
- `POST /api/v1/core/availability-exceptions/` - Log leave (triggers reschedule)
- `PUT` / `PATCH /api/v1/core/availability-exceptions/{id}/` - Update an exception
- `DELETE /api/v1/core/availability-exceptions/{id}/` - Soft-delete an exception
- `GET /api/v1/core/availability-exceptions/dropdown/` - Get label/value list
- `POST /api/v1/core/availability-exceptions/bulk_delete/` - Bulk soft-delete exceptions

---

## Endpoint Details

### 1. List Availability Exceptions
- **Path**: `GET /api/v1/core/availability-exceptions/`
- **Headers**: `Authorization: Bearer <token>`
- **Query Parameters**: `page`, `page_size`, `user` (filter), `type` (filter), `is_active` (filter). `ordering` by `start_time`, `end_time`, `created_at`.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "8aa85f64-5717-4562-b3fc-2c963f66af20",
        "user": { "id": "5fa85f64-…", "email": "alex@acme.com", "first_name": "Alex", "last_name": "Host" },
        "start_time": "2026-07-01T13:00:00Z",
        "end_time": "2026-07-01T17:00:00Z",
        "type": "EMERGENCY",
        "is_all_day": false,
        "created_at": "2026-06-30T10:00:00Z"
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

---

### 2. Create Availability Exception (Log Leave)
- **Path**: `POST /api/v1/core/availability-exceptions/`
- **Request Payload**:
  ```json
  {
    "user": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
    "start_time": "2026-07-01T13:00:00Z",
    "end_time": "2026-07-01T17:00:00Z",
    "type": "EMERGENCY",
    "reason": "Family emergency",
    "is_all_day": false
  }
  ```
### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `user` * | UUID | Whose time off this is. Their bookings in the window are what gets flagged. | Must belong to your tenant. |
| `start_time` * | ISO-8601 datetime | When the absence begins. **Absolute UTC instant**, unlike availability rules which are wall-clock. | Must parse; naive values read as UTC. |
| `end_time` * | ISO-8601 datetime | When the absence ends. | Must be **strictly after** `start_time`. |
| `type` | enum | Why they are away: `VACATION`, `SICK`, `EMERGENCY`, `HOLIDAY`, `OTHER`. | Default `OTHER`. Descriptive only — every type blocks availability identically. |
| `reason` | string | Free-text note. Also used as the dropdown `label`. | Optional. |
| `is_all_day` | boolean | Marks the entry as a whole-day absence for display. | Default `false`. **Descriptive only** — the actual block is `start_time`→`end_time` regardless, so set those to cover the full day too. |
| `is_active` | boolean | Whether this exception counts. | Default `true`. |

> **Note the contrast with `AvailabilityRule`.** Rules use wall-clock `TimeField`s interpreted in
> the profile's timezone; exceptions use absolute `DateTimeField`s. Same field names, different
> meaning — send real instants here.

### Side effect: the reschedule workflow

Creating an exception queues the reschedule engine for that user. Any of their **future active**
bookings (`SCHEDULED` / `CONFIRMED`) overlapping the window are:

1. flagged `REQUIRES_RESCHEDULE`,
2. issued a single-use reschedule token, and
3. emailed to the customer as a self-serve [reschedule link](./public_reschedule.md).

**Hosts are never silently swapped.** The customer chooses a new time with the same host.

Two consequences worth planning for:

- The work is **asynchronous**, so the `201` returns before any booking is flagged. Poll the
  bookings list if a UI needs to show the outcome.
- A flagged booking still **occupies its original slot** until the customer rebooks, so that time
  is not offered to anyone else — and the customer cannot pick their original time back, even if
  the leave is later deleted.

Deleting the exception does **not** un-flag bookings; those customers already have their emails.
- **Success Response (201 Created)**:
  ```json
  {
    "status": true,
    "message": "Created successfully",
    "data": {
      "id": "8aa85f64-5717-4562-b3fc-2c963f66af20",
      "tenant": { "id": "7bf2...", "name": "Alpha Corp", "subdomain": "alpha-corp" },
      "user": { "id": "5fa85f64-…", "email": "alex@acme.com", "first_name": "Alex", "last_name": "Host" },
      "start_time": "2026-07-01T13:00:00Z",
      "end_time": "2026-07-01T17:00:00Z",
      "type": "EMERGENCY",
      "reason": "Family emergency",
      "is_all_day": false,
      "is_active": true,
      "created_by": { "id": "3fa8...", "name": "Emma" },
      "updated_by": null,
      "deleted_by": null,
      "created_at": "2026-06-30T10:00:00Z",
      "updated_at": "2026-06-30T10:00:00Z",
      "deleted_at": null
    },
    "errors": null
  }
  ```
### Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **201** | Created; reschedule workflow queued. | `Created successfully` | `null` |
| **400** | `end_time` on or before `start_time`. | `end_time must be after start_time.` | `{"end_time": [...]}` |
| **400** | `user` belongs to another tenant. | `User does not belong to your tenant.` | `{"user": [...]}` |
| **400** | Caller's account has no tenant mapping. | `User is not associated with an active tenant.` | non-field |
| **400** | Missing/unparseable required field. | the field's message | `{"<field>": [...]}` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": ...}` |

```json
{
  "status": false,
  "message": "end_time must be after start_time.",
  "data": null,
  "errors": { "end_time": ["end_time must be after start_time."] },
  "request_id": "a3f1…"
}
```

> **Overlapping exceptions are allowed.** Two leave entries covering the same hours is not an
> error — they simply union. Unlike availability rules, there is no overlap validation here.

---

### 3. Update / Delete

**`PUT` / `PATCH /api/v1/core/availability-exceptions/{id}/`** — adjust the window, type, or reason.

Validation matches Create. **Updating does not re-run the reschedule workflow**, which only fires
on creation. Widening a window to cover more bookings therefore leaves those extra bookings
untouched — delete the exception and create a new one if you need them flagged.

**`DELETE /api/v1/core/availability-exceptions/{id}/`** — soft-delete. The host becomes bookable in
that window again immediately, but any booking already flagged `REQUIRES_RESCHEDULE` **stays
flagged**; its customer has already been emailed.

| Status | When | `message` |
|---|---|---|
| **200** | Updated / deleted. | `Updated successfully` / `Deleted successfully` |
| **400** | Validation failed (update only). | as above |
| **404** | No such exception in your tenant. | — |

---

### 4. Dropdown
- **Path**: `GET /api/v1/core/availability-exceptions/dropdown/`
- **Query Parameters**: `limit` (optional, ≤ 1000) + the same `user` / `type` / `is_active` filters as List.
- **Note**: The dropdown `label` is the exception's `reason`.
- **Success Response (200 OK)**: `{ "status": true, "message": "Dropdown options retrieved successfully", "data": [ { "label": "Family emergency", "value": "8aa85f64-..." } ], "errors": null }`

---

### 5. Bulk Delete

**`POST /api/v1/core/availability-exceptions/bulk_delete/`**

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Exceptions to soft-delete. | Non-empty list; every id must exist, be undeleted, and be in your tenant. All-or-nothing. |

| Status | When | `message` |
|---|---|---|
| **200** | All soft-deleted. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | Any id unknown, already deleted, or in another tenant; **nothing deleted**. | `Some ids are invalid` |

---

## Rules vs exceptions — which one do you want?

| You want to express | Use |
|---|---|
| "Alex works Mon–Fri 9–5" | [Availability Profile](./availability_profile.md) rules — the recurring pattern |
| "Alex is off next Tuesday afternoon" | an **exception** — a one-off deviation |
| "Alex no longer works here" | set the profile `is_active: false`, or delete it |
| "Alex is off every Wednesday from now on" | edit the **rules** — an exception per week would be unmanageable |

Rules define when someone *is* available; exceptions carve holes out of that. Both are subtracted
before slots are offered, but only an exception triggers the reschedule workflow for bookings
already made.

## Related

- [Availability Profile API](./availability_profile.md) — the recurring working hours
- [Public Reschedule API](./public_reschedule.md) — where the emailed link lands
- [Leave & Reschedule flow](../../flows/03-leave-and-reschedule.md)

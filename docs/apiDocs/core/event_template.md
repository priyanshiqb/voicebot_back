# Event Template API Documentation

Manage bookable appointment types (e.g. "30-Min Demo") scoped to the authenticated tenant.
An event template defines duration, buffers, how a host is chosen, and the booking policy
(min notice, horizon, lead times).

> **VB_AGENT access:** read-only — `list`, `retrieve`, `dropdown`. Creating, updating and
> deleting bookable types is human configuration and returns **403**. Agent tokens **must**
> send `X-Tenant-ID: <tenant uuid>` (**400** if omitted).
> See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

## Endpoints Summary
- `GET /api/v1/core/event-templates/` - List all event templates (paginated)
- `GET /api/v1/core/event-templates/dropdown/` - Get label/value list
- `GET /api/v1/core/event-templates/{id}/` - Retrieve template details
- `POST /api/v1/core/event-templates/` - Create a template
- `PUT /api/v1/core/event-templates/{id}/` - Update a template
- `PATCH /api/v1/core/event-templates/{id}/` - Partial update a template
- `DELETE /api/v1/core/event-templates/{id}/` - Soft-delete a template
- `POST /api/v1/core/event-templates/bulk_delete/` - Bulk soft-delete templates

---

## Endpoint Details

### 1. List Event Templates
- **Path**: `GET /api/v1/core/event-templates/`
- **Headers**:
  - `Authorization: Bearer <token>`
- **Query Parameters**:
  - `page` (optional): Page number (default: 1)
  - `page_size` (optional): Items per page (default: 10)
  - `search` (optional): Filter by `name` or `description` (partial, case-insensitive)
  - `ordering` (optional): Sort by `name`, `created_at`, `updated_at`, `duration_minutes` (prefix `-` for descending; default `-created_at`)
  - `is_active` (optional): Boolean filter
  - `assignment_strategy` (optional): One of `COLLECTIVE`, `DIRECT`
  - `location_type` (optional): One of `IN_PERSON`, `PHONE`, `VIDEO`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "name": "30-Min Demo",
        "duration_minutes": 30,
        "assignment_strategy": "DIRECT",
        "location_type": "VIDEO",
        "is_active": true,
        "created_at": "2026-06-03T18:00:00Z"
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

---

### 2. Get Dropdown List
- **Path**: `GET /api/v1/core/event-templates/dropdown/`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [ { "label": "30-Min Demo", "value": "3fa85f64-5717-4562-b3fc-2c963f66afa6" } ],
    "errors": null
  }
  ```

---

### 3. Retrieve Event Template
- **Path**: `GET /api/v1/core/event-templates/{id}/`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Retrieved successfully",
    "data": {
      "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "tenant": { "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c", "name": "Alpha Corp", "subdomain": "alpha-corp" },
      "name": "30-Min Demo",
      "description": "Product walkthrough with a specialist.",
      "duration_minutes": 30,
      "buffer_before_minutes": 5,
      "buffer_after_minutes": 5,
      "assignment_strategy": "DIRECT",
      "min_notice_minutes": 120,
      "max_horizon_days": 60,
      "slot_granularity_minutes": 15,
      "cancellation_lead_minutes": 60,
      "reschedule_lead_minutes": 60,
      "location_type": "VIDEO",
      "color": "#0284C7",
      "is_active": true,
      "created_by": { "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "name": "Emma" },
      "updated_by": null,
      "deleted_by": null,
      "created_at": "2026-06-03T18:00:00Z",
      "updated_at": "2026-06-03T18:00:00Z",
      "deleted_at": null
    },
    "errors": null
  }
  ```

---

### 4. Create Event Template
- **Path**: `POST /api/v1/core/event-templates/`
- **Request Payload**:
  ```json
  {
    "name": "30-Min Demo",
    "description": "Product walkthrough with a specialist.",
    "duration_minutes": 30,
    "buffer_before_minutes": 5,
    "buffer_after_minutes": 5,
    "assignment_strategy": "DIRECT",
    "min_notice_minutes": 120,
    "max_horizon_days": 60,
    "slot_granularity_minutes": 15,
    "cancellation_lead_minutes": 60,
    "reschedule_lead_minutes": 60,
    "location_type": "VIDEO",
    "color": "#0284C7"
  }
  ```
### Request fields

`*` marks required. Everything else has a default.

| Field | Type | Description | Validation |
|---|---|---|---|
| `name` * | string | What this meeting type is called, shown to bookers and used as the booking title. | 2–100 chars. Letters, numbers, spaces, hyphens, underscores only. **Unique per tenant** among non-deleted templates. |
| `duration_minutes` * | integer | How long the meeting runs. Directly sets each slot's `end - start`. | ≥ 1. Also enforced by a database check constraint. |
| `description` | string | Free text explaining what the meeting is for. | Any length. Default `""`. |
| `buffer_before_minutes` | integer | Protected padding **before** the meeting — travel, prep, notes. | ≥ 0, default `0`. Counts toward double-booking checks but is never shown to the customer. |
| `buffer_after_minutes` | integer | Protected padding **after** the meeting. | ≥ 0, default `0`. Same semantics as above. |
| `assignment_strategy` | enum | How the host is chosen. `DIRECT` — the booker names one host. `COLLECTIVE` — every skilled host is booked together as one meeting. | Default `DIRECT`. `ROUND_ROBIN` was removed; sending it is a **400**. |
| `min_notice_minutes` | integer | How far ahead a booking must be made. Stops someone booking a slot starting in two minutes. | ≥ 0, default `120`. |
| `max_horizon_days` | integer | How far into the future bookings are accepted. | **1–365**, default `60`. Enforced by validator *and* database constraint. |
| `slot_granularity_minutes` | integer | Step between candidate start times: `15` offers :00/:15/:30/:45. | **≥ 5**, default `15`. Enforced by validator *and* database constraint. |
| `cancellation_lead_minutes` | integer | Minimum notice a customer must give to cancel. Operators can override with `DELETE`. | ≥ 0, default `60`. |
| `reschedule_lead_minutes` | integer | Minimum notice required to reschedule. | ≥ 0, default `60`. |
| `location_type` | enum | Where the meeting happens: `IN_PERSON`, `PHONE`, `VIDEO`. | Default `VIDEO`. Descriptive only — no conferencing link is generated. |
| `color` | string | Hex colour for UI rendering, e.g. `#0284C7`. | Max 7 chars. **Stored as-is — the format is not validated**, so `purple` is accepted and will not render. |
| `is_active` | boolean | Whether this type can be booked. `false` keeps the template and its history but removes it from availability and booking. | Default `true`. |

`id`, `tenant`, `created_by`/`updated_by`/`deleted_by`, and all timestamps are **read-only**.

#### Why `max_horizon_days` and `slot_granularity_minutes` are bounded

Slot generation costs roughly `days × minutes_per_day ÷ granularity × hosts`, and it runs
**synchronously inside the request**. The raw field types would allow `max_horizon_days = 32767`
with a 1-minute granularity — about 47 million iterations per host, per request, reachable by a
tenant through the ordinary API against their own data. The bounds keep that product finite, and
they exist in the database as well as the serializer so a management command or data fix cannot
route around them.

Raising `max_horizon_days` also extends how far ahead availability is materialized, automatically,
within the same 365-day ceiling.

### Responses

| Status | When | `message` |
|---|---|---|
| **201** | Created. Body is the full template. | `Created successfully` |
| **400** | Name already used by an active template in this tenant. | `An event template with this name already exists.` |
| **400** | `max_horizon_days` outside 1–365. | `Ensure this value is less than or equal to 365.` |
| **400** | `slot_granularity_minutes` below 5. | `Ensure this value is greater than or equal to 5.` |
| **400** | `duration_minutes` below 1, or any field-level failure. | the field's message |
| **400** | `assignment_strategy` not `DIRECT` or `COLLECTIVE`. | `"…" is not a valid choice.` |
| **400** | Caller's account has no tenant mapping. | `User is not associated with an active tenant.` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | Agent (`VB_AGENT`) token — creation is human-only configuration. | permission denied |

```json
{
  "status": false,
  "message": "An event template with this name already exists.",
  "data": null,
  "errors": { "name": ["An event template with this name already exists."] },
  "request_id": "a3f1…"
}
```

---

### 5. Update / Patch Event Template

**`PUT` / `PATCH /api/v1/core/event-templates/{id}/`**

Same fields and validation as Create; on `PATCH` all are optional.

**Changes apply to future bookings only.** Existing bookings keep the duration and buffers they
were created with — `effective_start_time`/`effective_end_time` were frozen at creation, so
widening a buffer does not retroactively reserve more of the host's calendar.

| Status | When | `message` |
|---|---|---|
| **200** | Updated. Body is the full template. | `Updated successfully` |
| **400** | Same table as Create. | — |
| **403** | Agent token. | — |
| **404** | No such template in your tenant. | — |

> Setting `is_active: false` takes effect immediately: the template disappears from
> `available-slots` and new bookings are refused with a `400`. Bookings already made are untouched.

---

### 6. Delete Event Template (Soft Delete)

**`DELETE /api/v1/core/event-templates/{id}/`**

Soft-delete: the row is kept so historical bookings still resolve their event type, and the name is
suffixed with a deletion timestamp so it becomes immediately reusable.

| Status | When | `message` |
|---|---|---|
| **200** | Deleted. | `Deleted successfully` |
| **400** | The template still has active bookings. **Nothing is deleted.** | `This event type has 3 active booking(s) and cannot be deleted. Cancel or complete them first, or set is_active=false to stop new bookings while honouring the ones already made.` |
| **403** | Agent token. | — |
| **404** | No such template in your tenant. | — |

The guard exists because deletion is *soft*: the database's `PROTECT` foreign key never fires, so
without it customers would be left holding confirmed bookings for a meeting type that no longer
exists. It counts the blocking bookings so you know how much work clearing them is.

"Active" means `HELD`, `SCHEDULED`, `CONFIRMED`, or `REQUIRES_RESCHEDULE`. Cancelled, completed,
and no-show bookings do not block deletion.

**Three ways forward when blocked:**

| Situation | Do this |
|---|---|
| The type is genuinely retired, but existing bookings should still happen | `PATCH {"is_active": false}` — removes it from availability and refuses new bookings, while every booking already made proceeds normally |
| The bookings should not happen either | Cancel them (`POST /bookings/{id}/cancel/`), then delete |
| The meetings already happened | Mark them `complete`/`no-show`, then delete |

The guard is enforced on the **model**, so `bulk_delete` and direct ORM use are covered too — not
just this endpoint.

---

### 7. Bulk Delete

**`POST /api/v1/core/event-templates/bulk_delete/`**

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Templates to soft-delete. | Non-empty list; every id must exist, be undeleted, and be in your tenant. All-or-nothing. |

| Status | When | `message` |
|---|---|---|
| **200** | All soft-deleted. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | Any id unknown, already deleted, or in another tenant; **nothing deleted**. | `Some ids are invalid` |
| **400** | Any template still has active bookings; **nothing deleted**. | `This event type has 3 active booking(s) and cannot be deleted. …` |

The active-bookings guard applies here exactly as it does to `DELETE /{id}/` — it lives on the
model, so every deletion path enforces it. The whole request runs in one transaction, so a single
blocked template leaves every other id in the list untouched rather than half-deleting the batch.

---

## What this configuration drives

| Field | Where it takes effect |
|---|---|
| `duration_minutes`, `slot_granularity_minutes` | which start times [available-slots](./availability_slots.md) offers |
| `buffer_*_minutes` | the exclusion constraint that prevents double-booking |
| `min_notice_minutes`, `max_horizon_days` | which slots survive filtering, and the `400` on booking too soon or too far ahead |
| `max_horizon_days` | how far ahead availability is materialized |
| `cancellation_lead_minutes` | the `400` on a late cancellation |
| `assignment_strategy` | whether `target_host` is required or forbidden, and whether one booking or a group is created |
| `is_active` | whether the type appears in availability or accepts bookings at all |

## Related

- [Event Skill API](./event_skill.md) — which hosts may take each type
- [Available Slots API](./availability_slots.md)
- [Booking API](./booking.md)

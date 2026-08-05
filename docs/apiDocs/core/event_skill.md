# User Event Skill API Documentation

The routing table: **which people are allowed to host which meeting types.**

One row links one user to one event template. That link is the *only* thing that makes someone a
candidate host — availability, calendars, and working hours are all irrelevant without it.

Three consequences worth knowing before you start:

- **A template with no skills cannot be booked at all.** `available-slots` returns an empty list
  with no error, which is the single most common "why is nothing showing up" cause.
- **Booking checks this at both ends.** A `target_host` without a matching skill is rejected by the
  serializer *and* by the booking service, so an unchecked id can never route a booking to an
  arbitrary user — including one in another tenant.
- **For `COLLECTIVE` templates this set defines the meeting.** Every skilled host is booked
  together, so adding a skill enlarges every future collective booking of that type.

> **VB_AGENT access:** read-only — `list`, `retrieve`, `dropdown`. Who is eligible to host is
> human configuration; writes return **403**. Agent tokens **must** send
> `X-Tenant-ID: <tenant uuid>` (**400** if omitted).
> See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

## Endpoints Summary
- `GET /api/v1/core/event-skills/` - List host↔event-type links (paginated)
- `GET /api/v1/core/event-skills/{id}/` - Retrieve a link
- `POST /api/v1/core/event-skills/` - Link a host to an event type
- `PUT` / `PATCH /api/v1/core/event-skills/{id}/` - Update a link
- `DELETE /api/v1/core/event-skills/{id}/` - Soft-delete a link
- `GET /api/v1/core/event-skills/dropdown/` - Get label/value list
- `POST /api/v1/core/event-skills/bulk_delete/` - Bulk soft-delete links

---

## Endpoint Details

### 1. List Event Skills
- **Path**: `GET /api/v1/core/event-skills/`
- **Headers**: `Authorization: Bearer <token>`
- **Query Parameters**:
  - `page`, `page_size` (optional): Pagination.
  - `user` (optional): Filter by host user id.
  - `event_template` (optional): Filter by event template id.
  - `is_active` (optional): Boolean filter.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "9aa85f64-5717-4562-b3fc-2c963f66afa1",
        "user": {
          "id": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
          "email": "alex@acme.com",
          "first_name": "Alex",
          "last_name": "Host"
        },
        "event_template": { "id": "3fa85f64-…", "name": "30-Min Demo", "color": "#0284C7" },
        "is_active": true,
        "created_at": "2026-06-03T18:00:00Z"
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

---

### 2. Create Event Skill
- **Path**: `POST /api/v1/core/event-skills/`
- **Request Payload**:
  ```json
  {
    "user": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
    "event_template": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
  }
  ```
### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `user` * | UUID | The person who may host this meeting type. | Must belong to your tenant. |
| `event_template` * | UUID | The meeting type they are qualified for. | Must belong to your tenant. |
| `is_active` | boolean | Whether this link counts. `false` removes the host from this type without losing the record. | Default `true`. |

A `(user, event_template)` pair is **unique per tenant** among non-deleted rows — creating a
duplicate is a `400`. To re-enable a host you previously turned off, `PATCH` the existing row's
`is_active` back to `true` rather than creating a second link.

> **Linking a host does not make them bookable on its own.** They also need an
> [Availability Profile](./availability_profile.md) with rules; without one they produce zero slots
> and, again, no error.
- **Success Response (201 Created)**:
  ```json
  {
    "status": true,
    "message": "Created successfully",
    "data": {
      "id": "9aa85f64-5717-4562-b3fc-2c963f66afa1",
      "tenant": { "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c", "name": "Alpha Corp" },
      "user": {
        "id": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
        "email": "alex@acme.com",
        "first_name": "Alex",
        "last_name": "Host"
      },
      "event_template": { "id": "3fa85f64-…", "name": "30-Min Demo", "color": "#0284C7" },
      "is_active": true,
      "created_at": "2026-06-03T18:00:00Z",
      "updated_at": "2026-06-03T18:00:00Z",
      "deleted_at": null
    },
    "errors": null
  }
  ```

> **Write with an id, read back an object.** `user` and `event_template` accept a bare UUID on
> write but are returned **expanded** — do not assume the value you sent is the shape you get back.
>
> They also return **`null` if the related row has been soft-deleted**. A skill whose user was
> deleted comes back as `"user": null` rather than disappearing, so guard against nulls when
> rendering.
### Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **201** | Link created; the host becomes a candidate immediately. | `Created successfully` | `null` |
| **400** | `user` belongs to another tenant. | `User does not belong to your tenant.` | `{"user": [...]}` |
| **400** | `event_template` belongs to another tenant. | `Event template does not belong to your tenant.` | `{"event_template": [...]}` |
| **400** | This pair is already linked and active. | uniqueness message | non-field |
| **400** | Missing `user` or `event_template`. | `This field is required.` | `{"<field>": [...]}` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": ...}` |
| **403** | Agent (`VB_AGENT`) token — this is human configuration. | permission denied | `{"detail": ...}` |

---

### 3. Update / Delete

**`PUT` / `PATCH /api/v1/core/event-skills/{id}/`** — in practice only `is_active` is worth
changing; repointing `user` or `event_template` is better expressed as delete + create.

**`DELETE /api/v1/core/event-skills/{id}/`** — soft-delete.

| Status | When | `message` |
|---|---|---|
| **200** | Updated / deleted. | `Updated successfully` / `Deleted successfully` |
| **400** | Validation failed (update only). | as above |
| **404** | No such link in your tenant. | — |

**Existing bookings are unaffected.** Removing a skill stops *new* bookings from routing to that
host; it does not cancel or reassign anything already booked. Two follow-on effects:

- A customer holding a reschedule link for that host will get `400 "The requested host is not
  configured to host this event type."` when they submit — the rebook keeps the original host.
- For a `COLLECTIVE` template, future bookings will include one fewer host; bookings already made
  keep the group they were created with.

---

### 4. Dropdown
- **Path**: `GET /api/v1/core/event-skills/dropdown/`
- **Query Parameters**: `limit` (optional, ≤ 1000) + the same `user` / `event_template` / `is_active` filters as List.
- **Note**: For this resource the dropdown `label` equals the row id (i.e. `label == value`).
- **Success Response (200 OK)**:
  ```json
  { "status": true, "message": "Dropdown options retrieved successfully", "data": [ { "label": "9aa85f64-...", "value": "9aa85f64-..." } ], "errors": null }
  ```

---

### 5. Bulk Delete

**`POST /api/v1/core/event-skills/bulk_delete/`**

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Links to soft-delete. | Non-empty list; every id must exist, be undeleted, and be in your tenant. All-or-nothing. |

| Status | When | `message` |
|---|---|---|
| **200** | All soft-deleted. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | Any id unknown, already deleted, or in another tenant; **nothing deleted**. | `Some ids are invalid` |

---

## Setting a host up end to end

All four are required before anyone can book:

| # | Step | Endpoint |
|---|---|---|
| 1 | Create the meeting type | [`POST /event-templates/`](./event_template.md) |
| 2 | **Link the host to it** | `POST /event-skills/` ← this page |
| 3 | Give the host working hours | [`POST /availability-profiles/`](./availability_profile.md) |
| 4 | *(optional)* Connect their calendar | [`GET /calendar-integrations/connect/`](./calendar_integration.md) |

Miss step 2 or 3 and [`available-slots`](./availability_slots.md) returns an empty list with a
`200` — no error explains which step is missing, so check both.

## Related

- [Event Template API](./event_template.md)
- [Availability Profile API](./availability_profile.md)
- [Setup flow](../../flows/01-setup.md)

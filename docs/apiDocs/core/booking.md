# Booking API Documentation

Create and manage appointments, scoped to the authenticated tenant. Bookings are created
and mutated only through these endpoints (never direct field edits), so `PUT`/`PATCH` are
disabled. Double-booking is prevented at the database level — a taken slot returns
**409 Conflict**.

**Statuses:** `HELD`, `SCHEDULED`, `CONFIRMED`, `COMPLETED`, `CANCELED`, `NO_SHOW`,
`REQUIRES_RESCHEDULE`, `RESCHEDULED`.

## Endpoints Summary
- `GET /api/v1/core/bookings/` - List bookings (paginated)
- `GET /api/v1/core/bookings/{id}/` - Retrieve a booking
- `POST /api/v1/core/bookings/` - Create a booking
- `POST /api/v1/core/bookings/hold/` - Temporarily reserve a slot
- `POST /api/v1/core/bookings/{id}/confirm/` - Confirm a held booking
- `POST /api/v1/core/bookings/{id}/cancel/` - Cancel (lead-time enforced)
- `POST /api/v1/core/bookings/{id}/complete/` - Mark completed
- `POST /api/v1/core/bookings/{id}/no-show/` - Mark no-show
- `DELETE /api/v1/core/bookings/{id}/` - Cancel (internal; bypasses lead-time)

---

## Endpoint Details

### 1. List Bookings
- **Path**: `GET /api/v1/core/bookings/`
- **Headers**: `Authorization: Bearer <token>`
- **Query Parameters**: `page`, `page_size`; filters `status`, `primary_host`, `event_template`, `source`; `ordering` by `start_time`, `created_at`.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "4cc85f64-5717-4562-b3fc-2c963f66af30",
        "event_template": { "id": "3fa8...", "name": "30-Min Demo" },
        "title": "30-Min Demo",
        "primary_host": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
        "start_time": "2026-07-05T14:00:00Z",
        "end_time": "2026-07-05T14:30:00Z",
        "status": "SCHEDULED",
        "created_at": "2026-07-01T10:00:00Z"
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

---

### 2. Retrieve Booking
- **Path**: `GET /api/v1/core/bookings/{id}/`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Retrieved successfully",
    "data": {
      "id": "4cc85f64-5717-4562-b3fc-2c963f66af30",
      "tenant": { "id": "7bf2...", "name": "Alpha Corp" },
      "event_template": { "id": "3fa8...", "name": "30-Min Demo" },
      "title": "30-Min Demo",
      "primary_host": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
      "start_time": "2026-07-05T14:00:00Z",
      "end_time": "2026-07-05T14:30:00Z",
      "status": "SCHEDULED",
      "source": "INTERNAL",
      "hold_expires_at": null,
      "reschedule_token": null,
      "reschedule_token_expires_at": null,
      "external_event_id": null,
      "external_provider": null,
      "group_id": null,
      "cancellation_reason": "",
      "participants": [
        {
          "id": "p1", "user": "5fa8...", "name": "Alex Host",
          "email": "alex@acme.com", "phone_number": null,
          "role": "HOST", "response_status": "NEEDS_ACTION"
        },
        {
          "id": "p2", "user": null, "name": "Jamie Customer",
          "email": "jamie@example.com", "phone_number": "+15550001111",
          "role": "ATTENDEE", "response_status": "NEEDS_ACTION"
        }
      ],
      "created_by": { "id": "3fa8...", "name": "Emma" },
      "updated_by": null,
      "deleted_by": null,
      "created_at": "2026-07-01T10:00:00Z",
      "updated_at": "2026-07-01T10:00:00Z",
      "deleted_at": null
    },
    "errors": null
  }
  ```

---

### 3. Create Booking
- **Path**: `POST /api/v1/core/bookings/`
- **Request Payload**:
  ```json
  {
    "event_template": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "start_time": "2026-07-05T14:00:00Z",
    "target_host": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
    "participants": [
      { "name": "Jamie Customer", "email": "jamie@example.com", "phone_number": "+15550001111", "role": "ATTENDEE" }
    ]
  }
  ```
- **Field Validations**:
  - `event_template`: Required. Must belong to the caller's tenant.
  - `start_time`: Required, UTC-aware. Must satisfy the template's min-notice and horizon; not in the past.
  - `target_host`: Required for `DIRECT` templates; ignored for `COLLECTIVE`; optional for `ROUND_ROBIN` (auto-selected otherwise).
  - `participants`: Required, at least one. Each: `email` (required), `name`, `phone_number`, `role` (`ATTENDEE` or `GUEST`). The host is added automatically.
- **Success Response (201 Created)** — `ROUND_ROBIN` / `DIRECT` (single booking):
  ```json
  { "status": true, "message": "Booked successfully", "data": { "id": "4cc8...", "status": "SCHEDULED", "...": "full booking object" }, "errors": null }
  ```
- **Success Response (201 Created)** — `COLLECTIVE` (one booking per host, shared `group_id`):
  ```json
  {
    "status": true,
    "message": "Collective booking created",
    "data": [
      { "id": "b1", "primary_host": "host-1", "group_id": "g-123", "status": "SCHEDULED" },
      { "id": "b2", "primary_host": "host-2", "group_id": "g-123", "status": "SCHEDULED" }
    ],
    "errors": null
  }
  ```
- **Error Responses**:
  - `409 Conflict` — the slot is taken (for any required host):
    ```json
    { "status": false, "message": "The selected time is no longer available.", "data": null, "errors": null }
    ```
  - `400 Bad Request` — policy violation (past / min-notice / horizon):
    ```json
    { "status": false, "message": "This event requires at least 120 minutes notice.", "data": null, "errors": { "start_time": ["This event requires at least 120 minutes notice."] } }
    ```
  - `400 Bad Request` — no eligible host, or `target_host` missing for a `DIRECT` template.

---

### 4. Hold a Slot
- **Path**: `POST /api/v1/core/bookings/hold/`
- **Request Payload**: Same as Create.
- **Behaviour**: Creates a `HELD` booking with `hold_expires_at` (default 5 min); the slot is blocked for everyone else until confirmed or the hold lapses (auto-released by a background job).
- **Success Response (201 Created)**: `{ "status": true, "message": "Slot held successfully", "data": { "status": "HELD", "hold_expires_at": "...", "..." : "..." } }`
- **Error Responses**: `409` slot taken, `400` policy / no host.

---

### 5. Confirm a Hold
- **Path**: `POST /api/v1/core/bookings/{id}/confirm/`
- **Success Response (200 OK)**: `{ "status": true, "message": "Booking confirmed", "data": { "status": "CONFIRMED", "hold_expires_at": null, "...": "..." } }`
- **Error Responses**:
  - `409 Conflict` — the hold has expired: `{ "status": false, "message": "This hold has expired." }`
  - `400 Bad Request` — the booking isn't in `HELD` state.

---

### 6. Cancel a Booking
- **Path**: `POST /api/v1/core/bookings/{id}/cancel/`
- **Request Payload** (optional):
  ```json
  { "reason": "Customer requested cancellation" }
  ```
- **Success Response (200 OK)**: `{ "status": true, "message": "Booking canceled", "data": { "status": "CANCELED", "...": "..." } }`
- **Error Response (400 Bad Request)** — inside the cancellation lead-time window:
  ```json
  { "status": false, "message": "This booking can no longer be canceled (requires 60 minutes notice).", "data": null, "errors": { "start_time": ["This booking can no longer be canceled (requires 60 minutes notice)."] } }
  ```
- **Note**: For a `COLLECTIVE` booking, cancelling any row cancels the whole meeting.

---

### 7. Complete / No-Show
- **Path**: `POST /api/v1/core/bookings/{id}/complete/` → `COMPLETED` (`message: "Booking completed"`).
- **Path**: `POST /api/v1/core/bookings/{id}/no-show/` → `NO_SHOW` (`message: "Booking marked as no-show"`).
- **Error Response (400)**: attempting an illegal transition (e.g. from a terminal or held state).

---

### 8. Delete (Force Cancel)
- **Path**: `DELETE /api/v1/core/bookings/{id}/`
- Cancels the booking (with notification and calendar cleanup) **bypassing** the lead-time check. For a `COLLECTIVE` booking it cancels the whole group.
- **Success Response (200 OK)**: `{ "status": true, "message": "Deleted successfully", "data": null, "errors": null }`

---

> Side effects on create/confirm/cancel: confirmation / cancellation emails with `.ics`,
> and (if the host has a connected calendar) the event is mirrored / removed. See the
> [Booking lifecycle flow](../../flows/02-booking-lifecycle.md).

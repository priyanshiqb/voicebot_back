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
      "tenant": { "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c", "name": "Alpha Corp" },
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
- **Field Validations**:
  - `name`: Required, 2-100 chars, letters/numbers/spaces/hyphens/underscores only. Unique per tenant among active templates.
  - `description`: Optional text.
  - `duration_minutes`: Required, integer >= 1.
  - `buffer_before_minutes` / `buffer_after_minutes`: Optional, integer >= 0 (default 0). Counted in overlap/double-booking checks.
  - `assignment_strategy`: Optional (default `DIRECT`). One of `DIRECT` (booker names the host) or `COLLECTIVE` (books all skilled hosts as one meeting). `ROUND_ROBIN` was removed — sending it returns `400`.
  - `min_notice_minutes`: Optional (default 120). Earliest a slot may be booked from now.
  - `max_horizon_days`: Optional (default 60). Furthest a slot may be booked.
  - `slot_granularity_minutes`: Optional (default 15), integer >= 1. Step between candidate slots.
  - `cancellation_lead_minutes` / `reschedule_lead_minutes`: Optional (default 60). Minimum notice to cancel/reschedule.
  - `location_type`: Optional (default `VIDEO`). One of `IN_PERSON`, `PHONE`, `VIDEO`.
  - `color`: Optional, string up to 7 chars (e.g. a hex color like `#0284C7`). Stored as-is — **not** format-validated.
  - `is_active`: Optional (default `true`). Writable — set `false` to keep the template but hide it from booking.
  - `tenant`, audit fields, `id`, timestamps: Read-only.
- **Success Response (201 Created)**: Full template object (see Retrieve), `message: "Created successfully"`.
- **Error Response (400 Bad Request)**:
  ```json
  {
    "status": false,
    "message": "An event template with this name already exists.",
    "data": null,
    "errors": { "name": ["An event template with this name already exists."] }
  }
  ```

---

### 5. Update / Patch Event Template
- **Path**: `PUT` / `PATCH /api/v1/core/event-templates/{id}/`
- **Request Payload**: Same fields/validations as Create.
- **Success Response (200 OK)**: Updated template object, `message: "Updated successfully"`.

---

### 6. Delete Event Template (Soft Delete)
- **Path**: `DELETE /api/v1/core/event-templates/{id}/`
- **Success Response (200 OK)**:
  ```json
  { "status": true, "message": "Deleted successfully", "data": null, "errors": null }
  ```
- **Error Response (400 Bad Request)** — the template has active bookings:
  ```json
  {
    "status": false,
    "message": "This event type has active bookings and cannot be deleted.",
    "data": null,
    "errors": { "detail": "This event type has active bookings and cannot be deleted." }
  }
  ```

---

### 7. Bulk Delete
- **Path**: `POST /api/v1/core/event-templates/bulk_delete/`
- **Request Payload**: `{ "ids": ["3fa85f64-5717-4562-b3fc-2c963f66afa6", "..."] }`
- **Behaviour**: Soft-deletes all listed templates (tenant-scoped). If any id is unknown or not
  in your tenant, the whole request fails with `400 "Some ids are invalid"` and nothing is deleted.
- **Success Response (200 OK)**: `{ "status": true, "message": "Bulk deleted successfully" }`

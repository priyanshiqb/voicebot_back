# User Event Skill API Documentation

Defines which users (hosts) are allowed to host which event templates, scoped to the
authenticated tenant. A template with no skills cannot be booked.

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
        "user": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
        "event_template": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "weight": 1,
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
    "event_template": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "weight": 1
  }
  ```
- **Field Validations**:
  - `user`: Required. Must belong to the caller's tenant.
  - `event_template`: Required. Must belong to the caller's tenant.
  - `weight`: Optional (default 1). Relative bias for round-robin selection (higher = chosen more often).
  - `is_active`: Optional (default `true`). Writable on create/update.
  - A `(user, event_template)` pair is unique per tenant among active rows.
- **Success Response (201 Created)**:
  ```json
  {
    "status": true,
    "message": "Created successfully",
    "data": {
      "id": "9aa85f64-5717-4562-b3fc-2c963f66afa1",
      "tenant": { "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c", "name": "Alpha Corp" },
      "user": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
      "event_template": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "weight": 1,
      "is_active": true,
      "created_at": "2026-06-03T18:00:00Z",
      "updated_at": "2026-06-03T18:00:00Z",
      "deleted_at": null
    },
    "errors": null
  }
  ```
- **Error Response (400 Bad Request)**:
  ```json
  {
    "status": false,
    "message": "User does not belong to your tenant.",
    "data": null,
    "errors": { "user": ["User does not belong to your tenant."] }
  }
  ```

---

### 3. Update / Delete
- **Path**: `PUT` / `PATCH /api/v1/core/event-skills/{id}/` — update `weight` / `is_active`.
- **Path**: `DELETE /api/v1/core/event-skills/{id}/` — soft-delete → `{ "status": true, "message": "Deleted successfully" }`.

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
- **Path**: `POST /api/v1/core/event-skills/bulk_delete/`
- **Request Payload**: `{ "ids": ["9aa85f64-5717-4562-b3fc-2c963f66afa1", "..."] }`
- **Behaviour**: Soft-deletes all listed links (tenant-scoped). If any id is unknown/not in your
  tenant, the whole request fails with `400 "Some ids are invalid"` and nothing is deleted.
- **Success Response (200 OK)**: `{ "status": true, "message": "Bulk deleted successfully" }`

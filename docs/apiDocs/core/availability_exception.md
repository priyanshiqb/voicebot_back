# Availability Exception API Documentation

Log time off / leave / emergency absence for a user, scoped to the authenticated tenant.
**Creating an exception triggers the reschedule workflow**: any of that user's future
active bookings that overlap the window are auto-reassigned (ROUND_ROBIN, when another
host is free) or flagged `REQUIRES_RESCHEDULE` with a customer reschedule link.

## Endpoints Summary
- `GET /api/v1/core/availability-exceptions/` - List exceptions (paginated)
- `GET /api/v1/core/availability-exceptions/{id}/` - Retrieve an exception
- `POST /api/v1/core/availability-exceptions/` - Log leave (triggers reschedule)
- `PUT` / `PATCH /api/v1/core/availability-exceptions/{id}/` - Update an exception
- `DELETE /api/v1/core/availability-exceptions/{id}/` - Soft-delete an exception

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
        "user": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
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
- **Field Validations**:
  - `user`: Required. Must belong to the caller's tenant.
  - `start_time` / `end_time`: Required, UTC-aware. `end_time` must be after `start_time`.
  - `type`: Optional (default `OTHER`). One of `VACATION`, `SICK`, `EMERGENCY`, `HOLIDAY`, `OTHER`.
  - `reason`: Optional text.
  - `is_all_day`: Optional boolean (default `false`).
- **Side Effect**: On creation, the reschedule engine runs asynchronously for the user's
  overlapping future `SCHEDULED`/`CONFIRMED` bookings. See
  [Leave & Reschedule flow](../../flows/03-leave-and-reschedule.md).
- **Success Response (201 Created)**:
  ```json
  {
    "status": true,
    "message": "Created successfully",
    "data": {
      "id": "8aa85f64-5717-4562-b3fc-2c963f66af20",
      "tenant": { "id": "7bf2...", "name": "Alpha Corp" },
      "user": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
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
- **Error Response (400 Bad Request)**:
  ```json
  {
    "status": false,
    "message": "end_time must be after start_time.",
    "data": null,
    "errors": { "end_time": ["end_time must be after start_time."] }
  }
  ```

---

### 3. Update / Delete
- **Path**: `PUT` / `PATCH /api/v1/core/availability-exceptions/{id}/` — update the window/type.
- **Path**: `DELETE /api/v1/core/availability-exceptions/{id}/` — soft-delete.

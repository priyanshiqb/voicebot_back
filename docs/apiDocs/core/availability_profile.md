# Availability Profile API Documentation

Per-user weekly working hours, scoped to the authenticated tenant. Each profile has a
timezone and a set of weekly **rules** (blocks). Multiple rules per weekday allow split
shifts. Slot generation resolves these wall-clock times through the profile timezone
(DST-correct).

## Endpoints Summary
- `GET /api/v1/core/availability-profiles/` - List profiles (paginated)
- `GET /api/v1/core/availability-profiles/{id}/` - Retrieve a profile (with rules)
- `POST /api/v1/core/availability-profiles/` - Create a profile (with rules)
- `PUT` / `PATCH /api/v1/core/availability-profiles/{id}/` - Update a profile (rules replaced)
- `DELETE /api/v1/core/availability-profiles/{id}/` - Soft-delete a profile
- `GET /api/v1/core/availability-profiles/dropdown/` - Get label/value list
- `POST /api/v1/core/availability-profiles/bulk_delete/` - Bulk soft-delete profiles

---

## Endpoint Details

### 1. List Availability Profiles
- **Path**: `GET /api/v1/core/availability-profiles/`
- **Headers**: `Authorization: Bearer <token>`
- **Query Parameters**: `page`, `page_size`, `user` (filter), `is_active` (filter).
- **Success Response (200 OK)**:
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
        "timezone": {
          "id": "2b1c...-timezone-id",
          "timezone": "UTC",
          "offset": "+00:00"
        },
        "is_active": true,
        "created_at": "2026-06-03T18:00:00Z"
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

---

### 2. Retrieve Availability Profile
- **Path**: `GET /api/v1/core/availability-profiles/{id}/`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Retrieved successfully",
    "data": {
      "id": "1aa85f64-5717-4562-b3fc-2c963f66af10",
      "tenant": { "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c", "name": "Alpha Corp" },
      "user": {
        "id": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
        "email": "agent@example.com",
        "first_name": "Jane",
        "last_name": "Doe"
      },
      "timezone": {
        "id": "2b1c...-timezone-id",
        "timezone": "UTC",
        "offset": "+00:00"
      },
      "name": "Default",
      "rules": [
        { "id": "r1", "weekday": 0, "start_time": "09:00:00", "end_time": "12:00:00" },
        { "id": "r2", "weekday": 0, "start_time": "14:00:00", "end_time": "17:00:00" }
      ],
      "is_active": true,
      "created_by": { "id": "3fa8...", "name": "Emma" },
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

### 3. Create Availability Profile
- **Path**: `POST /api/v1/core/availability-profiles/`
- **Request Payload**:
  ```json
  {
    "user": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
    "timezone": "2b1c...-timezone-id",
    "name": "Default",
    "rules": [
      { "weekday": 0, "start_time": "09:00:00", "end_time": "17:00:00" },
      { "weekday": 1, "start_time": "09:00:00", "end_time": "17:00:00" }
    ]
  }
  ```
- **Field Validations**:
  - `user`: Required. Must belong to the caller's tenant. One active profile per user.
  - `timezone`: Required. FK to a `Timezone` id (IANA zone).
  - `name`: Optional (default "Default").
  - `rules`: Optional list. Each rule: `weekday` 0-6 (0 = Monday), `start_time` and `end_time` (`HH:MM:SS`), `end_time` > `start_time`. Rules on the same weekday must not overlap.
  - `is_active`: Optional (default `true`). Writable on create/update.
- **Success Response (201 Created)**: Full profile object (see Retrieve), `message: "Created successfully"`.
- **Error Response (400 Bad Request)**:
  ```json
  {
    "status": false,
    "message": "Overlapping availability rules on weekday 0.",
    "data": null,
    "errors": { "rules": ["Overlapping availability rules on weekday 0."] }
  }
  ```

---

### 4. Update / Patch
- **Path**: `PUT` / `PATCH /api/v1/core/availability-profiles/{id}/`
- Sending `rules` **replaces** the profile's existing rules. Omitting `rules` leaves them unchanged.
- **Success Response (200 OK)**: Updated profile, `message: "Updated successfully"`.

---

### 5. Delete (Soft Delete)
- **Path**: `DELETE /api/v1/core/availability-profiles/{id}/` → `{ "status": true, "message": "Deleted successfully" }`.

---

### 6. Dropdown
- **Path**: `GET /api/v1/core/availability-profiles/dropdown/`
- **Query Parameters**: `limit` (optional, ≤ 1000) + the same `user` / `is_active` filters as List.
- **Success Response (200 OK)**: `{ "status": true, "message": "Dropdown options retrieved successfully", "data": [ { "label": "...", "value": "<uuid>" } ], "errors": null }`

---

### 7. Bulk Delete
- **Path**: `POST /api/v1/core/availability-profiles/bulk_delete/`
- **Request Payload**: `{ "ids": ["1aa85f64-5717-4562-b3fc-2c963f66af10", "..."] }`
- **Behaviour**: Soft-deletes all listed profiles (tenant-scoped). If any id is unknown/not in
  your tenant, the whole request fails with `400 "Some ids are invalid"` and nothing is deleted.
- **Success Response (200 OK)**: `{ "status": true, "message": "Bulk deleted successfully" }`

# Device Token API Documentation

Register a device's FCM token to receive push notifications (internal-team alerts such as
booking reassignment or needs-reschedule), scoped to the authenticated tenant. Users only
see and manage their own tokens.

## Endpoints Summary
- `GET /api/v1/core/device-tokens/` - List the caller's device tokens
- `POST /api/v1/core/device-tokens/` - Register (upsert) a device token
- `POST /api/v1/core/device-tokens/deactivate/` - Deactivate a device token
- `DELETE /api/v1/core/device-tokens/{id}/` - Soft-delete a device token
- `GET /api/v1/core/device-tokens/dropdown/` - Get label/value list (caller's tokens)
- `POST /api/v1/core/device-tokens/bulk_delete/` - Bulk soft-delete the caller's tokens

---

## Endpoint Details

### 1. List Device Tokens
- **Path**: `GET /api/v1/core/device-tokens/`
- **Headers**: `Authorization: Bearer <token>`
- **Query Parameters**: `platform`, `is_active` filters.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      { "id": "d1aa...", "user": "5fa8...", "platform": "WEB", "is_active": true, "last_seen_at": "2026-07-05T14:00:00Z", "created_at": "2026-07-01T10:00:00Z" }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

---

### 2. Register Device Token (Upsert)
- **Path**: `POST /api/v1/core/device-tokens/`
- **Request Payload**:
  ```json
  { "token": "fcm-registration-token-abc123", "platform": "WEB" }
  ```
- **Field Validations**:
  - `token`: Required, non-blank FCM registration token.
  - `platform`: Optional (default `WEB`). One of `ANDROID`, `IOS`, `WEB`.
  - Idempotent: re-posting an existing token reactivates it and updates its platform/owner.
    The owner is always the authenticated user.
- **Success Response (201 Created)**:
  ```json
  {
    "status": true,
    "message": "Device registered",
    "data": {
      "id": "d1aa...",
      "tenant": { "id": "7bf2...", "name": "Alpha Corp" },
      "user": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
      "token": "fcm-registration-token-abc123",
      "platform": "WEB",
      "is_active": true,
      "last_seen_at": "2026-07-05T14:00:00Z",
      "created_at": "2026-07-05T14:00:00Z",
      "updated_at": "2026-07-05T14:00:00Z",
      "deleted_at": null
    },
    "errors": null
  }
  ```

---

### 3. Deactivate Device Token
- **Path**: `POST /api/v1/core/device-tokens/deactivate/`
- **Request Payload**:
  ```json
  { "token": "fcm-registration-token-abc123" }
  ```
- **Success Response (200 OK)**: `{ "status": true, "message": "Device deactivated", "data": null, "errors": null }`
- **Error Response (400 Bad Request)**: `token` missing.
- **Note**: Tokens are also auto-deactivated when FCM reports them unregistered/invalid.

---

### 4. Delete
- **Path**: `DELETE /api/v1/core/device-tokens/{id}/` → soft-delete → `{ "status": true, "message": "Deleted successfully" }`.

---

### 5. Dropdown
- **Path**: `GET /api/v1/core/device-tokens/dropdown/` — `{label, value}` pairs, scoped to the
  caller's own tokens. Supports `?limit` (≤ 1000) + the `platform` / `is_active` filters.

### 6. Bulk Delete
- **Path**: `POST /api/v1/core/device-tokens/bulk_delete/`
- **Request Payload**: `{ "ids": ["d1aa...", "..."] }` — only the caller's own tokens.
- **Behaviour**: Soft-deletes the listed tokens. If any id is unknown/not the caller's, the whole
  request fails with `400 "Some ids are invalid"` and nothing is deleted.
- **Success Response (200 OK)**: `{ "status": true, "message": "Bulk deleted successfully" }`

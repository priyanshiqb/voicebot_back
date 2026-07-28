# User Session & Device Management API

Provides secure endpoints to view and manage active login sessions and devices.

---

## 🔒 Authentication & Headers
All requests to these endpoints require a valid JWT Access Token.
- **Header**: `Authorization: Bearer <access_token>`

---

## 1. List Active Sessions
Retrieve a list of all active, unexpired login sessions for the authenticated user.

* **Path**: `/api/v1/auth/sessions/`
* **Method**: `GET`
* **Query Parameters**:
  * `?is_active=true|false`: Filter sessions by active status.
  * `?search=<term>`: Search by device name or IP address.
  * `?ordering=created_at|-created_at|updated_at|-updated_at`: Sort results. Default is `-updated_at`.
* **Response (Success - HTTP 200 OK)**:
  ```json
  {
      "status": true,
      "message": "success",
      "data": [
          {
              "id": "76495b28-1cdc-485c-bee9-8f8229516b8d",
              "device_name": "Chrome on Windows",
              "ip_address": "127.0.0.1",
              "is_active": true,
              "last_used": "2026-06-09T17:15:00Z",
              "created_at": "2026-06-09T17:00:00Z",
              "updated_at": "2026-06-09T17:15:00Z",
              "is_current": true
          },
          {
              "id": "e9686c12-d410-4662-92e5-370a584c6059",
              "device_name": "Postman on macOS",
              "ip_address": "192.168.1.5",
              "is_active": true,
              "last_used": "2026-06-09T12:00:00Z",
              "created_at": "2026-06-09T12:00:00Z",
              "updated_at": "2026-06-09T12:00:00Z",
              "is_current": false
          }
      ],
      "errors": null,
      "meta": {
          "count": 2,
          "next": null,
          "previous": null,
          "page": 1,
          "page_size": 10
      }
  }
  ```

---

## 2. Retrieve Specific Session Details
Retrieve detailed information about a specific login session.

* **Path**: `/api/v1/auth/sessions/{id}/`
* **Method**: `GET`
* **Response (Success - HTTP 200 OK)**:
  ```json
  {
      "status": true,
      "message": "Retrieved successfully",
      "data": {
          "id": "76495b28-1cdc-485c-bee9-8f8229516b8d",
          "created_at": "2026-06-09T17:00:00Z",
          "updated_at": "2026-06-09T17:15:00Z",
          "deleted_at": null,
          "is_deleted": false,
          "device_name": "Chrome on Windows",
          "ip_address": "127.0.0.1",
          "is_active": true,
          "expires_at": "2026-06-16T17:00:00Z",
          "last_used": "2026-06-09T17:15:00Z",
          "is_current": true
      }
  }
  ```
* **Error Responses**:
  * **HTTP 401 Unauthorized**: If missing or invalid access token.
  * **HTTP 404 Not Found**: If session ID is invalid or belongs to another user.

---

## 3. Revoke Single Session (Sign Out of Device)
Terminate and sign out of a specific device/session. Future requests using the revoked access token or refresh token will be rejected immediately.

* **Path**: `/api/v1/auth/sessions/{id}/`
* **Method**: `DELETE`
* **Response (Success - HTTP 200 OK)**:
  ```json
  {
      "status": true,
      "message": "Deleted successfully",
      "data": null,
      "errors": null
  }
  ```
* **Error Responses**:
  * **HTTP 401 Unauthorized**: If missing or invalid access token.
  * **HTTP 404 Not Found**: If session ID is invalid or belongs to another user.

---

## 4. Revoke All Other Sessions (Sign Out of Other Devices)
Sign out of all other active sessions globally, except for the current session making the request.

* **Path**: `/api/v1/auth/sessions/revoke-all/`
* **Method**: `POST`
* **Request Body**: None
* **Response (Success - HTTP 200 OK)**:
  ```json
  {
      "status": true,
      "message": "Successfully signed out of 1 other device(s).",
      "data": null,
      "errors": null
  }
  ```
* **Error Responses**:
  * **HTTP 401 Unauthorized**: If missing or invalid access token.

---

## 5. Create Session

Create a session record directly.

> **Note**: Sessions are normally created and rotated automatically by the login and token-refresh flows. This endpoint is exposed by the underlying viewset (`http_method_names` includes `post`) but is **system-managed** and generally not intended for direct client use.

* **Path**: `/api/v1/auth/sessions/`
* **Method**: `POST`
* **Request Body**: Requires `expires_at`; other writable fields (`device_name`, `ip_address`, `is_active`) are optional.
* **Response (Success - HTTP 201 Created)**: `{ "status": true, "message": "Created successfully", "data": { ...session... }, "errors": null }`
* **Error Responses**:
  * **HTTP 400 Bad Request**: If `expires_at` (or another required field) is missing/invalid.
  * **HTTP 401 Unauthorized**: If missing or invalid access token.

---

## 6. Session Dropdown

Lightweight `{ label, value }` list of the user's sessions, for populating selectors. Inherited from the base viewset.

* **Path**: `/api/v1/auth/sessions/dropdown/`
* **Method**: `GET`
* **Query Parameters**:
  * `?limit=<n>`: Cap the number of options. Maximum `1000` (a larger value returns `400` with `"Limit cannot exceed 1000"`).
  * Also supports the same list filters (`is_active`, `search`, `ordering`).
* **Response (Success - HTTP 200 OK)**: `data` is an array of `{ "label": "Chrome on Windows (IP: 127.0.0.1)", "value": "<session id>" }` objects, alongside the standard `meta` block.
* **Error Responses**:
  * **HTTP 401 Unauthorized**: If missing or invalid access token.

---

## 7. Bulk Delete Sessions

Soft-delete (revoke) multiple sessions in a single request. Inherited from the base viewset.

* **Path**: `/api/v1/auth/sessions/bulk_delete/`
* **Method**: `POST`
* **Request Body**:
  ```json
  {
      "ids": ["76495b28-1cdc-485c-bee9-8f8229516b8d", "e9686c12-d410-4662-92e5-370a584c6059"]
  }
  ```
* **Behavior**: All-or-nothing. Every id must resolve to one of the caller's sessions; if any id is invalid the whole request fails with `400`.
* **Response (Success - HTTP 200 OK)**: `{ "status": true, "message": "Bulk deleted successfully", "data": null, "errors": null }`
* **Error Responses**:
  * **HTTP 400 Bad Request**: `"ids must be a non-empty list"` (empty/malformed body) or `"Some ids are invalid"` (one or more ids not found).
  * **HTTP 401 Unauthorized**: If missing or invalid access token.

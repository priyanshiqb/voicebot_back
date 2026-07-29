# Custom Voice API

## Overview
Endpoints for managing tenant-scoped custom voice records. The backend receives a dynamic JSON payload, stores it verbatim, and returns it identically on read operations. 

**Base Path:** `/api/v1/core/custom-voices/`

---

## 1. List Custom Voice Records
**Endpoint:** `GET /api/v1/core/custom-voices/`  
**Description:** Get a paginated list of custom voice records for the authenticated tenant.

### Request Parameters
*   **`page`** *(optional)*: Integer. Page number (e.g., `?page=1`).
*   **`page_size`** *(optional)*: Integer. Number of records per page.
*   **`is_active`** *(optional)*: Boolean. Filter records by active status (e.g., `?is_active=true` or `?is_active=false`).

*Note: No JSON-level search or additional filtering is supported on this endpoint.*

### Successful Response (HTTP 200 OK)
```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "id": "aa45d90c-f443-4583-a8e2-64dbe0d73cc0",
      "user_id": "org_38ZJVLbnoN7QaiDCE7m7RaSDZuA",
      "is_public": false,
      "access": {
        "type": "private",
        "visibility": "owner"
      },
      "name": "dfgdf",
      "description": "fgvdf",
      "created_at": "2026-07-16T05:17:27.109Z",
      "language": "en",
      "record_type": "upload",
      "clone_type": "instant"
    }
  ],
  "errors": null,
  "meta": {
    "count": 1,
    "next": null,
    "previous": null,
    "page": 1,
    "page_size": 10
  }
}
```

---

## 2. Get Custom Voice Record by ID
**Endpoint:** `GET /api/v1/core/custom-voices/{id}/`  
**Description:** Get the exact custom voice record details.

### Path Parameters
*   **`id`** *(required)*: UUID. The ID of the custom voice record.

### Successful Response (HTTP 200 OK)
```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "id": "aa45d90c-f443-4583-a8e2-64dbe0d73cc0",
    "user_id": "org_38ZJVLbnoN7QaiDCE7m7RaSDZuA",
    "is_public": false,
    "access": {
      "type": "private",
      "visibility": "owner"
    },
    "name": "dfgdf",
    "description": "fgvdf",
    "created_at": "2026-07-16T05:17:27.109Z",
    "language": "en",
    "record_type": "upload",
    "clone_type": "instant"
  },
  "errors": null
}
```

---

## 3. Create Custom Voice Record
**Endpoint:** `POST /api/v1/core/custom-voices/`  
**Description:** Create a new custom voice record. The payload is stored verbatim inside the model's JSON field.

### Request Payload
**Constraints:** The system accepts any raw JSON structure provided in the root object. The backend will automatically generate the `id` (UUID) and `created_at` fields and inject them into your JSON payload before saving. 

```json
{
  "user_id": "org_38ZJVLbnoN7QaiDCE7m7RaSDZuA",
  "is_public": false,
  "access": {
    "type": "private",
    "visibility": "owner"
  },
  "name": "dfgdf",
  "description": "fgvdf",
  "language": "en",
  "record_type": "upload",
  "clone_type": "instant"
}
```

### Successful Response (HTTP 201 Created)
```json
{
  "status": true,
  "message": "Created successfully",
  "data": {
    "id": "aa45d90c-f443-4583-a8e2-64dbe0d73cc0",
    "user_id": "org_38ZJVLbnoN7QaiDCE7m7RaSDZuA",
    "is_public": false,
    "access": {
      "type": "private",
      "visibility": "owner"
    },
    "name": "dfgdf",
    "description": "fgvdf",
    "created_at": "2026-07-29T10:15:20.109Z",
    "language": "en",
    "record_type": "upload",
    "clone_type": "instant"
  },
  "errors": null
}
```

---

## 4. Delete Custom Voice Record
**Endpoint:** `DELETE /api/v1/core/custom-voices/{id}/`  
**Description:** Soft-deletes a custom voice record. 

### Path Parameters
*   **`id`** *(required)*: UUID. The ID of the custom voice record.

### Successful Response (HTTP 200 OK)
```json
{
  "status": true,
  "message": "Deleted successfully",
  "data": null,
  "errors": null
}
```

---

## Edge Cases & Error Handling

### HTTP 400 Bad Request
Triggered if the JSON payload is malformed.
```json
{
  "status": false,
  "message": "error",
  "data": null,
  "errors": {
    "detail": "JSON parse error - Expecting value: line 1 column 1 (char 0)"
  }
}
```

### HTTP 401 Unauthorized
Triggered when an unauthenticated request is made or the auth token is invalid/expired.
```json
{
  "status": false,
  "message": "error",
  "data": null,
  "errors": {
    "detail": "Authentication credentials were not provided."
  }
}
```

### HTTP 403 Permission Denied
Triggered when the user attempts to access or modify a record belonging to another tenant or lacks appropriate role permissions.
```json
{
  "status": false,
  "message": "error",
  "data": null,
  "errors": {
    "detail": "You do not have permission to perform this action."
  }
}
```

### HTTP 404 Not Found
Triggered when requesting a record ID that does not exist, belongs to another tenant, or has already been soft-deleted.
```json
{
  "status": false,
  "message": "error",
  "data": null,
  "errors": {
    "detail": "Not found."
  }
}
```

# Probing Question API Documentation

Manage Probing Questions mapped to Agent Templates under the active tenant context.

## Endpoints Summary
- `GET /api/v1/core/probing-questions/` - List all probing questions (paginated)
- `GET /api/v1/core/probing-questions/dropdown/` - Get label/value pairs
- `GET /api/v1/core/probing-questions/{id}/` - Retrieve probing question details
- `POST /api/v1/core/probing-questions/` - Create a probing question
- `PUT /api/v1/core/probing-questions/{id}/` - Update a probing question
- `PATCH /api/v1/core/probing-questions/{id}/` - Partial update a probing question
- `DELETE /api/v1/core/probing-questions/{id}/` - Soft-delete a probing question
- `POST /api/v1/core/probing-questions/bulk_delete/` - Bulk soft-delete probing questions
- `GET /api/v1/core/probing-questions/export/?format=csv|xlsx` - Export probing questions as a file download
- `POST /api/v1/core/probing-questions/import/` - Import probing questions from an uploaded file

---

## Endpoint Details

### 1. List Probing Questions
- **Path**: `GET /api/v1/core/probing-questions/`
- **Query Parameters**:
  - `page` (optional): Page number
  - `page_size` (optional): Items per page
  - `search` (optional): Filter questions by `question` string content
  - `ordering` (optional): Sort by `priority`, `score`, or `created_at` (prefix with `-` for descending, default: `priority, -created_at`)
  - `is_active` (optional): Boolean filter
  - `is_mandatory` (optional): Boolean filter
  - `can_user_edit` (optional): Boolean filter
  - `is_default` (optional): Boolean filter
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c",
        "question": "What is your primary investment goal?",
        "priority": 1,
        "score": 80,
        "is_mandatory": true,
        "can_user_edit": true,
        "is_default": false,
        "is_active": true,
        "created_at": "2026-06-03T18:00:00Z"
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

### 2. Create Probing Question
- **Path**: `POST /api/v1/core/probing-questions/`
- **Request Payload**:
  ```json
  {
    "question": "What is your primary investment goal?",
    "priority": 1,
    "score": 80,
    "is_mandatory": true,
    "is_active": true
  }
  ```
- **Field Validations**:
  - `question`: Required, 10-100 characters. Must be unique per tenant for active records.
  - `priority`: Required, integer >= 1.
  - `score`: Required, integer between 0 and 100.
  - `is_mandatory`: Optional, boolean (default: `false`).
  - `is_active`: Optional, boolean (default: `true`).
  - `can_user_edit`: Read-only field. Ignored on write operations. Defaults to `false` on creation. When `false` in database, the probing question cannot be edited or deleted via the API.
  - `is_default`: Read-only field. Ignored on write operations. Defaults to `false` on creation. When `true` in database, the probing question represents a system-wide default that is visible to all users but cannot be edited or deleted by any user.
- **Success Response (201 Created)**:
  *(Returns full detail representation)*

---

### 3. Retrieve Probing Question
- **Path**: `GET /api/v1/core/probing-questions/{id}/`
- **Success Response (200 OK)**:
  *(Returns full detail representation)*

---

### 4. Delete Probing Question (Soft Delete)
- **Path**: `DELETE /api/v1/core/probing-questions/{id}/`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": null,
    "errors": null
  }
  ```
- **Error Response (400 Bad Request)**:
  Returned when attempting to delete a question where `can_user_edit` is `false`, or if it is currently selected in an active agent or agent template.
  ```json
  {
    "status": false,
    "message": "validation_error",
    "data": null,
    "errors": {
      "detail": "This probing question is currently selected in the following agents: SalesBot... and cannot be deleted."
    }
  }
  ```

---

### 5. Bulk Delete Probing Questions
- **Path**: `POST /api/v1/core/probing-questions/bulk_delete/`
- **Request Payload**:
  ```json
  {
    "ids": [
      "7bf26cf4-e53b-48bb-bb82-261ee976378c"
    ]
  }
  ```
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Bulk deleted successfully",
    "data": null,
    "errors": null
  }
  ```
- **Error Response (400 Bad Request)**:
  Returned if any selected probing question is default or currently linked to an agent or agent template.
  ```json
  {
    "status": false,
    "message": "validation_error",
    "data": null,
    "errors": {
      "detail": "One or more selected probing questions are currently selected in an agent and cannot be deleted." // or "selected in an agent template..."
    }
  }
  ```

---

### 6. Export Probing Questions
- **Path**: `GET /api/v1/core/probing-questions/export/?format=csv|xlsx`
- **Description**: Streams a file download of probing questions. Defaults to `csv` when `format` is omitted; pass `format=xlsx` for an Excel workbook. Honors the same list filters (`search`, `is_active`, `is_mandatory`, `can_user_edit`, `is_default`).
- **Success Response (200 OK)**: Binary file download (`text/csv` or `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`).

---

### 7. Import Probing Questions
- **Path**: `POST /api/v1/core/probing-questions/import/`
- **Headers**:
  - `Content-Type: multipart/form-data`
- **Request Payload (Form-Data)**:
  - `file` (**Required**): A `.csv`, `.xls`, or `.xlsx` file. The import is validated as a dry-run first and is all-or-nothing: if any row fails, nothing is persisted.
- **Requires a tenant**: the caller must have an active tenant mapping, or be a superuser. `VB_AGENT` tokens have no mapping and receive **403**. An `id` column resolves only within the caller's own tenant, so a CSV carrying another tenant's id creates a new row rather than updating theirs. See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Successfully imported 5 records."
  }
  ```
- **Error Responses (400 Bad Request)**:
  - No file provided: `{ "status": false, "message": "No file provided" }`
  - Unsupported format: `{ "status": false, "message": "Unsupported file format. Use csv or xlsx." }`
  - Row validation errors:
    ```json
    {
      "status": false,
      "message": "Import failed due to validation errors.",
      "errors": [
        { "row": 1, "error": "Score must be between 0 and 100." }
      ]
    }
    ```

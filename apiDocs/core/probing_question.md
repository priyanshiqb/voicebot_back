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

---

## Endpoint Details

### 1. List Probing Questions
- **Path**: `GET /api/v1/core/probing-questions/`
- **Query Parameters**:
  - `page` (optional): Page number
  - `page_size` (optional): Items per page
  - `search` (optional): Filter questions by `question` string content
  - `ordering` (optional): Sort by `priority`, `score`, or `created_at` (prefix with `-` for descending, default: `agent_template, priority, -created_at`)
  - `is_active` (optional): Boolean filter
  - `is_mandatory` (optional): Boolean filter
  - `agent_template` (optional): UUID of the parent Agent Template
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c",
        "agent_template": {
          "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
          "name": "Wealth Advisory"
        },
        "question": "What is your primary investment goal?",
        "priority": 1,
        "score": 80,
        "is_mandatory": true,
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
    "agent_template": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "question": "What is your primary investment goal?",
    "priority": 1,
    "score": 80,
    "is_mandatory": true
  }
  ```
- **Field Validations**:
  - `agent_template`: Required, valid UUID of an active Agent Template.
  - `question`: Required, 10-100 characters. Must be unique per parent `agent_template` for active records.
  - `priority`: Required, integer >= 1.
  - `score`: Required, integer between 0 and 100.
  - `is_mandatory`: Optional, boolean (default: `false`).
- **Success Response (201 Created)**:
  *(Returns full detail representation, where `agent_template` resolves to the nested template Ref representation)*

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

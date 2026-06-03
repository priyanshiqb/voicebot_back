# Agent Template API Documentation

Manage predefined or custom voicebot Agent Templates scoped to the authenticated tenant.

## Endpoints Summary
- `GET /api/v1/core/agent-templates/` - List all templates (paginated)
- `GET /api/v1/core/agent-templates/dropdown/` - Get label/value list
- `GET /api/v1/core/agent-templates/{id}/` - Retrieve template details
- `POST /api/v1/core/agent-templates/` - Create a template
- `PUT /api/v1/core/agent-templates/{id}/` - Update a template
- `PATCH /api/v1/core/agent-templates/{id}/` - Partial update a template
- `DELETE /api/v1/core/agent-templates/{id}/` - Soft-delete a template
- `POST /api/v1/core/agent-templates/bulk_delete/` - Bulk soft-delete templates

---

## Endpoint Details

### 1. List Agent Templates
- **Path**: `GET /api/v1/core/agent-templates/`
- **Headers**:
  - `Authorization: Bearer <token>`
- **Query Parameters**:
  - `page` (optional): Page number (default: 1)
  - `page_size` (optional): Items per page (default: 10)
  - `search` (optional): Filter templates by `name` or `short_description` (partial case-insensitive)
  - `ordering` (optional): Sort by `name` or `created_at` (prefix with `-` for descending, default: `-created_at`)
  - `is_active` (optional): Boolean filter
  - `strict_facts` (optional): Boolean filter
  - `can_user_edit_flow` (optional): Boolean filter
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "name": "Wealth Advisory",
        "short_description": "Custom customer support bot for investments.",
        "strict_facts": true,
        "can_user_edit_flow": false,
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

### 2. Get Dropdown List
- **Path**: `GET /api/v1/core/agent-templates/dropdown/`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [
      {
        "label": "Wealth Advisory",
        "value": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
      }
    ],
    "errors": null
  }
  ```

---

### 3. Retrieve Agent Template
- **Path**: `GET /api/v1/core/agent-templates/{id}/`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": {
      "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "tenant": {
        "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c",
        "name": "Alpha Corp"
      },
      "name": "Wealth Advisory",
      "short_description": "Custom customer support bot for investments.",
      "visual_icon": "support-icon",
      "primary_accent": "#00FF00",
      "agent_prompt": "You are a customer support agent assisting users with their investments.",
      "strict_facts": true,
      "can_user_edit_flow": false,
      "created_by": {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "name": "Emma"
      },
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

### 4. Create Agent Template
- **Path**: `POST /api/v1/core/agent-templates/`
- **Request Payload**:
  ```json
  {
    "name": "Wealth Advisory",
    "short_description": "Custom customer support bot for investments.",
    "visual_icon": "support-icon",
    "primary_accent": "#00FF00",
    "agent_prompt": "You are a customer support agent assisting users with their investments.",
    "strict_facts": true,
    "can_user_edit_flow": false
  }
  ```
- **Field Validations**:
  - `name`: Required, 2-100 characters. Letters, numbers, spaces, hyphens, and underscores only. Must be unique per tenant for active templates.
  - `short_description`: Required, 10-300 characters.
  - `primary_accent`: Optional, valid 6-digit hex color format (e.g. `#00FF00`, matching `^#[0-9A-Fa-f]{6}$`).
  - `agent_prompt`: Required, 30-5000 characters.
- **Success Response (201 Created)**:
  *(Returns full detail JSON representation)*

---

### 5. Update Agent Template
- **Path**: `PUT /api/v1/core/agent-templates/{id}/`
- **Request Payload**: *(Same field validations as POST)*
- **Success Response (200 OK)**:
  *(Returns full updated detail JSON representation)*

---

### 6. Delete Agent Template (Soft Delete)
- **Path**: `DELETE /api/v1/core/agent-templates/{id}/`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": null,
    "errors": null
  }
  ```

---

### 7. Bulk Delete Agent Templates
- **Path**: `POST /api/v1/core/agent-templates/bulk_delete/`
- **Request Payload**:
  ```json
  {
    "ids": [
      "3fa85f64-5717-4562-b3fc-2c963f66afa6",
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

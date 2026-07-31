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
- `GET /api/v1/core/agent-templates/export/?format=csv|xlsx` - Export templates as a file download
- `POST /api/v1/core/agent-templates/import/` - Import templates from an uploaded file

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
  - `ordering` (optional): Sort by `name`, `created_at`, `updated_at`, `industry`, `is_default`, or `strict_facts` (prefix with `-` for descending, default: `-created_at`)
  - `is_active` (optional): Boolean filter
  - `strict_facts` (optional): Boolean filter
  - `can_user_edit_flow` (optional): Boolean filter
  - `is_default` (optional): Boolean filter
  - `industry` (optional): Filter templates by industry category. Allowed values: `CLINIC_RECEPTIONIST`, `AGRICULTURAL_SUPPLIER`, `REAL_ESTATE`, `E_COMMERCE`, `RESTAURANT`, `GENERAL_ASSISTANT`, `OTHER`
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
        "visual_icon": "support-icon",
        "primary_accent": "#00FF00",
        "industry": "GENERAL_ASSISTANT",
        "strict_facts": true,
        "can_user_edit_flow": true,
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
      "industry": "GENERAL_ASSISTANT",
      "strict_facts": true,
      "can_user_edit_flow": true,
      "is_active": true,
      "probing_questions": [
        {
          "id": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
          "question": "What is your account number?"
        }
      ],
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
    "industry": "GENERAL_ASSISTANT",
    "strict_facts": true,
    "can_user_edit_flow": true,
    "is_active": true,
    "add_probing_questions": [
      "5fa85f64-5717-4562-b3fc-2c963f66afa7"
    ]
  }
  ```
- **Field Validations**:
  - `name`: Required, 2-100 characters. Letters, numbers, spaces, hyphens, and underscores only. Must be unique per tenant for active templates.
  - `short_description`: Required, 10-300 characters.
  - `visual_icon`: Optional, max length 50 characters.
  - `primary_accent`: Optional, valid 6-digit hex color format (e.g. `#00FF00`, matching `^#[0-9A-Fa-f]{6}$`).
  - `agent_prompt`: Required, 30-50000 characters.
  - `industry`: Optional. Selected industry category for the template (default: `GENERAL_ASSISTANT`). Allowed values: `CLINIC_RECEPTIONIST`, `AGRICULTURAL_SUPPLIER`, `REAL_ESTATE`, `E_COMMERCE`, `RESTAURANT`, `GENERAL_ASSISTANT`, `OTHER`.
  - `strict_facts`: Optional, boolean (default: `true`).
  - `can_user_edit_flow`: Optional, boolean (default: `true`).
  - `is_active`: Optional, boolean (default: `true`).
  - `probing_questions`: Read-only field. Ignored on write operations. If passed in request body, a validation error is raised.
  - `add_probing_questions`: Optional list of UUID strings. Used to incrementally add probing questions. Each referenced question must belong to the active tenant's context or be a global default probing question (`is_default = true`).
  - `remove_probing_questions`: Optional list of UUID strings. Used to incrementally remove probing questions.
  - `is_default`: Read-only field. Ignored on write operations. Defaults to `false` on creation. When `true` in database, the agent template represents a system-wide default that is visible to all users but cannot be edited or deleted by any user.
- **Success Response (201 Created)**:
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
      "industry": "GENERAL_ASSISTANT",
      "strict_facts": true,
      "can_user_edit_flow": true,
      "is_active": true,
      "probing_questions": [
        {
          "id": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
          "question": "What is your account number?"
        }
      ],
      "created_by": {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "name": "Emma"
      },
      "updated_by": null,
      "deleted_by": null,
      "created_at": "2026-06-03T18:00:00Z",
      "updated_at": "2026-06-03T18:00:00Z",
      "deleted_at": null,
      "is_default": false
    },
    "errors": null
  }
  ```

---

### 5. Update Agent Template
- **Path**: `PUT /api/v1/core/agent-templates/{id}/`
- **Request Payload (Full Update)**: *(Same field validations as POST)*
- **Request Payload (Incremental Probing Question Update)**:
  ```json
  {
    "add_probing_questions": [
      "5fa85f64-5717-4562-b3fc-2c963f66afa7"
    ],
    "remove_probing_questions": [
      "8fa85f64-5717-4562-b3fc-2c963f66afa9"
    ]
  }
  ```
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
      "short_description": "Updated customer support bot description.",
      "visual_icon": "support-icon",
      "primary_accent": "#00FF00",
      "agent_prompt": "You are a customer support agent assisting users with their investments.",
      "strict_facts": true,
      "can_user_edit_flow": true,
      "is_active": true,
      "probing_questions": [
        {
          "id": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
          "question": "What is your account number?"
        }
      ],
      "created_by": {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "name": "Emma"
      },
      "updated_by": {
        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "name": "Emma"
      },
      "deleted_by": null,
      "created_at": "2026-06-03T18:00:00Z",
      "updated_at": "2026-06-04T12:00:00Z",
      "deleted_at": null,
      "is_default": false
    },
    "errors": null
  }
  ```

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
- **Error Response (400 Bad Request)**:
  Returned when attempting to delete a template that is currently selected in an active agent, or if it is a default template.
  ```json
  {
    "status": false,
    "message": "validation_error",
    "data": null,
    "errors": {
      "detail": "This agent template is currently selected in the following agents: SalesBot... and cannot be deleted."
    }
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
- **Error Response (400 Bad Request)**:
  Returned if any of the selected templates are default or are currently used by an agent.
  ```json
  {
    "status": false,
    "message": "validation_error",
    "data": null,
    "errors": {
      "detail": "One or more selected agent templates are currently selected in an agent and cannot be deleted."
    }
  }
  ```

---

### 8. Export Agent Templates
- **Path**: `GET /api/v1/core/agent-templates/export/?format=csv|xlsx`
- **Description**: Streams a file download of agent templates. Defaults to `csv` when `format` is omitted; pass `format=xlsx` for an Excel workbook. Honors the same list filters (`search`, `is_active`, `strict_facts`, `can_user_edit_flow`, `is_default`, `industry`).
- **Success Response (200 OK)**: Binary file download (`text/csv` or `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`).

---

### 9. Import Agent Templates
- **Path**: `POST /api/v1/core/agent-templates/import/`
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
        { "row": 1, "error": "Name must be between 2 and 100 characters." }
      ]
    }
    ```

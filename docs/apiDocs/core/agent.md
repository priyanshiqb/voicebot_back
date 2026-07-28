# Agent API Documentation

Manage configured Voicebot Agents under the active tenant context.

## General Rules
- **Authentication**: `Bearer <token>` (JWT or VB_AGENT token)
- **Tenant Scoping**: All records are isolated per tenant by default.
- **VB_AGENT Access**: Tokens with `VB_AGENT` user type automatically bypass tenant-scoping for `GET` requests (`list`, `retrieve`, `dropdown`) and can access all active agents system-wide.

## Endpoints Summary
- `GET /api/v1/core/agents/` - List all agents (paginated)
- `GET /api/v1/core/agents/dropdown/` - Get label/value pairs
- `GET /api/v1/core/agents/{id}/` - Retrieve agent details
- `POST /api/v1/core/agents/` - Create a new agent
- `PUT /api/v1/core/agents/{id}/` - Update an agent
- `PATCH /api/v1/core/agents/{id}/` - Partial update an agent
- `DELETE /api/v1/core/agents/{id}/` - Soft-delete an agent
- `POST /api/v1/core/agents/bulk_delete/` - Bulk soft-delete agents
- `POST /api/v1/core/agents/check-bot-name/` - Check if a bot name is unique
- `GET /api/v1/core/agents/export/` - Export agents to CSV/XLSX
- `POST /api/v1/core/agents/import/` - Import agents from CSV/XLSX

---

## Endpoint Details

### 1. List Agents
- **Path**: `GET /api/v1/core/agents/`
- **Query Parameters**:
  - `page` (optional): Page number
  - `page_size` (optional): Items per page
  - `search` (optional): Filter agents by `bot_name` or `persona_name`
  - `ordering` (optional): Sort by `bot_name`, `created_at`, or `tone` (prefix with `-` for descending, default: `-created_at`)
  - `is_active` (optional): Boolean filter
  - `tone` (optional): Filter by agent tone. Allowed values: `PROFESSIONAL`, `FRIENDLY`, `FORMAL`, `CASUAL`, `ENTHUSIASTIC`, `RUDE`
  - `industry` (optional): Filter by the industry category of the agent's template. Allowed values: `CLINIC_RECEPTIONIST`, `AGRICULTURAL_SUPPLIER`, `REAL_ESTATE`, `E_COMMERCE`, `RESTAURANT`, `GENERAL_ASSISTANT`, `OTHER`
  - `agent_template` (optional): UUID of the associated template
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c",
        "bot_name": "Investment Advisor Bot",
        "persona_name": "Oliver",
        "tone": "PROFESSIONAL",
        "agent_template": {
          "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
          "name": "Wealth Advisory"
        },
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

### 2. Create Agent
- **Path**: `POST /api/v1/core/agents/`
- **Request Payload**:
  ```json
  {
    "bot_name": "Investment Advisor Bot",
    "persona_name": "Oliver",
    "languages": ["en-IN", "en-US"],
    "tone": "PROFESSIONAL",
    "agent_template": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "voice_name": "en-IN-Wavenet-B",
    "add_knowledge_bases": [
      "9734df89-1065-4f40-8458-1c4b81cf738b"
    ],
    "add_probing_questions": [
      "5fa85f64-5717-4562-b3fc-2c963f66afa7"
    ],
    "add_event_templates": [
      "4cc85f64-5717-4562-b3fc-2c963f66af30"
    ],
    "transfer_destinations": {
      "support_desk": "+1234567890"
    }
  }
  ```
- **Field Validations**:
  - `bot_name`: Required, 2-100 characters. Letters, numbers, spaces, hyphens, and underscores only. Must be unique per tenant for active agents.
  - `persona_name`: Required, 2-100 characters. Letters, numbers, spaces, hyphens, and underscores only.
  - `languages`: Required, non-empty list of language strings.
  - `tone`: Required. Tone of the agent interaction. Allowed values: `PROFESSIONAL`, `FRIENDLY`, `FORMAL`, `CASUAL`, `ENTHUSIASTIC`, `RUDE`.
  - `agent_template`: Optional, valid UUID of an active Agent Template.
  - `voice_name`: Optional string.
  - `knowledge_bases`: Read-only field. Ignored on write operations. If passed in request body, a validation error is raised.
  - `add_knowledge_bases`: Optional list of UUID strings. Used to incrementally add knowledge bases. Each referenced KB must belong to the active tenant's context.
  - `remove_knowledge_bases`: Optional list of UUID strings. Used to incrementally remove knowledge bases.
  - `probing_questions`: Read-only field. Ignored on write operations. If passed in request body, a validation error is raised.
  - `add_probing_questions`: Optional list of UUID strings. Used to incrementally add probing questions. Each referenced question must belong to the active tenant's context or be a global default probing question (`is_default = true`).
  - `remove_probing_questions`: Optional list of UUID strings. Used to incrementally remove probing questions.
  - `event_templates`: Read-only field. The **bookable meeting types this agent may schedule** (returned as `{id, name}` refs). Ignored on write; passing it in the body raises a validation error. Drives the AI-agent booking guardrail — an agent can only book templates in this set. See [AI Agent Booking flow](../../flows/06-ai-agent-booking.md).
  - `add_event_templates`: Optional list of UUID strings. Incrementally links event templates. Each must belong to the active tenant's context.
  - `remove_event_templates`: Optional list of UUID strings. Incrementally unlinks event templates.
  - `transfer_destinations`: Optional dictionary/JSON object mapping labels to destination numbers/URIs.
- **Success Response (201 Created)**:
  *(Returns full detail representation, with resolved hybrid fields and audit properties)*

---

### 3. Retrieve Agent Details
- **Path**: `GET /api/v1/core/agents/{id}/`
- **Success Response (200 OK)**:
  *(Returns full detail representation)*

---

### 4. Delete Agent (Soft Delete)
- **Path**: `DELETE /api/v1/core/agents/{id}/`
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

### 5. Update Agent
- **Path**: `PUT /api/v1/core/agents/{id}/`
- **Request Payload (Full Update)**: *(Same field validations as POST)*
- **Request Payload (Incremental Update)**:
  ```json
  {
    "add_knowledge_bases": [
      "9734df89-1065-4f40-8458-1c4b81cf738b"
    ],
    "remove_knowledge_bases": [
      "8734df89-1065-4f40-8458-1c4b81cf738a"
    ],
    "add_probing_questions": [
      "5fa85f64-5717-4562-b3fc-2c963f66afa7"
    ],
    "remove_probing_questions": [
      "8fa85f64-5717-4562-b3fc-2c963f66afa9"
    ],
    "add_event_templates": [
      "4cc85f64-5717-4562-b3fc-2c963f66af30"
    ],
    "remove_event_templates": [
      "3cc85f64-5717-4562-b3fc-2c963f66af29"
    ]
  }
  ```
- **Success Response (200 OK)**:
  *(Returns full detail representation)*

---

### 6. Check Bot Name Uniqueness
- **Path**: `POST /api/v1/core/agents/check-bot-name/`
- **Description**: Checks if a bot name is unique under the active tenant context.
- **Request Payload**:
  ```json
  {
    "bot_name": "Investment Advisor Bot",
    "exclude_id": "7bf26cf4-e53b-48bb-bb82-261ee976378c" // Optional: ID to exclude from uniqueness check
  }
  ```
- **Field Validations**:
  - `bot_name`: Required, max 100 characters.
  - `exclude_id`: Optional, valid UUID string.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": {
      "is_unique": true
    },
    "errors": null
  }
  ```

---

### 7. Dropdown Options
- **Path**: `GET /api/v1/core/agents/dropdown/`
- **Description**: Lightweight `{label, value}` pairs for active agents (for select inputs).
- **Query Parameters**:
  - `limit` (optional): Max rows to return. Must be ≤ `1000` (else `400`).
  - Also accepts the same `search` / filter params as List.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [
      { "label": "Investment Advisor Bot", "value": "7bf26cf4-e53b-48bb-bb82-261ee976378c" }
    ],
    "errors": null
  }
  ```

---

### 8. Bulk Delete
- **Path**: `POST /api/v1/core/agents/bulk_delete/`
- **Description**: Soft-delete multiple agents in one call (tenant-scoped).
- **Request Payload**:
  ```json
  { "ids": ["7bf26cf4-e53b-48bb-bb82-261ee976378c", "8cc37df5-...."] }
  ```
- **Field Validations**:
  - `ids`: Required, **non-empty** list of agent UUIDs. If any id is unknown/not in the caller's tenant, the whole request fails with `400 "Some ids are invalid"` (nothing is deleted).
- **Success Response (200 OK)**: `{ "status": true, "message": "Bulk deleted successfully" }`

---

### 9. Export Agents
- **Path**: `GET /api/v1/core/agents/export/`
- **Description**: Download the current (filtered) agent list as a file. Honors the same
  `search` / filter / `ordering` query params as List.
- **Query Parameters**:
  - `format` (optional): `csv` (default) or `xlsx`.
- **Success Response (200 OK)**: a file download (`text/csv` or the XLSX mime type) via
  `Content-Disposition: attachment`. **Not** the JSON envelope.

---

### 10. Import Agents
- **Path**: `POST /api/v1/core/agents/import/`
- **Headers**: `Content-Type: multipart/form-data`
- **Request Payload** (form-data):
  - `file`: Required. A `.csv`, `.xls`, or `.xlsx` file.
- **Behaviour**: Parsed and validated in a dry run first; only committed if the whole file is
  valid (all-or-nothing). Tenant/user context is injected from the caller.
- **Success Response (200 OK)**: `{ "status": true, "message": "Successfully imported N records." }`
- **Error Response (400 Bad Request)**:
  - No file: `{ "status": false, "message": "No file provided" }`
  - Bad extension: `{ "status": false, "message": "Unsupported file format. Use csv or xlsx." }`
  - Row validation errors:
    ```json
    {
      "status": false,
      "message": "Import failed due to validation errors.",
      "errors": [ { "row": 2, "error": "..." } ]
    }
    ```

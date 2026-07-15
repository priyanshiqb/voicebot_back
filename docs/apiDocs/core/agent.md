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
  - `tone` (optional): Filter by agent tone. Allowed values: `PROFESSIONAL`, `FRIENDLY`, `FORMAL`, `CASUAL`, `ENTHUSIASTIC`
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
    "transfer_destinations": {
      "support_desk": "+1234567890"
    }
  }
  ```
- **Field Validations**:
  - `bot_name`: Required, 2-100 characters. Letters, numbers, spaces, hyphens, and underscores only. Must be unique per tenant for active agents.
  - `persona_name`: Required, 2-100 characters. Letters, numbers, spaces, hyphens, and underscores only.
  - `languages`: Required, non-empty list of language strings.
  - `tone`: Required. Tone of the agent interaction. Allowed values: `PROFESSIONAL`, `FRIENDLY`, `FORMAL`, `CASUAL`, `ENTHUSIASTIC`.
  - `agent_template`: Optional, valid UUID of an active Agent Template.
  - `voice_name`: Optional string.
  - `knowledge_bases`: Read-only field. Ignored on write operations. If passed in request body, a validation error is raised.
  - `add_knowledge_bases`: Optional list of UUID strings. Used to incrementally add knowledge bases. Each referenced KB must belong to the active tenant's context.
  - `remove_knowledge_bases`: Optional list of UUID strings. Used to incrementally remove knowledge bases.
  - `probing_questions`: Read-only field. Ignored on write operations. If passed in request body, a validation error is raised.
  - `add_probing_questions`: Optional list of UUID strings. Used to incrementally add probing questions. Each referenced question must belong to the active tenant's context or be a global default probing question (`is_default = true`).
  - `remove_probing_questions`: Optional list of UUID strings. Used to incrementally remove probing questions.
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

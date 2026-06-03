# Agent API Documentation

Manage configured Voicebot Agents under the active tenant context.

## Endpoints Summary
- `GET /api/v1/core/agents/` - List all agents (paginated)
- `GET /api/v1/core/agents/dropdown/` - Get label/value pairs
- `GET /api/v1/core/agents/{id}/` - Retrieve agent details
- `POST /api/v1/core/agents/` - Create a new agent
- `PUT /api/v1/core/agents/{id}/` - Update an agent
- `PATCH /api/v1/core/agents/{id}/` - Partial update an agent
- `DELETE /api/v1/core/agents/{id}/` - Soft-delete an agent
- `POST /api/v1/core/agents/bulk_delete/` - Bulk soft-delete agents

---

## Endpoint Details

### 1. List Agents
- **Path**: `GET /api/v1/core/agents/`
- **Query Parameters**:
  - `page` (optional): Page number
  - `page_size` (optional): Items per page
  - `search` (optional): Filter agents by `bot_name` or `persona_name`
  - `ordering` (optional): Sort by `bot_name` or `created_at` (prefix with `-` for descending, default: `-created_at`)
  - `is_active` (optional): Boolean filter
  - `tone` (optional): Text filter matching tone choices (Professional, Friendly, Formal, etc.)
  - `industry` (optional): Text filter matching industry choices
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
        "tone": "Professional",
        "industry": "Financial Services",
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
    "tone": "Professional",
    "industry": "Financial Services",
    "agent_template": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "voice_name": "en-IN-Wavenet-B",
    "knowledge_bases": [
      "9734df89-1065-4f40-8458-1c4b81cf738b"
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
  - `tone`: Required, must match valid tone enum/choice.
  - `industry`: Required, must match valid industry choice.
  - `agent_template`: Optional, valid UUID of an active Agent Template.
  - `voice_name`: Optional string.
  - `knowledge_bases`: Optional list of active Knowledge Base UUIDs.
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

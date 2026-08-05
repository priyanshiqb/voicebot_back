# Agent API Documentation

Manage configured Voicebot Agents under the active tenant context.

## General Rules
- **Authentication**: `Bearer <token>` (JWT or VB_AGENT token)
- **Tenant Scoping**: All records are isolated per tenant by default.
- **VB_AGENT Access**: `retrieve` **only** (`GET /agents/{id}/`). It bypasses tenant scoping and can
  reach any tenant's agent. `list`, `dropdown` and every write action return **403**.
  ⚠️ `list` and `dropdown` were previously open to agent tokens — this is a breaking change for
  any runner that enumerated agents.
- **`X-Tenant-ID` header** (VB_AGENT only, **required**): scopes `retrieve` to one tenant, so an
  agent outside it returns 404. Omitting it returns **400**. See
  [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md). Ignored for JWT users,
  whose scope always comes from their tenant mapping.

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
    "timezone": "2b1c85f64-5717-4562-b3fc-2c963f66afa9",
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
### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `bot_name` * | string | Internal identifier for this bot, shown in listings and campaign pickers. | 2–100 chars. Letters, numbers, spaces, hyphens, underscores only. **Unique per tenant** among non-deleted agents. |
| `persona_name` * | string | The name the bot uses for itself on a call ("Hi, this is *Riya*…"). | 2–100 chars, same character set as `bot_name`. Not required to be unique. |
| `languages` * | string array | Locale codes the agent can converse in, e.g. `["en-IN"]`. | Non-empty list; every entry a non-empty string. |
| `tone` * | enum | How the agent phrases itself. | One of `PROFESSIONAL`, `FRIENDLY`, `FORMAL`, `CASUAL`, `ENTHUSIASTIC`, `RUDE`. |
| `agent_template` | UUID \| null | Template used to seed the agent's prompt/config. | Must be an active Agent Template. |
| `voice_name` | string \| null | TTS voice identifier, e.g. `en-IN-Wavenet-B`. | Max 100 chars. Not validated against a provider catalogue. |
| `timezone` | UUID \| null | **The clock this agent speaks in.** Times it quotes to a caller and times it hears back are in this zone. | Must be an existing `Timezone` id. `null` falls back to the tenant's base timezone, then UTC. See below. |
| `transfer_destinations` | object \| null | Label → destination map for call transfer, e.g. `{"support_desk": "+1234567890"}`. | Any JSON object. Not yet acted on by the runtime. |
| `add_knowledge_bases` | UUID array | Knowledge bases to **link**. | Each must belong to your tenant. |
| `remove_knowledge_bases` | UUID array | Knowledge bases to **unlink**. | — |
| `add_probing_questions` | UUID array | Probing questions to **link**. | Each must belong to your tenant **or** be a global default (`is_default: true`). |
| `remove_probing_questions` | UUID array | Probing questions to **unlink**. | — |
| `add_event_templates` | UUID array | Meeting types this agent may book. | Each must belong to your tenant. |
| `remove_event_templates` | UUID array | Meeting types to **unlink**. | — |

#### Read-only relationship fields

`knowledge_bases`, `probing_questions`, and `event_templates` are returned on read as `{id, name}`
refs but are **rejected on write** — sending any of them raises a `400` telling you to use the
matching `add_*` / `remove_*` list. The paired lists exist so two clients editing different
knowledge bases don't clobber each other the way a whole-list replacement would.

`event_templates` is the **AI booking guardrail**: an agent can only schedule meeting types in this
set, and any other template id is refused before the engine runs. See the
[AI Agent Booking flow](../../flows/06-ai-agent-booking.md).

#### `timezone` — the clock the agent speaks in

Everything stored and everything crossing this API stays **UTC**. This field only controls the
zone used when the agent talks to a human:

| | |
|---|---|
| Times the agent **says** on a call | rendered in this zone |
| A date the caller gives ("Tuesday") | interpreted in this zone |
| A time the caller gives with no offset | interpreted in this zone |
| `start` / `end` / `hold_expires_at` in tool results | always UTC |
| Anything written to the database | always UTC |

Set it to the zone your callers are in, not the zone your hosts are in — the caller is the one
talking to the agent. Leaving it `null` is safe, and falls back to the tenant's base timezone.

Full contract: [Timezones — two clocks, never mixed](../../flows/06-ai-agent-booking.md#timezones--two-clocks-never-mixed).

### Responses

| Status | When | `message` |
|---|---|---|
| **201** | Created. Body is the full detail representation, with `timezone`, `knowledge_bases`, `probing_questions` and `event_templates` resolved to `{id, …}` refs. | `Created successfully` |
| **400** | `bot_name` already used by an active agent in this tenant. | `Agent with this name already exists.` |
| **400** | A read-only relationship field was sent. | `This field is read-only. Use add_knowledge_bases/remove_knowledge_bases instead.` |
| **400** | A linked knowledge base / question / template belongs to another tenant. | the offending id and reason |
| **400** | `timezone` id unknown. | `Invalid pk "…" - object does not exist.` |
| **400** | Field-level validation (length, charset, empty `languages`, bad `tone`). | the field's message |
| **400** | Caller's account has no tenant mapping. | `User is not associated with an active tenant.` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |

---

### 3. Retrieve Agent Details
- **Path**: `GET /api/v1/core/agents/{id}/`
- **Success Response (200 OK)**: full detail representation.

| Status | When |
|---|---|
| **200** | Found. |
| **403** | Caller's account has no tenant mapping. |
| **404** | No such agent **in your tenant** — indistinguishable from an id that does not exist anywhere. |

---

### 4. Delete Agent (Soft Delete)
- **Path**: `DELETE /api/v1/core/agents/{id}/`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Deleted successfully",
    "data": null,
    "errors": null
  }
  ```

Soft-delete only: the row is retained and its `bot_name` is suffixed with a deletion timestamp so
the name becomes reusable immediately.

| Status | When | `message` |
|---|---|---|
| **200** | Deleted. | `Deleted successfully` |
| **400** | **A live campaign still uses this agent.** Nothing is deleted. | `This bot is used by N campaign(s) that are still running. Cancel them before deleting the bot.` |
| **404** | No such agent in your tenant. | — |

> The campaign guard exists because agents are soft-deleted, so the database's `PROTECT` foreign key
> never fires — a running campaign would otherwise be left pointing at a deleted bot mid-run.

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
- **Requires a tenant**: the caller must have an active tenant mapping, or be a superuser.
  `VB_AGENT` tokens have no mapping and receive **403** — bulk CSV writes are not a machine
  caller's action. An `id` column resolves only within the caller's own tenant, so a CSV
  carrying another tenant's id creates a new row rather than updating theirs.
  See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).
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

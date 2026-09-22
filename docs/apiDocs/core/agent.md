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

**`GET /api/v1/core/agents/`**

Paginated list of the tenant's agents. Returns a compact shape — relationships, `timezone`, and
audit fields are **not** included; use Retrieve for those.

| Parameter | Type | Description |
|---|---|---|
| `page`, `page_size` | integer | Pagination. |
| `search` | string | Partial, case-insensitive match on `bot_name` or `persona_name`. |
| `ordering` | string | `bot_name`, `created_at`, `tone`, `agent_template` (sorts by name), `languages` (sorts by length), or `total_languages_count`; prefix `-` to reverse. Default `-created_at`. |
| `is_active` | boolean | Filter by active flag. |
| `tone` | enum | `PROFESSIONAL`, `FRIENDLY`, `FORMAL`, `CASUAL`, `ENTHUSIASTIC`, `RUDE`. Supports multiple comma-separated values (e.g. `?tone=PROFESSIONAL,CASUAL`). |
| `agent_template` | UUID | Only agents built from this template. |
| `industry` | enum | Filters by the **template's** industry, not a field on the agent: `CLINIC_RECEPTIONIST`, `AGRICULTURAL_SUPPLIER`, `REAL_ESTATE`, `E_COMMERCE`, `RESTAURANT`, `GENERAL_ASSISTANT`, `OTHER`. Agents with no template are excluded when this is set. Supports multiple comma-separated values. |

> **Not available to `VB_AGENT` tokens** — this action returns **403**. A runner is handed the
> agent id it is acting for, so enumerating a tenant's agents is reach it never needed. Use
> `GET /agents/{id}/`.

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
        "created_at": "2026-06-03T18:00:00Z",
        "total_languages_count": 2,
        "voice_name": "en-IN-Wavenet-B"
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

> **The list payload is not a subset of the detail payload.** `total_languages_count` appears
> **only here** — it is a computed count, not a stored field, and there is no equivalent on
> retrieve. Two fields are worth calling out:
>
> | Field | Meaning |
> |---|---|
> | `total_languages_count` | How many entries are in the agent's `languages` array. `0` when the array is empty or null — never absent. |
> | `voice_name` | The TTS voice, so a list can show it without a per-row detail call. |
>
> Everything else omitted from the list — `languages` itself, `knowledge_bases`, `probing_questions`,
> `transfer_destinations`, `timezone` — requires [retrieve](#2-retrieve-an-agent).

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
| `add_event_templates` | UUID array | Meeting types this agent may book. | Each must belong to your tenant **and be active** — an inactive type is a **400** (`Event template <name> is inactive. Activate it before adding it to a bot.`), because it refuses every booking. |
| `remove_event_templates` | UUID array | Meeting types to **unlink**. | — |

> **`service_tier` is not settable here.** Every agent carries one — it decides what a
> minute of its calls costs, and every plan prices all three tiers — but it is not
> exposed on this endpoint. A new agent gets `conversation_high_latency`, and changing
> it is a staff action in the admin.
>
> The tier a call is *actually billed at* comes from the call log, not from here: a bot
> configured for realtime may have been served on the standard model, and the tenant
> pays for what ran. See
> [Billing → What a minute costs](../billing/README.md#what-a-minute-costs).

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

**`PUT` / `PATCH /api/v1/core/agents/{id}/`**

Same field table as Create; on `PATCH` every field is optional.

**Relationships are edited incrementally, not replaced.** Unlike most resources here, you never
send the full `knowledge_bases` list — you send `add_*` / `remove_*` deltas. That is deliberate:
two people editing different knowledge bases at the same time would otherwise clobber each other,
since a whole-list write encodes "and nothing else".

| You send | Result |
|---|---|
| `add_knowledge_bases: [a]` | `a` is linked; every existing link is kept |
| `remove_knowledge_bases: [b]` | `b` is unlinked; everything else kept |
| both, in one call | removals and additions both applied |
| `knowledge_bases: [...]` | **400** — read-only field |
| neither | links untouched |

The same pattern applies to `probing_questions` and `event_templates`.

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
| Status | When | `message` |
|---|---|---|
| **200** | Updated. Body is the full detail representation. | `Updated successfully` |
| **400** | Same causes as Create, plus: an `add_*` id belongs to another tenant. | the first error |
| **403** | Caller's account has no tenant mapping. | `Your user account is not associated with any active tenant.` |
| **404** | No such agent in your tenant. | — |

> Changing `timezone` affects **future conversations only**. Times already quoted on a live call
> are not revisited, and stored bookings are UTC and therefore unaffected.

---

### 6. Check Bot Name Uniqueness

**`POST /api/v1/core/agents/check-bot-name/`**

Answers "is this name free?" without attempting a write — for inline validation on a create or
rename form, so the user learns before submitting.

| Field | Type | Description | Validation |
|---|---|---|---|
| `bot_name` * | string | The name to test. | Required, max 100 chars. **Matched exactly** — not case-insensitively, and not trimmed, so `"Bot "` and `"bot"` both count as free when `"Bot"` is taken. |
| `exclude_id` | UUID | An agent to ignore in the check. **Pass the agent's own id when renaming**, or it will always report its current name as taken. | Optional, valid UUID. |

**Scope:** the caller's tenant only. Superusers check globally. Soft-deleted agents never count as
taking a name, since deletion suffixes the name to free it.

```json
{ "status": true, "message": "success", "data": { "is_unique": true }, "errors": null }
```

| Status | When |
|---|---|
| **200** | Checked. `data.is_unique` is `true` (free) or `false` (taken). |
| **400** | `bot_name` missing or over 100 chars, or `exclude_id` is not a UUID. |
| **403** | Caller's account has no tenant mapping. |

> **This is advisory, not a reservation.** Another request can take the name between your check
> and your `POST`. Always handle `400 "Agent with this name already exists."` on submit as well —
> a green tick here is not a guarantee.

---

### 7. Dropdown Options

**`GET /api/v1/core/agents/dropdown/`**

Compact `{label, value}` pairs for a select input, skipping pagination. `label` is `bot_name`.

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `limit` | integer | Max rows. | Above 1000 is a **400** (`Limit cannot exceed 1000`), not a silent cap. Omitting it returns at most 1000. |
| `search`, filters | — | Same as List. | — |

```json
{
  "status": true,
  "message": "Dropdown options retrieved successfully",
  "data": [{ "label": "Investment Advisor Bot", "value": "7bf26cf4-…" }],
  "errors": null
}
```

---

### 8. Bulk Delete

**`POST /api/v1/core/agents/bulk_delete/`**

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Agents to soft-delete. | Non-empty list; every id must exist, be undeleted, and be in your tenant. All-or-nothing. |

| Status | When | `message` |
|---|---|---|
| **200** | All soft-deleted; each name freed for reuse. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | Any id unknown, already deleted, or in another tenant; **nothing deleted**. | `Some ids are invalid` |
| **400** | Any agent is still used by a live campaign; **nothing deleted**. | `This bot is used by 2 campaign(s) that are still running. Cancel them before deleting the bot.` |

The live-campaign guard is enforced on the model, so it applies here exactly as it does to
`DELETE /{id}/` — a bulk call cannot route around it.

---

### 9. Export Agents

**`GET /api/v1/core/agents/export/`**

Downloads the current agent list as a spreadsheet. Honours the same `search`, filter, and
`ordering` parameters as List, so **what you filter is what you export** — there is no
"export everything" mode separate from the list view.

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `format` | enum | `csv` (default) or `xlsx`. | Anything else falls back to CSV. |
| `search`, filters, `ordering` | — | Same as List. | — |

**Returns a file, not JSON.** The response is `text/csv` or the XLSX mime type with
`Content-Disposition: attachment` — do not try to parse the standard envelope from it. Errors
(401/403) *do* still come back as JSON, so branch on the content type.

The export is paired with Import below: a round-trip of export → edit → import is the intended
bulk-edit workflow.

---

### 10. Import Agents

**`POST /api/v1/core/agents/import/`** · `Content-Type: multipart/form-data`

Creates or updates agents in bulk from a spreadsheet.

| Field | Type | Description | Validation |
|---|---|---|---|
| `file` * | file | The spreadsheet to import. Columns match the Export output. | Required. Extension must be `.csv`, `.xls`, or `.xlsx`. |

**All-or-nothing.** The file is parsed and validated in a dry run first; only if *every* row passes
is anything written. One bad row on line 400 means nothing at all is imported — you never end up
with a half-applied file to reconcile by hand.

**An `id` column updates rather than creates**, but only resolves **within your own tenant**. A CSV
carrying another tenant's id creates a new row in yours instead of touching theirs, so a leaked
export cannot be used to overwrite someone else's data.

| Status | When | `message` |
|---|---|---|
| **200** | Imported. | `Successfully imported 12 records.` |
| **400** | No file in the request. | `No file provided` |
| **400** | Extension not csv/xls/xlsx. | `Unsupported file format. Use csv or xlsx.` |
| **400** | One or more rows failed validation; **nothing imported**. | `Import failed due to validation errors.` |
| **403** | Caller has no tenant mapping — including every `VB_AGENT` token. Bulk spreadsheet writes are not a machine caller's action. | permission denied |

Row errors are returned as a list so a UI can point at the offending lines:

```json
{
  "status": false,
  "message": "Import failed due to validation errors.",
  "errors": [
    { "row": 2, "error": "bot_name: Agent with this name already exists." },
    { "row": 7, "error": "tone: \"LOUD\" is not a valid choice." }
  ]
}
```

> `row` is the **spreadsheet row number**, counting the header as row 1 — so `row: 2` is the first
> data row.

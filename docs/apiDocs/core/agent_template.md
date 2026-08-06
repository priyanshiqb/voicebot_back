# Agent Template API Documentation

The reusable blueprint an [Agent](./agent.md) is built from: its prompt, its personality settings,
its industry, and the [probing questions](./probing_question.md) it should ask. Create a template
once, then spin up agents from it.

Four things govern this resource:

- **You see your own templates *and* the system-wide defaults.** The list is not a plain tenant
  filter — it returns `tenant = yours OR is_default = true`. Default templates belong to no tenant,
  are visible to every tenant, and can be modified or deleted by nobody.
- **Probing questions are edited incrementally, never as a list.** `probing_questions` is
  read-only; you attach and detach with `add_probing_questions` / `remove_probing_questions`.
  Sending `probing_questions` is an explicit **400**, not a silent ignore.
- **A template in use cannot be deleted.** If any live agent was built from it, the delete is
  refused and names up to three of them.
- **`can_user_edit_flow` is not about editing this template.** It is a flag carried *to* the agents
  built from it. See [the two "editable" flags](#the-two-editable-flags) — it is easy to confuse
  with the probing-question flag of a similar name.

> **VB_AGENT access:** none. This viewset declares no `vb_agent_allowed_actions`, so every action
> returns **403 `This endpoint is not available to agent tokens.`** Templates are human
> configuration; the runtime receives the resolved agent, not the blueprint.
> See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

## Endpoints Summary
- `GET /api/v1/core/agent-templates/` - List templates (paginated)
- `GET /api/v1/core/agent-templates/{id}/` - Retrieve a template
- `POST /api/v1/core/agent-templates/` - Create a template
- `PUT` / `PATCH /api/v1/core/agent-templates/{id}/` - Update a template
- `DELETE /api/v1/core/agent-templates/{id}/` - Soft-delete a template
- `GET /api/v1/core/agent-templates/dropdown/` - Get label/value pairs
- `POST /api/v1/core/agent-templates/bulk_delete/` - Bulk soft-delete
- `GET /api/v1/core/agent-templates/export/` - Download as CSV or XLSX
- `POST /api/v1/core/agent-templates/import/` - Bulk create/update from a file

---

## The two "editable" flags

Two flags in this area sound alike and mean entirely different things. Getting them confused
produces bugs that look like permission errors.

| Flag | Lives on | Controls |
|---|---|---|
| `can_user_edit_flow` | **Agent Template** (this page) | Whether an operator may edit the *conversation flow* of agents built from this template. A downstream feature flag — it does **not** restrict editing the template itself. |
| `can_user_edit` | [Probing Question](./probing_question.md) | Whether that question may be updated through its own API. A genuine write guard. |

**`can_user_edit_flow` is freely writable** here, defaults to `true`, and never blocks a `PATCH` of
this template. The only thing that blocks editing a template is `is_default`.

---

## Default templates

A default template has `is_default: true` and `tenant: null`. Administrators seed them; every
tenant sees them.

| Operation | Behaviour for a default template |
|---|---|
| Appears in your list / dropdown | Yes, alongside your own |
| Update | **400** `Default agent templates cannot be modified.` |
| Delete | **400** `This is a default agent template and cannot be deleted.` |
| Build an agent from it | Yes |

> **Your templates can duplicate a default's name.** Uniqueness is scoped to your tenant and
> defaults have no tenant, so creating your own "Wealth Advisory" alongside the default one
> succeeds and both appear in your list.

---

## Endpoint Details

### 1. List Agent Templates

Your tenant's templates plus the shared defaults, newest first.

- **Path**: `GET /api/v1/core/agent-templates/`
- **Headers**: `Authorization: Bearer <token>`
- **Query Parameters**:

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `page` | integer | Page number. Default `1`. | Beyond the last page → **404**. |
| `page_size` | integer | Rows per page. Default `10`. | Capped at `100`. |
| `search` | string | Partial, case-insensitive match on `name` and `short_description`. | Words split on spaces and **AND**ed. |
| `ordering` | string | Sort. Default `-created_at`. | Only `name`, `created_at`, `updated_at`, `industry`, `is_default`, `strict_facts`, each optionally `-` prefixed. Anything else is ignored. |
| `is_active` | boolean | Exact match. | `true` / `false`. |
| `strict_facts` | boolean | Exact match. | `true` / `false`. |
| `can_user_edit_flow` | boolean | Exact match. | `true` / `false`. |
| `is_default` | boolean | `?is_default=false` narrows to your own templates. | `true` / `false`. |
| `industry` | string | Exact match on industry category. | One of `CLINIC_RECEPTIONIST`, `AGRICULTURAL_SUPPLIER`, `REAL_ESTATE`, `E_COMMERCE`, `RESTAURANT`, `GENERAL_ASSISTANT`, `OTHER`. Unknown value → **400**. |

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
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

> **The list payload omits `agent_prompt` and `probing_questions`.** Prompts run to 50 000
> characters, so shipping them in a list would be wasteful. Call retrieve (§2) for the full record.

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more rows. | `success` |
| **400** | Unknown `industry`, or a non-boolean on a boolean filter. | filter error |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping, or an agent token. | `Your user account is not associated with any active tenant.` / `This endpoint is not available to agent tokens.` |
| **404** | `page` beyond the last page. | `Invalid page.` |

---

### 2. Retrieve an Agent Template

- **Path**: `GET /api/v1/core/agent-templates/{id}/`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Retrieved successfully",
    "data": {
      "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "tenant": { "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c", "name": "Alpha Corp" },
      "name": "Wealth Advisory",
      "short_description": "Custom customer support bot for investments.",
      "visual_icon": "support-icon",
      "primary_accent": "#00FF00",
      "agent_prompt": "You are a customer support agent assisting users with their investments.",
      "industry": "GENERAL_ASSISTANT",
      "strict_facts": true,
      "can_user_edit_flow": true,
      "is_active": true,
      "is_default": false,
      "probing_questions": [
        {
          "id": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
          "question": "What is your account number?",
          "score": 50,
          "priority": 2
        }
      ],
      "created_by": { "id": "9aa85f64-…", "email": "emma@acme.com", "first_name": "Emma", "last_name": "Stone" },
      "updated_by": null,
      "deleted_by": null,
      "created_at": "2026-06-03T18:00:00Z",
      "updated_at": "2026-06-03T18:00:00Z",
      "deleted_at": null
    },
    "errors": null
  }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | Found — one of yours, or a shared default. | `Retrieved successfully` |
| **401** / **403** | As elsewhere. | — |
| **404** | No such id, it is soft-deleted, or it belongs to another tenant and is not a default. | — |

> **`probing_questions` entries can be `null`.** Each is expanded through a hybrid field that
> returns `null` when the related row has been soft-deleted. In normal operation this cannot happen
> — a question attached to a template refuses to be deleted — but a question removed directly in
> the database or admin will surface as a `null` element. Filter nulls before rendering.

---

### 3. Create an Agent Template

- **Path**: `POST /api/v1/core/agent-templates/`
- **Headers**: `Authorization: Bearer <token>`
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
    "add_probing_questions": ["5fa85f64-5717-4562-b3fc-2c963f66afa7"]
  }
  ```

### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `name` * | string | Human-readable template name, e.g. `Wealth Management Advisor`. Shown in pickers. | Required. **2–100 characters**. Letters, digits, underscores, spaces and hyphens only (`^[\w\s-]+$`) — no punctuation, no `&`, no `/`. Unicode letters are accepted. Must be unique among your tenant's live templates; a default with the same name does **not** conflict. |
| `short_description` * | string | One-line summary of what the bot does and who it helps. Shown on template cards. | Required. **10–300 characters**. |
| `agent_prompt` * | string | The core instruction defining personality, constraints, and behaviour. This is what actually drives the agent. | Required. **30–50 000 characters**. |
| `visual_icon` | string | Identifier of a predefined icon for template cards. | Optional, nullable. Max **50** characters. **Not validated against any list** — an unknown name is stored and renders as a missing icon. |
| `primary_accent` | string | Accent colour for template cards and highlights. | Optional, nullable. Must be a **6-digit** hex colour with a leading `#` (`^#[0-9A-Fa-f]{6}$`). Three-digit shorthand like `#FFF` is **rejected**. Empty string and `null` are accepted and mean "no accent". |
| `industry` | choice | Industry category, used for grouping and filtering. | Optional, default `GENERAL_ASSISTANT`. One of `CLINIC_RECEPTIONIST`, `AGRICULTURAL_SUPPLIER`, `REAL_ESTATE`, `E_COMMERCE`, `RESTAURANT`, `GENERAL_ASSISTANT`, `OTHER`. |
| `strict_facts` | boolean | When enabled, agents from this template stick strictly to supplied facts rather than improvising. | Optional, **default `true`**. |
| `can_user_edit_flow` | boolean | Whether operators may edit the conversation flow of agents built from this template. See [the two "editable" flags](#the-two-editable-flags). | Optional, **default `true`**. |
| `is_active` | boolean | Soft on/off switch, independent of deletion. | Optional, default `true`. |
| `add_probing_questions` | UUID array | Questions to attach. Write-only. | Optional. Each id must exist, be undeleted, and either belong to your tenant or be a shared default. |
| `remove_probing_questions` | UUID array | Write-only. **Accepted but has no effect on create** — there is nothing attached yet to remove. | Optional. Ids are still validated for existence, so a bad id here fails the create. |

**Read-only — rejected or ignored if you send them:** `probing_questions` (**400** if present),
`is_default` (always `false`), `id`, `tenant`, `created_at`, `updated_at`, `deleted_at`,
`is_deleted`, `created_by`, `updated_by`, `deleted_by`.

> **`probing_questions` is a hard 400, not a silent ignore.** Sending it returns
> `{"probing_questions": "This field is read-only. Use add_probing_questions/remove_probing_questions instead."}`.
> The rejection is deliberate: a whole-list write means "these and nothing else", so two people
> editing different questions on the same template would silently discard each other's work. The
> incremental fields cannot express that mistake.

- **Success Response (201 Created)**: the full object from §2, with `"message": "Created successfully"`.

### Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **201** | Created, with any `add_probing_questions` already attached. | `Created successfully` | `null` |
| **400** | `name` outside 2–100 characters. | `Name must be between 2 and 100 characters.` | `{"name": [...]}` |
| **400** | `name` contains disallowed characters. | `Template Name can only contain letters, numbers, spaces, hyphens, and underscores.` | `{"name": [...]}` |
| **400** | A live template in your tenant already has this name. | `Agent template with this name already exists.` | `{"name": "…"}` |
| **400** | `short_description` outside 10–300 characters. | `Short description must be between 10 and 300 characters.` | `{"short_description": [...]}` |
| **400** | `agent_prompt` outside 30–50 000 characters. | `Agent prompt must be between 30 and 50000 characters.` | `{"agent_prompt": [...]}` |
| **400** | `primary_accent` is not a 6-digit hex colour. | `Primary accent must be a valid 6-digit hex color code starting with '#' (e.g., #00FF00).` | `{"primary_accent": [...]}` |
| **400** | An id in `add_probing_questions` does not exist or is deleted. | `Probing question <id> does not exist.` | `{"add_probing_questions": "…"}` |
| **400** | An id in `add_probing_questions` belongs to another tenant and is not a default. | `Probing question <id> does not belong to your tenant context and is not a default question.` | `{"add_probing_questions": "…"}` |
| **400** | `probing_questions` was sent. | `This field is read-only. Use add_probing_questions/remove_probing_questions instead.` | `{"probing_questions": "…"}` |
| **400** | `name`, `short_description`, or `agent_prompt` omitted. | `This field is required.` | `{"<field>": [...]}` |
| **400** | Unknown `industry` value. | `"x" is not a valid choice.` | `{"industry": [...]}` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": ...}` |
| **403** | No active tenant mapping, or an agent token. | as above | `{"detail": ...}` |

> **Shape gotcha.** Field validators return a **list** under the field key
> (`{"name": ["Name must be between…"]}`), while the uniqueness and probing-question errors return
> a **bare string** (`{"name": "Agent template with this name already exists."}`). A client doing
> `errors.name[0]` renders the letter `A` for the duplicate case. Read `message` — it is correct
> for both.

---

### 4. Update an Agent Template

- **Path**: `PUT` (full) or `PATCH` (partial) `/api/v1/core/agent-templates/{id}/`
- **Request Payload**: any subset of the writable fields from §3.

**The default check runs before any field validation** — a default template is refused regardless
of what you sent, including an empty `PATCH` body.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Updated. | `Updated successfully` | `null` |
| **400** | The template is a shared default. | `Default agent templates cannot be modified.` | `{"non_field_errors": [...]}` |
| **400** | An id in `remove_probing_questions` does not exist or is deleted. | `Probing question <id> does not exist.` | `{"remove_probing_questions": "…"}` |
| **400** | Any field validation from §3, including the new name colliding with another live template. | as §3 | `{"<field>": …}` |
| **401** / **403** / **404** | As in §2. | — | — |

### Attaching and detaching probing questions

Both lists may be sent in the same request. **Removals are applied first, then additions**, so an
id present in both ends up **attached**.

```json
{ "remove_probing_questions": ["5fa85f64-…"], "add_probing_questions": ["7bf26cf4-…"] }
```

| Behaviour | Detail |
|---|---|
| Adding an already-attached question | No-op. Not an error, and no duplicate row is created. |
| Removing a question that is not attached | No-op. Returns `200`. |
| Removing another tenant's question id | **Accepted and silently no-ops.** Unlike `add_probing_questions`, the remove path checks only that the question exists — not that it is yours. Nothing is detached, since it was never attached. |
| Adding a soft-deleted question | **400** `Probing question <id> does not exist.` |

> **Changes take effect for agents built afterwards.** Attaching a question does not retro-fit
> agents already created from this template — those carry their own question set. Edit the
> [Agent](./agent.md) directly to change a live one.

> **A question attached here cannot be deleted.** The [Probing Question](./probing_question.md)
> delete guard refuses while any live template references it, naming this template. Detach first.

---

### 5. Delete an Agent Template

- **Path**: `DELETE /api/v1/core/agent-templates/{id}/`
- **Success Response (200 OK)**:
  ```json
  { "status": true, "message": "Deleted successfully", "data": null, "errors": null }
  ```

Two guards, first match wins:

1. **Is it a default?** → 400, nobody can delete it.
2. **Is any live agent built from it?** → 400, naming up to three agents.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Soft-deleted. | `Deleted successfully` | `null` |
| **400** | It is a shared default. | `This is a default agent template and cannot be deleted.` | `{"detail": "…"}` |
| **400** | One or more live agents use it. | `This agent template is currently selected in the following agents: SalesBot, SupportBot... and cannot be deleted.` | `{"detail": "…"}` |
| **401** / **403** / **404** | As in §2. | — | — |

> **Names are truncated at three with `...` appended.** Treat the message as a pointer, not a
> complete list — filter agents by `agent_template` to find every one.

**What soft delete does to the name.** So the unique-name constraint does not block a future
template with the same name, deleting one **rewrites its stored name** with a
`-deleted-<YYYYMMDDHHMMSS>` suffix, truncating the original to fit 100 characters. You can
therefore re-create a template with the original name immediately — but a long name is partially
lost in the archived row, so do not treat soft-deleted rows as a faithful record.

> **Attached probing questions are not deleted.** They are detached along with the template row and
> remain available to other templates and agents.

---

### 6. Bulk Delete

- **Path**: `POST /api/v1/core/agent-templates/bulk_delete/`
- **Request Payload**: `{ "ids": ["3fa85f64-…"] }`

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Templates to soft-delete. | Non-empty list. None may be a default; none may be in use by a live agent. Every id must exist, be undeleted, and be visible to you. **All-or-nothing.** |

Checks run in this order:

1. `ids` is a non-empty list → else 400.
2. None of the matched rows is a default → else 400.
3. None of them is used by a live **agent** → else 400.
4. Every id resolves to a visible, undeleted row → else `Some ids are invalid`.

Steps 2–3 only consider ids that resolve to templates **you can see**. An id from another tenant is
not examined for usage; it falls through to step 4 and is reported as invalid like any other
unknown id.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | All soft-deleted. | `Bulk deleted successfully` | `null` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` | `null` |
| **400** | Any selected template is a shared default. | `One or more selected agent templates are default and cannot be deleted.` | `{"detail": "…"}` |
| **400** | Any is used by a live agent. | `One or more selected agent templates are currently selected in an agent and cannot be deleted.` | `{"detail": "…"}` |
| **400** | Any id unknown, already deleted, or not visible to you — **nothing is deleted**. | `Some ids are invalid` | `null` |
| **401** / **403** | As elsewhere. | — | — |

> **Bulk delete names nothing.** Unlike §5 it does not say which template or which agent blocked
> the request. Delete one at a time when you need to know.

> **The `ids must be a non-empty list` response has no `data` key** — it is built by hand rather
> than through the shared helper, so its body is three keys, not four.

> **Duplicate ids fail the whole request** at step 4, because the check compares matched rows
> against the length of `ids`. De-duplicate client-side.

---

### 7. Dropdown

- **Path**: `GET /api/v1/core/agent-templates/dropdown/`
- **Query Parameters**: `limit` (optional, ≤ **1000**; larger → **400 `Limit cannot exceed 1000`**), plus all List filters and `search`.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [ { "label": "Wealth Advisory", "value": "3fa85f64-5717-4562-b3fc-2c963f66afa6" } ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

`label` is the template name, `value` its id. Shared defaults are included and carry no marker — if
your picker needs to separate them, use List (§1) and read `is_default`.

---

### 8. Export

- **Path**: `GET /api/v1/core/agent-templates/export/`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `file_format` | string | `csv` (default) or `xlsx`. | Any other value falls back to CSV rather than erroring. |

All List filters and `search` apply.

**Columns:** `id`, `name`, `short_description`, `industry`, `strict_facts`, `can_user_edit_flow`,
`is_active`, `created_at`.

| Status | When | Body |
|---|---|---|
| **200** | File returned. | Raw bytes — **not** the JSON envelope. `Content-Disposition: attachment; filename="agent-template.csv"` (or `.xlsx`). |
| **401** | Missing or invalid token. | JSON error |
| **403** | No active tenant mapping, or an agent token. | JSON error |

> **`agent_prompt`, `visual_icon`, `primary_accent`, and `probing_questions` are not exported.** The
> export is a catalogue, not a backup — a round-trip through export and import **loses the prompt**,
> which is the part that actually makes the template work. Do not use this to migrate templates
> between environments.

> **Use `file_format`, not `format`.** DRF reserves `format` for content negotiation and rejects
> `?format=csv` with a **404** before this endpoint runs. (Earlier versions of this document showed
> `?format=` — that was wrong.)

---

### 9. Import

Bulk create-or-update from a spreadsheet. Validated as a dry run first and **all-or-nothing**.

- **Path**: `POST /api/v1/core/agent-templates/import/`
- **Headers**: `Authorization: Bearer <token>`, `Content-Type: multipart/form-data`

| Field | Type | Description | Validation |
|---|---|---|---|
| `file` * | file | A `.csv`, `.xls`, or `.xlsx` file whose header row names the columns below. | Required. Any other extension → 400. CSV must be UTF-8 decodable. |

**Recognised columns:** `id`, `name`, `short_description`, `industry`, `strict_facts`,
`can_user_edit_flow`, `is_active`, `created_at`. Any other column is ignored.

- **`id` present and matching one of *your* rows** → that row is updated.
- **`id` absent, or naming a row in another tenant** → a new row is created in your tenant. A
  crafted CSV carrying someone else's UUID cannot reach their record.
- **`tenant` is set for you** from your mapping. `is_default` is not importable.

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Imported 3 new and 1 updated record(s).",
    "data": { "created": 3, "updated": 1, "skipped": 0 }
  }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | Import committed. Counts reflect what was written, not the file's row count. | `Imported N new and M updated record(s).` |
| **400** | No file in the request. | `No file provided` |
| **400** | Extension is not `csv` / `xls` / `xlsx`. | `Unsupported file format. Use csv or xlsx.` |
| **400** | The file could not be parsed. | `Failed to parse file: <reason>` |
| **400** | A row failed during the dry run. Nothing was written. | `Import failed due to validation errors.` |
| **400** | A row failed during the real write and everything was rolled back. | `Import failed and was rolled back.` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping (this includes agent tokens). | `Your user account is not associated with any active tenant.` |

> **Imported templates have an empty `agent_prompt`.** The column is not part of the import
> resource, so newly created rows get an empty string — and an empty prompt is well below the
> 30-character minimum this API enforces everywhere else. The row imports successfully and is then
> **unusable as a template** until someone PATCHes a real prompt in. Plan on a follow-up pass, or
> create templates through `POST` instead.

> **Import does not run this API's validations.** The serializer is bypassed, so the name regex,
> the length rules, and the hex-colour check are **not** applied to imported rows — only
> database-level constraints stop bad data, and the errors you see are raw database messages rather
> than the friendly ones from §3.

> **Probing questions cannot be imported.** Attach them afterwards with
> `add_probing_questions` (§4).

---

## Building an agent from a template

| # | Step | Endpoint |
|---|---|---|
| 1 | Create the template | `POST /agent-templates/` ← this page |
| 2 | Attach probing questions | `PATCH /agent-templates/{id}/` with `add_probing_questions` |
| 3 | Create the agent from it | [`POST /agents/`](./agent.md) with `agent_template` |

The agent copies what it needs at creation time. Later template edits do **not** propagate to
agents already created — which is why a template can be changed freely without auditing every
agent, and equally why fixing a prompt here does not fix it out in production.

## Related

- [Agent API](./agent.md) — what gets built from these
- [Probing Question API](./probing_question.md) — what `add_probing_questions` references
- [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md)

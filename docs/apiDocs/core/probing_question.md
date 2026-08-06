# Probing Question API Documentation

The questions an agent asks during a call to qualify or score the person it is speaking to. Each
one carries a **priority** (what order to ask in) and a **score** (how much the answer is worth),
and gets attached to [Agents](./agent.md) and [Agent Templates](./agent_template.md).

Four behaviours shape everything on this page — read these before you build a UI:

- **You see your own questions *and* the system-wide defaults.** The list is not a plain
  tenant filter: it returns `tenant = yours OR is_default = true`. Default questions belong to no
  tenant (`tenant: null`), are visible to everyone, and can be edited or deleted by no one.
- **Questions you create here are yours to edit; seeded and imported ones may not be.** Editability
  is governed by `can_user_edit`, which is server-controlled — see
  [Who can edit what](#who-can-edit-what).
- **Read-only blocks editing but not deleting.** A question with `can_user_edit: false` still
  deletes successfully. Only `is_default` blocks deletion outright.
- **A question in use cannot be deleted.** If any live agent or agent template references it, the
  delete is refused with the names of what is using it.

> **VB_AGENT access:** none. This viewset declares no `vb_agent_allowed_actions`, so every action
> returns **403 `This endpoint is not available to agent tokens.`** Probing questions are human
> configuration; the runtime receives them as part of the agent's context, not by calling this API.
> See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

## Endpoints Summary
- `GET /api/v1/core/probing-questions/` - List questions (paginated)
- `GET /api/v1/core/probing-questions/{id}/` - Retrieve one question
- `POST /api/v1/core/probing-questions/` - Create a question
- `PUT` / `PATCH /api/v1/core/probing-questions/{id}/` - Update a question
- `DELETE /api/v1/core/probing-questions/{id}/` - Soft-delete a question
- `GET /api/v1/core/probing-questions/dropdown/` - Get label/value pairs
- `POST /api/v1/core/probing-questions/bulk_delete/` - Bulk soft-delete
- `GET /api/v1/core/probing-questions/export/` - Download as CSV or XLSX
- `POST /api/v1/core/probing-questions/import/` - Bulk create/update from a file

---

## Who can edit what

`can_user_edit` decides whether a question can be updated through this API. It is
**server-controlled — read-only on the serializer** — so a client can neither grant nor revoke it.
What it ends up as depends on how the row was created:

| Created via | `can_user_edit` | Editable through this API? |
|---|---|---|
| `POST /probing-questions/` | **`true`** — forced by the server | Yes |
| CSV/XLSX import (§8) | `false` (model default) | No |
| Django admin / database seed | Whatever the administrator set; `false` unless chosen | Depends |

Sending `"can_user_edit": false` in a create payload does **not** produce a locked row — the field
is ignored and the question comes back `true`. That is the point: the flag exists to protect seeded
and imported content, not to let callers mint uneditable rows.

**What still works on a read-only question:**

| Operation | Allowed? |
|---|---|
| Read (list, retrieve, dropdown, export) | Yes |
| Update (`PUT` / `PATCH`) | **No** — 400 |
| Delete (`DELETE`, `bulk_delete`) | **Yes**, provided it is not default and not in use |
| Attach to an agent or template | Yes |

So for an imported question, the way to change it is **delete it and create a replacement** —
unless an agent already uses it, in which case detach it there first. Alternatively, have an
administrator set `can_user_edit = true` in the admin.

> **This changed.** Questions created through the API previously inherited the model default
> `false` and so were uneditable the moment they were created — `POST` then `PATCH` returned
> *"This probing question is read-only and cannot be modified."* If your client works around that
> by recreating instead of patching, it no longer needs to.

---

## Default questions

A default question has `is_default: true` and `tenant: null`. They are seeded by administrators and
shared across every tenant.

| Operation | Behaviour for a default question |
|---|---|
| Appears in your list / dropdown | Yes, alongside your own |
| Update | **400** `Default probing questions cannot be modified.` |
| Delete | **400** `This is a default probing question and cannot be deleted.` |
| Attach to your agent / template | Yes |

> **Your questions can duplicate a default's text.** The uniqueness rule is scoped to your tenant,
> and defaults have no tenant, so creating your own copy of a default's wording succeeds. Both then
> appear in your list. Deduplicate in the UI if that matters to you.

---

## Endpoint Details

### 1. List Probing Questions

Your tenant's questions plus the shared defaults, ordered by `priority` ascending then newest
first — i.e. already in the order an agent would ask them.

- **Path**: `GET /api/v1/core/probing-questions/`
- **Headers**: `Authorization: Bearer <token>`
- **Query Parameters**:

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `page` | integer | Page number. Default `1`. | Beyond the last page → **404**. |
| `page_size` | integer | Rows per page. Default `10`. | Capped at `100`. |
| `search` | string | Partial, case-insensitive match on `question` text. | Words split on spaces and **AND**ed. |
| `ordering` | string | Sort. Default `priority,-created_at`. | Only `priority`, `score`, `created_at`, `is_mandatory`, `is_active`, `question`, each optionally `-` prefixed. Anything else is ignored. |
| `is_active` | boolean | Exact match. | `true` / `false`. |
| `is_mandatory` | boolean | Only questions the agent must always ask. | `true` / `false`. |
| `can_user_edit` | boolean | Useful for showing which rows your UI should render as editable. | `true` / `false`. |
| `is_default` | boolean | `?is_default=false` narrows to just your tenant's own questions. | `true` / `false`. |

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
        "can_user_edit": false,
        "is_default": false,
        "is_active": true,
        "created_at": "2026-06-03T18:00:00Z"
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

> **The list payload omits `tenant`.** To tell one of your own questions from a shared default,
> read `is_default` — not the absence of a tenant, which is only visible on retrieve.

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more rows. Never empty in practice if defaults are seeded. | `success` |
| **400** | A non-boolean value on a boolean filter. | filter error |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping, or an agent token. | `Your user account is not associated with any active tenant.` / `This endpoint is not available to agent tokens.` |
| **404** | `page` beyond the last page. | `Invalid page.` |

---

### 2. Create Probing Question

- **Path**: `POST /api/v1/core/probing-questions/`
- **Headers**: `Authorization: Bearer <token>`
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

### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `question` * | string | The wording the agent speaks. Write it as a complete question — it is used verbatim. | Required. **10–100 characters** (checked on the raw string, so leading/trailing spaces count). Must be unique among your tenant's live questions; a shared default with the same text does **not** conflict. |
| `priority` * | integer | Ask-order relative to the agent's other questions. Lower is asked first. | Required. Integer **≥ 1**. `0` and negatives → 400. Not unique — two questions may share a priority, and their relative order is then by newest-first. |
| `score` * | integer | Weight this question's answer carries in lead scoring. | Required. Integer **0–100** inclusive. |
| `is_mandatory` | boolean | Whether the agent must ask this in every interaction rather than treating it as optional. | Optional, default `false`. |
| `is_active` | boolean | Soft on/off switch, independent of deletion. Inactive questions stay attached but should not be asked. | Optional, default `true`. |

**Read-only — ignored if you send them:** `can_user_edit` (always `true` on create),
`is_default` (always `false`), `id`, `tenant`, `created_at`, `updated_at`, `deleted_at`,
`is_deleted`, `created_by`, `updated_by`, `deleted_by`.

> **`priority` and `score` are `PositiveIntegerField`s.** A negative value is rejected with the
> validator message; a non-integer (`"high"`, `1.5`) is rejected by DRF's field parsing first, with
> a different message.

- **Success Response (201 Created)**:
  ```json
  {
    "status": true,
    "message": "Created successfully",
    "data": {
      "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c",
      "tenant": { "id": "3fa85f64-…", "name": "Alpha Corp", "subdomain": "alpha-corp" },
      "question": "What is your primary investment goal?",
      "priority": 1,
      "score": 80,
      "is_mandatory": true,
      "can_user_edit": true,
      "is_default": false,
      "is_active": true,
      "created_at": "2026-06-03T18:00:00Z",
      "updated_at": "2026-06-03T18:00:00Z",
      "deleted_at": null,
      "created_by": { "id": "5fa85f64-…", "email": "alex@acme.com" },
      "updated_by": { "id": "5fa85f64-…", "email": "alex@acme.com" },
      "deleted_by": null
    },
    "errors": null
  }
  ```

> **`can_user_edit` comes back `true` even if you sent `false`.** The server owns this field — see
> [Who can edit what](#who-can-edit-what).

### Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **201** | Created. | `Created successfully` | `null` |
| **400** | `question` shorter than 10 or longer than 100 characters. | `Question must be between 10 and 100 characters.` | `{"question": [...]}` |
| **400** | A live question with this exact text already exists in your tenant. | `Probing question with this text already exists.` | `{"question": "…"}` |
| **400** | `priority` below 1. | `Priority must be at least 1.` | `{"priority": [...]}` |
| **400** | `score` outside 0–100. | `Score must be between 0 and 100.` | `{"score": [...]}` |
| **400** | `question`, `priority`, or `score` omitted. | `This field is required.` | `{"<field>": [...]}` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": ...}` |
| **403** | No active tenant mapping, or an agent token. | as above | `{"detail": ...}` |

> **Shape gotcha on the duplicate error.** Field validators return a **list** under the field key
> (`{"question": ["Question must be between…"]}`), but the uniqueness check returns a **bare
> string** (`{"question": "Probing question with this text already exists."}`). If your client does
> `errors.question[0]`, the duplicate case will render the letter `P`. Read `message` instead — it
> is correct in both cases.

---

### 3. Retrieve a Probing Question

- **Path**: `GET /api/v1/core/probing-questions/{id}/`
- **Success Response (200 OK)**: the full object from §2, with `"message": "Retrieved successfully"`.

| Status | When | `message` |
|---|---|---|
| **200** | Found — one of yours, or a shared default. | `Retrieved successfully` |
| **401** / **403** | As elsewhere. | — |
| **404** | No such id, it is soft-deleted, or it belongs to another tenant and is not a default. | — |

---

### 4. Update a Probing Question

- **Path**: `PUT` (full) or `PATCH` (partial) `/api/v1/core/probing-questions/{id}/`
- **Request Payload**: any subset of `question`, `priority`, `score`, `is_mandatory`, `is_active`.

**The eligibility check runs before any field validation.** A read-only or default question is
refused regardless of what you sent — even an empty `PATCH` body.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Updated. | `Updated successfully` | `null` |
| **400** | The question is a shared default. | `Default probing questions cannot be modified.` | `{"non_field_errors": [...]}` |
| **400** | `can_user_edit` is `false` on this question — typically an imported or admin-seeded one. See [Who can edit what](#who-can-edit-what). | `This probing question is read-only and cannot be modified.` | `{"non_field_errors": [...]}` |
| **400** | Any field validation from §2, including the new text colliding with another live question. | as §2 | `{"<field>": …}` |
| **401** / **403** / **404** | As in §3. | — | — |

> **Changing a question's text does not change what past calls asked.** Answers already captured
> reference the question row, so editing the wording retroactively re-labels historical results.
> If that matters for your reporting, create a new question and deactivate the old one instead.

---

### 5. Delete a Probing Question

Soft delete, with two guards in front of it.

- **Path**: `DELETE /api/v1/core/probing-questions/{id}/`
- **Success Response (200 OK)**:
  ```json
  { "status": true, "message": "Deleted successfully", "data": null, "errors": null }
  ```

The guards run in this order, and the first one that trips wins:

1. **Is it a default?** → 400, cannot be deleted by anyone.
2. **Is any live agent using it?** → 400, listing up to three agent bot names.
3. **Is any live agent template using it?** → 400, listing up to three template names.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Soft-deleted. | `Deleted successfully` | `null` |
| **400** | It is a shared default. | `This is a default probing question and cannot be deleted.` | `{"detail": "…"}` |
| **400** | One or more live agents reference it. | `This probing question is currently selected in the following agents: SalesBot, SupportBot... and cannot be deleted.` | `{"detail": "…"}` |
| **400** | One or more live agent templates reference it. | `This probing question is currently selected in the following agent templates: Finance Intake... and cannot be deleted.` | `{"detail": "…"}` |
| **401** / **403** / **404** | As in §3. | — | — |

> **`can_user_edit: false` does **not** block deletion.** Only defaults and in-use questions are
> protected. A question you cannot edit, you can still remove — which is the practical way to
> "change" one.

> **The names are truncated at three, with `...` appended.** Use the message to point the operator
> at where to look, not as a complete list. To find every user, filter agents and templates by this
> question id.

**What soft delete does to the text.** To keep the unique-text constraint from blocking a future
question with the same wording, deleting a question **rewrites its stored text** with a
`-deleted-<YYYYMMDDHHMMSS>` suffix (truncating the original to fit 100 characters). Two
consequences:

- You can immediately re-create a question with the original wording. No waiting, no conflict.
- The original text is **partially lost** for long questions — a 95-character question keeps only
  its first 79 characters in the archived row. Do not treat soft-deleted rows as a faithful audit
  of what was asked.

---

### 6. Bulk Delete

Same guards as §5, applied to the whole set — but checked in a different order, and reported
without naming what blocked the request.

- **Path**: `POST /api/v1/core/probing-questions/bulk_delete/`
- **Request Payload**:
  ```json
  { "ids": ["7bf26cf4-e53b-48bb-bb82-261ee976378c"] }
  ```

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Questions to soft-delete. | Non-empty list. None may be a default; none may be referenced by a live agent or agent template. Every id must exist, be undeleted, and be visible to you. **All-or-nothing.** |

Checks run in this order:

1. `ids` is a non-empty list → else 400.
2. None of the matched rows is a default → else 400.
3. None of them is referenced by a live **agent** → else 400.
4. None of them is referenced by a live **agent template** → else 400.
5. Every id resolves to a visible, undeleted row → else `Some ids are invalid`.

Steps 2–4 only consider ids that resolve to questions **you can see**. An id belonging to another
tenant is not examined for usage; it simply falls through to step 5 and is reported as invalid like
any other unknown id.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | All soft-deleted. | `Bulk deleted successfully` | `null` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` | `null` |
| **400** | Any selected question is a shared default. | `One or more selected probing questions are default and cannot be deleted.` | `{"detail": "…"}` |
| **400** | Any is referenced by a live agent. | `One or more selected probing questions are currently selected in an agent and cannot be deleted.` | `{"detail": "…"}` |
| **400** | Any is referenced by a live agent template. | `One or more selected probing questions are currently selected in an agent template and cannot be deleted.` | `{"detail": "…"}` |
| **400** | Any id is unknown, already deleted, or not visible to you — **nothing is deleted**. | `Some ids are invalid` | `null` |
| **401** / **403** | As elsewhere. | — | — |

> **Bulk delete names nothing.** Unlike the single delete, these messages do not say *which*
> question or *which* agent blocked the request. Delete one at a time when you need to know.

> **The `ids must be a non-empty list` response has no `data` key.** This one error is built
> by hand rather than through the shared helper, so its body is
> `{"status": false, "message": …, "errors": null}` — three keys, not four. Do not assume `data` is
> present when parsing errors from this endpoint.

> **Duplicate ids fail the whole request** at step 5, because the check compares matched rows
> against the length of `ids`. De-duplicate client-side.

---

### 7. Export

- **Path**: `GET /api/v1/core/probing-questions/export/`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `file_format` | string | `csv` (default) or `xlsx`. | Any other value falls back to CSV rather than erroring. |

All List filters and `search` apply, so you can export a subset.

**Columns:** `id`, `question`, `priority`, `score`, `is_mandatory`, `is_active`, `created_at`.

| Status | When | Body |
|---|---|---|
| **200** | File returned. | Raw bytes — **not** the JSON envelope. `Content-Disposition: attachment; filename="probing-question.csv"` (or `.xlsx`). |
| **401** | Missing or invalid token. | JSON error |
| **403** | No active tenant mapping, or an agent token. | JSON error |

> **Use `file_format`, not `format`.** DRF reserves `format` for content negotiation and rejects
> `?format=csv` with a **404** before this endpoint runs. (Earlier versions of this document showed
> `?format=` — that was wrong.)

> **The export includes shared defaults**, because the list queryset does. Filter with
> `?is_default=false` if you want only your own.

---

### 8. Import

Bulk create-or-update from a spreadsheet. Validated as a dry run first and **all-or-nothing**: if
any row fails, nothing is written.

- **Path**: `POST /api/v1/core/probing-questions/import/`
- **Headers**: `Authorization: Bearer <token>`, `Content-Type: multipart/form-data`

| Field | Type | Description | Validation |
|---|---|---|---|
| `file` * | file | A `.csv`, `.xls`, or `.xlsx` file whose header row names the columns below. | Required. Any other extension → 400. CSV must be UTF-8 decodable. |

**Recognised columns:** `id`, `question`, `priority`, `score`, `is_mandatory`, `is_active`,
`created_at`. Any other column is ignored.

- **`id` present and it matches one of *your* rows** → that row is updated.
- **`id` absent, or it names a row in another tenant** → a new row is created in your tenant. A
  crafted CSV carrying someone else's UUID cannot reach their record.
- **`tenant` is set for you** from your mapping. `can_user_edit` and `is_default` are not
  importable and take their model defaults (`false`) on new rows.

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Imported 5 new and 2 updated record(s).",
    "data": { "created": 5, "updated": 2, "skipped": 0 }
  }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | Import committed. Counts come from what was actually written, not the file's row count. | `Imported N new and M updated record(s).` |
| **400** | No file in the request. | `No file provided` |
| **400** | Extension is not `csv` / `xls` / `xlsx`. | `Unsupported file format. Use csv or xlsx.` |
| **400** | The file could not be parsed (bad encoding, corrupt workbook). | `Failed to parse file: <reason>` |
| **400** | A row failed during the dry run. Nothing was written. | `Import failed due to validation errors.` |
| **400** | A row failed during the real write, e.g. a concurrent insert landed in between. Everything was rolled back. | `Import failed and was rolled back.` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping (this includes agent tokens). | `Your user account is not associated with any active tenant.` |

Row errors are reported with the file's row number:
```json
{
  "status": false,
  "message": "Import failed due to validation errors.",
  "errors": [ { "row": 3, "error": "value too long for type character varying(100)" } ]
}
```

> **Import does not run this API's validations.** The serializer is bypassed entirely, so the
> 10-character minimum, the 0–100 score range, the `priority >= 1` rule, and the read-only/default
> guards are **not** applied to imported rows. Only database-level constraints stop bad data —
> which means the errors you see are raw database messages, not the friendly ones from §2.
> Validate your spreadsheet before uploading; a 5-character question will import successfully and
> then fail validation forever after if anyone tries to edit it through the API.

> **Superusers import without a tenant scope.** Every other caller is confined to their own rows.

---

### 9. Dropdown

- **Path**: `GET /api/v1/core/probing-questions/dropdown/`
- **Query Parameters**: `limit` (optional, ≤ **1000**; larger → **400 `Limit cannot exceed 1000`**), plus all List filters and `search`.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [ { "label": "What is your primary investment goal?", "value": "7bf26cf4-…" } ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

The `label` is the full question text and the `value` is its id. Shared defaults are included and
are **not marked** — the dropdown payload has no `is_default` flag, so if your picker needs to
distinguish them, call List (§1) instead.

---

## Where questions are used

| Consumer | Relationship | Effect |
|---|---|---|
| [Agent](./agent.md) | `agent.probing_questions` (many-to-many) | The live set an agent asks. Attaching one here is what blocks its deletion. |
| [Agent Template](./agent_template.md) | `template.probing_questions` (many-to-many) | Seeds the question set for agents created from the template. |

Attach and detach from those endpoints — there is no "add to agent" action on this page. Because
attachment blocks deletion, the order of operations for removing a question is: detach from every
agent and template, then delete.

## Related

- [Agent API](./agent.md)
- [Agent Template API](./agent_template.md)
- [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md)

# Custom Voice API Documentation

A tenant-scoped store for custom (cloned or uploaded) voice records. This endpoint is a
**pass-through JSON bucket**: it accepts whatever object you post, stores it verbatim in a single
JSON column, and returns that same object back — unchanged, unvalidated, and unenriched.

That design is deliberate — the voice payload's shape is owned by the voice-cloning provider, not
by this backend, so it is not modelled here. But it has consequences you must design around:

- **Nothing is validated.** There are no required fields, no type checks, and no schema. `{}` is a
  valid record. A typo in a key name is stored as a typo.
- **Two fields are added on read.** Responses return your payload **plus** `id` (the row's
  primary key) and `created_at`. Everything else about the row — `tenant`, `is_active`, the audit
  fields — stays hidden. See [Addressing a record](#addressing-a-record).
- **Records cannot be updated.** `PUT` and `PATCH` are not routed. Replace by deleting and
  re-creating.

**Base path:** `/api/v1/core/custom-voices/`

> **VB_AGENT access:** none. This viewset declares no `vb_agent_allowed_actions`, so every action
> returns **403 `This endpoint is not available to agent tokens.`**
> See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

## Endpoints Summary
- `GET /api/v1/core/custom-voices/` - List records (paginated)
- `GET /api/v1/core/custom-voices/{id}/` - Retrieve one record
- `POST /api/v1/core/custom-voices/` - Create a record
- `DELETE /api/v1/core/custom-voices/{id}/` - Soft-delete a record
- `POST /api/v1/core/custom-voices/bulk_delete/` - Bulk soft-delete
- `GET /api/v1/core/custom-voices/dropdown/` - *Present but not useful* — see §7

**`PUT` and `PATCH` are not routed** — the viewset allows only `GET`, `POST`, `DELETE`. Either
returns **405 Method Not Allowed**.

---

## Addressing a record

**The `{id}` in `GET /{id}/` and `DELETE /{id}/` is the database row's primary key**, and every
response includes it as `id` — including the `201` from create and each entry in the list. Take the
`id` from any response and it works directly in the detail URL.

Two server-owned fields are merged into the blob on read:

| Key | Type | Meaning |
|---|---|---|
| `id` | UUID string | The row's primary key. Use this in `GET /{id}/`, `DELETE /{id}/`, and `bulk_delete`. |
| `created_at` | ISO-8601 datetime | When the row was stored, from the model column — not from anything in your payload. |

> **Server values win over your payload's keys.** If you post `"id": "provider-side-identifier"`,
> the value is still stored verbatim in the database, but the response returns the row's real
> primary key under `id`. A client-supplied `id` never routes, so letting it shadow the real key
> would put you back to unaddressable records. If you need to keep a provider's identifier, send it
> under a different key such as `provider_voice_id`.

> **Everything else about the row stays hidden.** `tenant`, `is_active`, `is_deleted`,
> `updated_at`, and the `created_by` / `updated_by` / `deleted_by` audit fields are not exposed by
> this API.

---

## What gets stored

| Layer | Contents |
|---|---|
| JSON column (`data`) | Your payload, byte-for-byte — including any `id` or `created_at` keys you sent. |
| Model columns | `id` (UUID pk), `tenant`, `is_active`, `is_deleted`, `deleted_at`, `created_at`, `updated_at`, `created_by`, `updated_by`, `deleted_by`. Only `id` and `created_at` are returned. |

The response body is the JSON column with the two returned model columns merged over the top.

> **Keys in your JSON never touch the columns of the same name.** Posting `"is_active": false` sets
> a key inside the blob; the model's `is_active` column stays `true`. The `?is_active=` filter reads
> the **column**, not your key — so it will not filter on what you think it does.

---

## Endpoint Details

### 1. List Custom Voice Records

Returns every non-deleted record for your tenant, newest first (by the model's `created_at`
column, not by any `created_at` inside your JSON).

- **Path**: `GET /api/v1/core/custom-voices/`
- **Headers**: `Authorization: Bearer <token>`
- **Query Parameters**:

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `page` | integer | Page number. Default `1`. | Beyond the last page → **404**. |
| `page_size` | integer | Rows per page. Default `10`. | Capped at `100`. |
| `is_active` | boolean | Filters the model **column**, which nothing on this API can change — so every live record matches `true`. Effectively inert unless rows were edited in the admin. | `true` / `false`. |
| `ordering` | string | Sort. Default `-created_at`. | Only `created_at` / `-created_at`. Anything else is ignored. |

> **No search, and no filtering inside the JSON.** `search_fields` is empty, so `?search=` is
> accepted and silently does nothing. There is no way to query by `name`, `language`, or any other
> key in your payload — fetch the page and filter client-side.

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "user_id": "org_38ZJVLbnoN7QaiDCE7m7RaSDZuA",
        "is_public": false,
        "access": { "type": "private", "visibility": "owner" },
        "name": "Riley — warm",
        "description": "Warm female narration voice",
        "language": "en",
        "record_type": "upload",
        "clone_type": "instant",
        "id": "aa45d90c-f443-4583-a8e2-64dbe0d73cc0",
        "created_at": "2026-07-16T05:17:27.109000Z"
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

Each entry is the JSON that was posted for that record, plus `id` and `created_at`. Every other key
shown above came from the payload — if the poster omitted `description`, it is absent here.

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more records. An empty `data` array is normal. | `success` |
| **400** | Non-boolean value for `is_active`. | filter error |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping, or an agent token. | `Your user account is not associated with any active tenant.` / `This endpoint is not available to agent tokens.` |
| **404** | `page` beyond the last page. | `Invalid page.` |

---

### 2. Retrieve a Custom Voice Record

- **Path**: `GET /api/v1/core/custom-voices/{id}/`
- **Path Parameters**:

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `id` * | UUID | The row's primary key, as returned in the `id` of any create or list response. | Must be a well-formed UUID; anything else → **404**. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Retrieved successfully",
    "data": {
      "user_id": "org_38ZJVLbnoN7QaiDCE7m7RaSDZuA",
      "name": "Riley — warm",
      "language": "en",
      "record_type": "upload",
      "clone_type": "instant",
      "id": "aa45d90c-f443-4583-a8e2-64dbe0d73cc0",
      "created_at": "2026-07-16T05:17:27.109000Z"
    },
    "errors": null
  }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | Found. `data` is the stored blob. | `Retrieved successfully` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | No tenant mapping, or an agent token. | as above |
| **404** | No such row, it is soft-deleted, it belongs to another tenant, or `{id}` is not a valid UUID. | `Not found.` |

> **Cross-tenant access is a 404, not a 403.** The queryset is filtered to your tenant before the
> lookup runs, so another tenant's record is indistinguishable from one that does not exist. (A
> `403` here means *you* have no tenant mapping, or you used an agent token — it never means "that
> record belongs to someone else".)

---

### 3. Create a Custom Voice Record

- **Path**: `POST /api/v1/core/custom-voices/`
- **Headers**: `Authorization: Bearer <token>`, `Content-Type: application/json`

### Request payload

**The entire request body is the payload.** There is no envelope and no named field — whatever JSON
object you send becomes the record.

| Field | Type | Description | Validation |
|---|---|---|---|
| *(request body)* | JSON object | The complete voice record as your voice provider defines it. Stored verbatim. | **None.** Any valid JSON is accepted, including `{}`. No key is required, no type is checked, no size limit is enforced by this API. |

There are no `*`-required fields to list, because the API requires none. The keys below are the
shape the frontend currently sends — a convention, not a contract this API enforces:

| Conventional key | Type | Meaning |
|---|---|---|
| `user_id` | string | The voice provider's owning-account identifier. |
| `name` | string | Display name shown in voice pickers. |
| `description` | string | Free text. |
| `language` | string | Language code, e.g. `en`. |
| `record_type` | string | How the voice was produced, e.g. `upload`. |
| `clone_type` | string | Cloning method, e.g. `instant`. |
| `is_public` | boolean | Provider-side visibility. Unrelated to this API's access control. |
| `access` | object | Provider-side access descriptor, e.g. `{"type": "private", "visibility": "owner"}`. |

- **Request example**:
  ```json
  {
    "user_id": "org_38ZJVLbnoN7QaiDCE7m7RaSDZuA",
    "is_public": false,
    "access": { "type": "private", "visibility": "owner" },
    "name": "Riley — warm",
    "description": "Warm female narration voice",
    "language": "en",
    "record_type": "upload",
    "clone_type": "instant"
  }
  ```

- **Success Response (201 Created)**:
  ```json
  {
    "status": true,
    "message": "Created successfully",
    "data": {
      "user_id": "org_38ZJVLbnoN7QaiDCE7m7RaSDZuA",
      "is_public": false,
      "access": { "type": "private", "visibility": "owner" },
      "name": "Riley — warm",
      "description": "Warm female narration voice",
      "language": "en",
      "record_type": "upload",
      "clone_type": "instant",
      "id": "aa45d90c-f443-4583-a8e2-64dbe0d73cc0",
      "created_at": "2026-07-29T10:15:20.109000Z"
    },
    "errors": null
  }
  ```

> **The response echoes your request, plus `id` and `created_at`.** Nothing else is added — no
> `tenant`, no audit fields. Store the returned `id`; it is what addresses this record from now on.

> **Duplicates are allowed.** There is no uniqueness rule of any kind. Posting the same payload
> five times creates five separate rows with five different `id`s and otherwise identical content.

### Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **201** | Record stored. | `Created successfully` | `null` |
| **400** | The body is not parseable JSON. This is the **only** way to get a 400 from this endpoint. | `JSON parse error - <detail>` | `{"detail": "…"}` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": ...}` |
| **403** | No active tenant mapping, or an agent token. | as above | `{"detail": ...}` |

> **A malformed *record* is not an error — only malformed *JSON* is.** `{"nmae": "typo"}` returns
> `201`. Validate the payload shape client-side; the server will not do it for you.

---

### 4. Delete a Custom Voice Record

- **Path**: `DELETE /api/v1/core/custom-voices/{id}/`
- **Path Parameters**: `id` — the database primary key, as in §2.
- **Success Response (200 OK)**:
  ```json
  { "status": true, "message": "Deleted successfully", "data": null, "errors": null }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | Soft-deleted; it disappears from list and retrieve. | `Deleted successfully` |
| **401** / **403** | As in §2. | — |
| **404** | No such row, already deleted, another tenant's, or a malformed UUID. | `Not found.` |

**Soft delete** — the row and its JSON survive in the database with `is_deleted: true`. Nothing on
this API can restore it.

> **Deleting a voice does not detach it from agents.** [Agent](./agent.md) references a voice by
> the free-text `voice_name` string, not by a foreign key, so an agent pointing at a deleted voice
> keeps its `voice_name` and no error is raised here or there. Check for agents using the voice
> before deleting it.

---

### 5. Bulk Delete

- **Path**: `POST /api/v1/core/custom-voices/bulk_delete/`
- **Request Payload**:
  ```json
  { "ids": ["aa45d90c-f443-4583-a8e2-64dbe0d73cc0", "…"] }
  ```

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | **Database primary keys** to soft-delete. | Non-empty list. Every id must exist, be undeleted, and be in your tenant. All-or-nothing. |

| Status | When | `message` |
|---|---|---|
| **200** | All soft-deleted. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | Any id unknown, already deleted, or another tenant's — **nothing is deleted**. | `Some ids are invalid` |
| **401** / **403** | As elsewhere. | — |

> **Duplicate ids fail the whole request**, because the check compares matched rows against the
> length of `ids`. De-duplicate client-side.

---

### 6. Update — not available

`PUT` and `PATCH` are not routed on this viewset.

| Status | When | `message` |
|---|---|---|
| **405** | Any `PUT` or `PATCH` request. | `Method "PATCH" not allowed.` |

To change a record, delete it and create a replacement. Note that this produces a new `id`, so
anything holding the old one is stale.

---

### 7. Dropdown — present, but not a dropdown

- **Path**: `GET /api/v1/core/custom-voices/dropdown/`

The route exists because it is inherited from the shared base viewset, but **no dropdown serializer
is configured for this resource**, so it falls back to the normal serializer and returns full JSON
blobs — not `{label, value}` pairs like every other dropdown in this API.

It is therefore equivalent to List (§1) with a `limit` parameter. Use List instead and build your
own options — `value` from `id`, `label` from whichever key your payloads use for the display name.

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `limit` | integer | Caps the number of records. | Must be ≤ **1000**; larger → **400 `Limit cannot exceed 1000`**. Non-numeric values are ignored. |

---

## Known limitations

Recorded here so they are not rediscovered as bugs:

| # | Limitation | Impact |
|---|---|---|
| 1 | No validation of any kind on the payload. | Malformed records are stored silently and only fail downstream. |
| 2 | No update path. | Editing means delete + create, which changes the `id`. |
| 3 | No uniqueness. | Duplicate rows differ only by `id`. |
| 4 | `?is_active=` and `?search=` are effectively inert. | Filtering must be done client-side. |
| 5 | `dropdown` does not return `{label, value}`. | Do not wire a picker to it; use List and build options from `id` + your `name` key. |
| 6 | `Agent.voice_name` is a free-text string, not a foreign key. | Deleting a voice leaves agents pointing at a name that no longer resolves; nothing warns you. |

## Related

- [Agent API](./agent.md) — `voice_name` is where a voice is selected for an agent
- [Agent Template API](./agent_template.md)
- [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md)

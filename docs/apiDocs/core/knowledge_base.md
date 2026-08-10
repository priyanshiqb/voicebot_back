# Knowledge Base API Documentation

What an [Agent](./agent.md) knows. A knowledge base is a **named container for four kinds of source
material** — free text, uploaded documents, websites to crawl, and question/answer pairs — which
are shipped to an external ETL service, embedded, and made available to the bot during calls.

Five things govern everything on this page:

- **It is one nested write, not five endpoints.** You create a knowledge base *and* its texts,
  files, websites, and Q&A pairs in a single `POST`. Components have no top-level CRUD of their own;
  you edit them through the parent using the [flag system](#the-flag-system).
- **Files are uploaded before they are attached.** Upload first, get an id, then pass that id in the
  `files` array. Two upload paths exist — [pick one](#file-uploads).
- **Every write triggers ingestion, synchronously.** Create, update, and delete all make an
  outbound call to the ETL service before responding. See
  [Ingestion and the ETL lifecycle](#ingestion-and-the-etl-lifecycle) — the most surprising
  behaviour here.
- **A `201` does not mean it was ingested.** It means the record was stored and ingestion was
  *attempted*. Read `processing_status`.
- **Bulk delete lives at a different URL than everywhere else** — `/bulk-delete/` with a hyphen,
  not `/bulk_delete/`. See [§11](#11-bulk-delete).

> **VB_AGENT access:** none for the ordinary endpoints — this viewset declares no
> `vb_agent_allowed_actions`, so agent tokens get **403**. The one exception is
> [`update-etl-status`](#13-update-etl-status-etl-service-only), which uses a separate
> authentication class for the ETL service.
> See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

**Base path:** `/api/v1/core/knowledge-base/`

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/knowledge-base/` | List knowledge bases (paginated) |
| `GET` | `/knowledge-base/{id}/` | Retrieve one, with all components |
| `POST` | `/knowledge-base/` | Create, with nested components |
| `PUT` / `PATCH` | `/knowledge-base/{id}/` | Update, using the flag system |
| `DELETE` | `/knowledge-base/{id}/` | Soft-delete (needs `?force=true` if in use) |
| `POST` | `/knowledge-base/upload_file/` | Upload a document directly (multipart) |
| `POST` | `/knowledge-base/request-upload-url/` | Presigned S3 upload — step 1 |
| `POST` | `/knowledge-base/confirm-upload/` | Presigned S3 upload — step 2 |
| `POST` | `/knowledge-base/discard-files/` | Permanently delete unattached uploads |
| `GET` | `/knowledge-base/dropdown/` | Label/value picker options |
| `POST` | `/knowledge-base/bulk-delete/` | Bulk soft-delete (**hyphen**) |
| `POST` | `/knowledge-base/check-name/` | Is this name free? |
| `POST` | `/knowledge-base/check-url/` | Is this website URL free? |
| `POST` | `/knowledge-base/{id}/update-etl-status/` | ETL service callback |

---

## Ingestion and the ETL lifecycle

**Creating, updating, or deleting a knowledge base makes a blocking HTTP call to the ETL service
before your request returns.** It is not queued to a background worker.

What happens on every write:

1. A fresh `etl_id` is generated and stamped on the knowledge base.
2. `processing_status` is set to `PENDING`, and the previous error and timestamps are cleared.
3. The **whole** knowledge base — every text, file, website, and Q&A — is serialised and `POST`ed
   to `ETL_SERVER_URL` with a **10-second timeout**.
4. The ETL service later calls back to [`update-etl-status`](#13-update-etl-status-etl-service-only)
   as each component progresses.

Two consequences to design around:

> **Your request can take up to ~10 seconds.** A slow or unreachable ETL service is paid for by the
> API caller. Set client timeouts accordingly, and do not put this on a UI path that cannot show a
> spinner.

> **If the ETL call fails, you still get a `201` / `200`.** The failure is caught, and the knowledge
> base is moved to `processing_status: FAILED` with `processing_error` set — but the HTTP response
> is still a success, because the record *was* saved. **Always read `processing_status` from the
> response body**; never infer ingestion from the status code.

### `processing_status` values

Carried by the knowledge base **and independently by each component**, so one failed file does not
mark the whole base failed.

| Status | Meaning |
|---|---|
| `PENDING` | Queued for ingestion. Set on every write. |
| `PROCESSING` | The ETL service has started on it. |
| `COMPLETED` | Embedded and usable. `is_processed` becomes `true` and `processing_error` is cleared. |
| `FAILED` | Ingestion failed. `processing_error` explains why; `is_processed` stays `false`. |

Matching timestamps — `pending_at`, `processing_at`, `completed_at`, `failed_at` — are stamped as
each state is entered. They are never individually cleared, so a component that failed and later
succeeded carries both `failed_at` and `completed_at`; compare them to see which came last.

> **`etl_id` is a fencing token.** Each write mints a new one, and a status callback carrying a
> stale `etl_id` is **rejected and ignored**. That is what stops an abandoned ingestion run from
> overwriting the state of the current one. It also means editing a knowledge base while ingestion
> is running discards the in-flight results — the edit restarts the whole pipeline.

---

## File uploads

Files must exist before they can be attached. There are two ways to create one, and they produce
the same `KBFiles` row.

| | **Direct multipart** (§6) | **Presigned S3** (§7–8) |
|---|---|---|
| Calls | 1 | 2, plus your own `PUT` to S3 |
| File passes through this API | Yes | No — the browser uploads straight to S3 |
| Best for | Small files, server-side scripts | Browser uploads, large files |
| Endpoint | `POST /upload_file/` | `POST /request-upload-url/` → `POST /confirm-upload/` |

**Both paths enforce the same limits:**

| Rule | Value |
|---|---|
| Allowed extensions | `.pdf`, `.docx`, `.txt` — **nothing else** |
| Maximum size | **10 MB** (`DATA_UPLOAD_MAX_MEMORY_SIZE`, 11 MiB in bytes) |

> **The size limit is enforced twice on the presigned path** — once against the size you declare in
> `request-upload-url`, and again against the object's *actual* size at `confirm-upload`. Declaring
> a small size and uploading a large file fails the second check, and **the object is deleted from
> S3** rather than left as an orphan.

An uploaded file that is never attached stays as an orphan row. Clean those up with
[`discard-files`](#9-discard-uploaded-files).

---

## Endpoint Details

### 1. List Knowledge Bases

Names and ingestion state only — deliberately light, with no components.

- **Path**: `GET /api/v1/core/knowledge-base/`
- **Headers**: `Authorization: Bearer <token>`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `page` | integer | Page number. Default `1`. | Beyond the last page → **404**. |
| `page_size` | integer | Rows per page. Default `10`. | Capped at `100`. |
| `search` | string | Partial, case-insensitive match on `name`. | Words split on spaces and **AND**ed. |
| `ordering` | string | Sort. Default `-created_at`. | `created_at`, `updated_at`, `name`, `is_processed`, `processing_status`, each optionally `-` prefixed. Anything else is ignored. |
| `is_processed` | boolean | Only fully-ingested bases. | `true` / `false`. |
| `processing_status` | enum | Filter by ingestion state. | `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`. Unknown → **400**. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "e4b2d561-71fb-48eb-bf2e-0a5682855cf8",
        "name": "Acme Setup Guides",
        "is_processed": false,
        "processing_status": "PROCESSING",
        "processing_error": null,
        "created_at": "2026-06-03T18:00:00Z"
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

> **No components, and no `tenant`, in the list payload.** A knowledge base can hold hundreds of
> texts and Q&A pairs; returning them for ten bases per page would be gratuitous. Use retrieve (§2)
> for contents.

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more rows. | `success` |
| **400** | Unknown `processing_status`, or a non-boolean `is_processed`. | filter error |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping, or an agent token. | `Your user account is not associated with any active tenant.` |
| **404** | `page` beyond the last page. | `Invalid page.` |

---

### 2. Retrieve a Knowledge Base

The full record, with every component expanded and each carrying its own ingestion state.

- **Path**: `GET /api/v1/core/knowledge-base/{id}/`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Retrieved successfully",
    "data": {
      "id": "e4b2d561-71fb-48eb-bf2e-0a5682855cf8",
      "tenant": { "id": "7bf26cf4-…", "name": "Alpha Corp", "subdomain": "alpha-corp" },
      "name": "Acme Setup Guides",
      "texts": [
        {
          "id": "a1b2…",
          "title": "Config Instructions",
          "description": "Configure API credentials.",
          "is_processed": true,
          "processing_status": "COMPLETED",
          "processing_error": null,
          "etl_id": "9c7e…",
          "completed_at": "2026-06-03T18:04:11Z"
        }
      ],
      "files": [
        {
          "id": "c3d4…",
          "file": "https://bucket.s3.amazonaws.com/t-7bf2…/kb_files/…?X-Amz-Expires=86400…",
          "original_name": "setup.pdf",
          "extension": ".pdf",
          "is_processed": false,
          "processing_status": "FAILED",
          "processing_error": "Could not extract text from page 4",
          "failed_at": "2026-06-03T18:05:02Z"
        }
      ],
      "websites": [
        { "id": "e5f6…", "url": "https://acme-setup.org/faq", "processing_status": "PENDING" }
      ],
      "question_answers": [
        {
          "id": "07a8…",
          "answer": "Yes, nested creation is supported.",
          "questions": [ { "id": "19b0…", "question": "Is nested creation supported?" } ],
          "processing_status": "COMPLETED"
        }
      ],
      "is_processed": false,
      "processing_status": "PROCESSING",
      "processing_error": null,
      "etl_id": "9c7e…",
      "pending_at": "2026-06-03T18:00:00Z",
      "processing_at": "2026-06-03T18:00:07Z",
      "completed_at": null,
      "failed_at": null,
      "created_by": { "id": "5fa8…", "email": "alex@acme.com", "first_name": "Alex", "last_name": "P" },
      "updated_by": null,
      "deleted_by": null,
      "created_at": "2026-06-03T18:00:00Z",
      "updated_at": "2026-06-03T18:00:07Z",
      "deleted_at": null
    },
    "errors": null
  }
  ```

> **`file` is a presigned download URL, not a stored path** — generated per request and valid for
> **24 hours**. Do not cache or persist it. If S3 storage is not configured, the raw storage path is
> returned instead.

> **The parent's `processing_status` is not a rollup.** A base can read `COMPLETED` while one of its
> files reads `FAILED`; they are tracked independently. To show "is this knowledge base healthy",
> check the components too.

| Status | When | `message` |
|---|---|---|
| **200** | Found. | `Retrieved successfully` |
| **401** / **403** | As elsewhere. | — |
| **404** | No such base, soft-deleted, or another tenant's. | `Not found.` |

---

### 3. Create a Knowledge Base

One call creates the container and all of its components.

- **Path**: `POST /api/v1/core/knowledge-base/`
- **Headers**: `Authorization: Bearer <token>`, `Content-Type: application/json`
- **Request Payload**:
  ```json
  {
    "name": "Acme Setup Guides",
    "texts": [
      { "title": "Config Instructions", "description": "Configure API credentials." }
    ],
    "files": [
      { "id": "c3d4e5f6-…" }
    ],
    "websites": [
      { "url": "https://acme-setup.org/faq" }
    ],
    "question_answers": [
      {
        "answer": "Yes, nested creation is supported.",
        "questions": [ { "question": "Is nested creation supported?" } ]
      }
    ]
  }
  ```

### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `name` * | string | What operators call this knowledge base. | Required. Max **255** characters. **Unique per tenant** among non-deleted bases — a duplicate is a 400. |
| `texts` | array | Free-text blocks written directly into the base. See [component fields](#component-field-reference). | Optional, default `[]`. |
| `files` | array | Previously uploaded documents, referenced by id. | Optional, default `[]`. Each id must be an existing file **in your tenant**; an id from another tenant is silently ignored rather than erroring. |
| `websites` | array | URLs for the ETL service to crawl. | Optional, default `[]`. Each `url` must be unique per tenant among live rows. |
| `question_answers` | array | Q&A pairs, each with one answer and any number of trigger questions. | Optional, default `[]`. |

**Read-only — ignored if you send them:** `is_processed`, `processing_status`, `processing_error`,
`etl_id`, `pending_at`, `processing_at`, `completed_at`, `failed_at`, `id`, `tenant`, `created_at`,
`updated_at`, `deleted_at`, `created_by`, `updated_by`, `deleted_by`.

> **A bare UUID string works wherever a component object is expected.** `"files": ["c3d4…"]` is
> normalised to `[{"id": "c3d4…"}]`, so you can pass a plain id list for attach-only cases.

> **An unknown or foreign component id is silently skipped, not rejected.** If a `files` id does not
> resolve inside your tenant, that entry is dropped and the knowledge base is created without it —
> with a `201`. Verify the returned arrays match what you sent rather than assuming everything
> attached.

- **Success Response (201 Created)**: the full object from §2, with `"message": "Created successfully"`.

### Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **201** | Stored, and ingestion attempted. **Check `processing_status`** — `FAILED` here means the ETL service could not be reached. | `Created successfully` | `null` |
| **400** | A live knowledge base in your tenant already has this name. | `A Knowledge Base with this name already exists.` | `{"name": "…"}` |
| **400** | `name` missing or over 255 characters. | `This field is required.` | `{"name": [...]}` |
| **400** | A `websites[].url` is already registered in your tenant. | `This website URL has already been registered.` | `{"websites": {"0": {"url": [...]}}}` |
| **400** | A `websites[].url` is not a valid URL, or over 500 characters. | `Enter a valid URL.` | `{"websites": {...}}` |
| **400** | A `texts[].title` is missing or over 255 characters. | `This field is required.` | `{"texts": {...}}` |
| **400** | A `question_answers[]` entry has `questions: []`. | `At least one question must be linked.` | `{"question_answers": {...}}` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": …}` |
| **403** | No active tenant mapping, or an agent token. | as above | `{"detail": …}` |

> **`questions: []` is an error, but omitting `questions` is fine.** An empty list explicitly asks
> for a Q&A pair with no triggers, which is rejected; leaving the key out creates the answer with no
> questions attached and no complaint.

---

### 4. Update a Knowledge Base

`PUT` and `PATCH` behave identically for components — both use the flag system. The only difference
is that `PUT` expects `name`.

- **Path**: `PUT` / `PATCH /api/v1/core/knowledge-base/{id}/`

### The flag system

Component arrays are **not** whole-list replacements. Each entry carries a `flag` saying what to do
with it:

| `flag` | Effect | Requires |
|---|---|---|
| `create` | Build a new component and attach it. | The component's own required fields |
| `update` | Edit the existing component in place. | `id` |
| `delete` | Detach **and soft-delete** the component. | `id` |
| *(omitted, with `id`)* | Attach an existing component, unchanged. | `id` |
| *(omitted, no `id`)* | Same as `create`. | The component's required fields |

```json
{
  "name": "Acme Updated Guides",
  "texts": [
    { "id": "a1b2…", "flag": "update", "description": "Updated credential steps." },
    { "flag": "create", "title": "CLI Guide", "description": "Command line instructions." },
    { "id": "c3d4…", "flag": "delete" }
  ],
  "files": [
    { "id": "e5f6…", "flag": "create" }
  ]
}
```

> **Components you do not mention are left alone.** Omitting `texts` entirely changes no texts;
> sending `"texts": []` also changes nothing, because there are no entries to act on. There is no
> way to clear a list in one move — delete entries individually.

> **`flag: "delete"` soft-deletes the component, it does not merely detach it.** The row is removed
> from this knowledge base *and* marked deleted, so it cannot be re-attached afterwards. If a
> component is shared between knowledge bases, deleting it from one removes it from all.

> **Q&A questions nest one level deeper.** Inside a `question_answers` entry with `flag: "update"`,
> the `questions` array takes its own per-question flags, following exactly the same rules.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Updated, and ingestion re-triggered from scratch. | `Updated successfully` | `null` |
| **400** | Any validation error from §3, including the new name colliding. | as §3 | `{"<field>": …}` |
| **401** / **403** | As elsewhere. | — | — |
| **404** | No such base, soft-deleted, or another tenant's. | `Not found.` | `{"detail": …}` |

> **Every update restarts ingestion for the whole knowledge base**, not just the changed parts. A
> new `etl_id` is minted, everything returns to `PENDING`, and previously `COMPLETED` components are
> re-processed. Batch your edits — ten separate `PATCH` calls mean ten full re-ingestions.

> **An `id` that does not resolve in your tenant is skipped silently.** A `flag: "update"` naming an
> unknown id does nothing and still returns `200`. Compare the response against your intent.

---

### 5. Delete a Knowledge Base

Soft-delete, with a confirmation gate when the base is in use.

- **Path**: `DELETE /api/v1/core/knowledge-base/{id}/`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `force` | string | Pass `true` to proceed when agents are using this knowledge base. | Query parameter. Compared case-insensitively against `"true"`; any other value counts as not forcing. |

| Status | When | `message` |
|---|---|---|
| **200** | Soft-deleted. | `Deleted successfully` |
| **400** | Live agents use it and `force=true` was not passed. **Nothing is deleted** — a confirmation prompt, not a hard block. | `This knowledge base is connected to the following agents: SalesBot, SupportBot... Are you sure you want to delete it?` |
| **401** / **403** | As elsewhere. | — |
| **404** | No such base, soft-deleted, or another tenant's. | `Not found.` |

> **This 400 carries an extra `warning: true` key** that no other endpoint returns, and it has **no
> `data` key**. It is meant to be rendered as a confirmation dialog: show `message`, and on confirm
> re-issue the request with `?force=true`.

> **Unlike agents and event templates, being in use does not prevent deletion** — it only asks
> first. Agents keep working; they simply lose this source of knowledge.

> **Agent names are truncated at three with `...` appended.** Treat the message as a pointer, not a
> complete list.

**What soft delete does:**

- The base's `name` is rewritten with a `-deleted-<YYYYMMDDHHMMSS>` suffix. This is **load-bearing,
  not cosmetic**: the unique constraint on `(name, tenant)` has no "not deleted" condition, so
  without the rename the name would be burned permanently. Website URLs work the same way.
- Components are cascade-soft-deleted **only if no other live knowledge base uses them**. A text
  shared with a second base survives.
- Ingestion is triggered once more, so the ETL service learns the base is gone.

---

### 6. Upload a Document File (direct)

Sends the file through this API. Simplest path; best for server-side callers and small files.

- **Path**: `POST /api/v1/core/knowledge-base/upload_file/`
- **Headers**: `Authorization: Bearer <token>`, `Content-Type: multipart/form-data`

| Field | Type | Description | Validation |
|---|---|---|---|
| `file` * | file | The document to ingest. | Required. Extension must be `.pdf`, `.docx`, or `.txt`. Max **10 MB**. |

- **Success Response (201 Created)**:
  ```json
  {
    "status": true,
    "message": "File uploaded successfully",
    "data": {
      "id": "c3d4e5f6-…",
      "file": "https://bucket.s3.amazonaws.com/…?X-Amz-Expires=86400…",
      "original_name": "setup.pdf",
      "extension": ".pdf",
      "processing_status": "PENDING"
    },
    "errors": null
  }
  ```

**Capture `data.id`** — that is what goes in the `files` array when you create or update a knowledge
base. Uploading alone attaches nothing.

| Status | When | `message` |
|---|---|---|
| **201** | Stored. Not yet attached to any knowledge base. | `File uploaded successfully` |
| **400** | No file, or an unsupported extension. | `Unsupported format. Only .pdf, .docx, .txt are allowed.` |
| **400** | Over 10 MB. | `File size exceeds the limit of 10 MB.` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping. | `User must have an active tenant to upload files to a knowledge base.` |

---

### 7. Request an Upload URL (presigned, step 1)

Reserves a file row and returns a temporary S3 URL your client `PUT`s the bytes to directly.

- **Path**: `POST /api/v1/core/knowledge-base/request-upload-url/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `filename` * | string | The file's name, used to derive the extension and the stored object key. | Required, max **255**. Extension must be `.pdf`, `.docx`, or `.txt`. |
| `content_type` * | string | MIME type, baked into the presigned URL — your `PUT` must send the **same** `Content-Type` header or S3 rejects it. | Required, max **100**. Not cross-checked against `filename`. |
| `file_size_in_bytes` * | integer | Declared size, baked into the URL as a limit. | Required. Must not exceed **10 MB**. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Upload URL generated successfully.",
    "data": {
      "file_id": "c3d4e5f6-…",
      "upload_url": "https://bucket.s3.amazonaws.com/t-7bf2…/kb_files/…?X-Amz-Signature=…",
      "object_key": "t-7bf26cf4-…/kb_files/c3d4e5f6-…_setup.pdf"
    },
    "errors": null
  }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | URL issued. The row exists with `upload_status: PENDING`. | `Upload URL generated successfully.` |
| **400** | Unsupported extension, or declared size over 10 MB. | first validation error |
| **401** / **403** | As elsewhere. | — |
| **500** | The storage service could not issue a URL. The row is marked `FAILED`. | `Failed to generate upload URL.` |

> **Keep `file_id`; ignore `object_key`.** `object_key` is **deprecated** and returned only so older
> clients keep working. Step 2 resolves the file by `file_id` alone.

> **The filename is sanitised to a basename** before the object key is built, so a crafted value
> like `../../other-tenant/x.pdf` cannot steer the upload outside your tenant's prefix.

---

### 8. Confirm the Upload (presigned, step 2)

Call after your `PUT` to S3 succeeds. Verifies the object really arrived and marks the row usable.

- **Path**: `POST /api/v1/core/knowledge-base/confirm-upload/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `file_id` * | UUID | The id from step 1. **Preferred and sufficient on its own.** | Required unless `object_key` is supplied. Must belong to your tenant. |
| `object_key` | string | **Deprecated.** The legacy way to identify the upload. | Max **512**. Used only when `file_id` is absent. Must sit inside your tenant's prefix. |
| `original_name` | string | Display name for the file. | Optional with `file_id` (the name from step 1 is kept). **Required** on the legacy `object_key` path. Extension must be allowed. |
| `extension` | string | Override the derived extension. | Optional. Must be `.pdf`, `.docx`, or `.txt`; a leading dot is added if you omit it. |

- **Success Response (201 Created)**: the file object as in §6, with `upload_status: CONFIRMED`.

| Status | When | `message` |
|---|---|---|
| **201** | Verified and recorded. Now usable in a `files` array. | `File confirmed and saved successfully.` |
| **400** | Neither `file_id` nor `object_key` supplied. | `Provide file_id (preferred) or object_key.` |
| **400** | The object is not in the bucket — the `PUT` never landed. | `File not found in storage bucket. Did the upload succeed?` |
| **400** | The **actual** object is over 10 MB. **The object is deleted from S3** and the row marked `FAILED`. | `File size exceeds the limit of 10 MB.` |
| **400** | `original_name` missing on the legacy path, or a disallowed extension. | first validation error |
| **403** | No active tenant mapping. | `Your user account is not associated with any active tenant.` |
| **404** | `file_id` unknown or another tenant's; or an `object_key` outside your tenant's prefix. | `File not found.` |
| **500** | The storage service could not be queried. | `Failed to verify file upload.` |

> **A foreign `object_key` returns 404, not 403.** Deliberate — a 403 would confirm the object exists
> in some other tenant.

> **The tenant is resolved before anything touches S3.** The oversize branch deletes the object it
> was handed, so an unauthorised key must never reach it.

---

### 9. Discard Uploaded Files

Permanently removes uploads that were never attached — the "user changed their mind" path.

- **Path**: `POST /api/v1/core/knowledge-base/discard-files/`
- **Request Payload**: `{ "file_ids": ["c3d4e5f6-…", "…"] }`

| Field | Type | Description | Validation |
|---|---|---|---|
| `file_ids` * | UUID array | Uploads to destroy. | Non-empty list. Only ids in your tenant are touched; unknown ones are skipped silently. |

| Status | When | `message` |
|---|---|---|
| **200** | Deleted. The count reflects what actually matched, which may be fewer than you sent. | `N file(s) discarded successfully.` |
| **400** | `file_ids` missing, not a list, or empty. | `A valid list of file_ids is required.` |
| **403** | No tenant mapping, **or any unexpected failure during deletion**. | `Permission denied or error occurred.` |

> **This is a hard delete, not a soft one.** The database row and the S3 object are both destroyed
> and cannot be recovered — unlike everything else on this page.

> **It does not check whether the file is attached to a knowledge base.** Discarding an attached file
> removes it from that base's contents. Use the [flag system](#the-flag-system) with
> `flag: "delete"` for files already in use.

> **The `403` is over-broad.** Any exception during deletion is reported as a permission problem, so
> a storage outage is indistinguishable from an authorisation failure. Treat a `403` here as "did
> not happen", not necessarily "not allowed".

---

### 10. Dropdown

- **Path**: `GET /api/v1/core/knowledge-base/dropdown/`
- **Query Parameters**: `limit` (≤ **1000**; over that → **400 `Limit cannot exceed 1000`**), plus the List filters and `search`.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [ { "label": "Acme Setup Guides", "value": "e4b2d561-…" } ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

`label` is the name, `value` the id. Filter with `?processing_status=COMPLETED` to offer only
knowledge bases that are actually ready to use.

---

### 11. Bulk Delete

- **Path**: `POST /api/v1/core/knowledge-base/bulk-delete/`

> **Note the hyphen.** Every other resource in this API uses `/bulk_delete/` with an underscore;
> this one is `/bulk-delete/`. The underscore form is a **404** here.

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Knowledge bases to soft-delete. | Non-empty list. Every id must exist, be undeleted, and be in your tenant. All-or-nothing. |
| `force` | string | Query parameter. Pass `true` to proceed when any base is connected to an agent. | Compared case-insensitively against `"true"`. |

| Status | When | `message` |
|---|---|---|
| **200** | All soft-deleted. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | One or more are connected to an agent and `force=true` was not passed. **Nothing deleted.** Carries `warning: true`. | `One or more selected knowledge bases are connected to an agent. Are you sure you want to delete them?` |
| **400** | Any id unknown, already deleted, or another tenant's. **Nothing deleted.** | `Some ids are invalid` |
| **401** / **403** | As elsewhere. | — |

> **The warning does not name which base or which agent**, unlike the single delete. Delete
> individually when you need to tell the user specifically what they are disconnecting.

> **Duplicate ids fail the whole request** with `Some ids are invalid`. De-duplicate client-side.

---

### 12. Check Name / URL Uniqueness

Two advisory lookups for wizard validation, so a user learns about a clash before submitting.

- **Paths**: `POST /api/v1/core/knowledge-base/check-name/` · `POST /api/v1/core/knowledge-base/check-url/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `name` * | string | *(check-name)* The candidate knowledge base name. | Required, max **255**. |
| `url` * | string | *(check-url)* The candidate website URL. | Required, must be a valid URL, max **500**. |
| `exclude_id` | UUID | The record being edited, so it does not clash with itself. | Optional, nullable. |

- **Success Response (200 OK)**:
  ```json
  { "status": true, "message": "success", "data": { "is_unique": true }, "errors": null }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | Check ran. `data.is_unique` is the answer. | `success` |
| **400** | `name` / `url` missing or malformed. | field error |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping. | `Your user account is not associated with any active tenant.` |

> **Advisory, not a reservation.** Another user can take the name between your check and your submit,
> so still handle the `400` on create. A `200` here is never a guarantee.

> **These checks include soft-deleted rows.** They query without excluding them — which works out
> correctly, because deletion renames the row to `<name> -deleted-<timestamp>`, so a freed name does
> read as unique.

---

### 13. Update ETL Status (ETL service only)

The callback the external ETL service uses to report progress. **Not for application clients.**

- **Path**: `POST /api/v1/core/knowledge-base/{id}/update-etl-status/`
- **Auth**: an **ETL system token** — a different authentication class from every other endpoint
  here. A normal user JWT is rejected.

| Field | Type | Description | Validation |
|---|---|---|---|
| `type` * | enum | Which kind of object this update is about. | `knowledgebase`, `file`, `website`, `text`, `qa`. |
| `object_id` * | UUID | The specific object. | Must belong to the knowledge base in the URL. For `knowledgebase`, must equal the URL id. |
| `etl_id` * | UUID | The ingestion run this update belongs to. | Must match the object's **current** `etl_id`, or the update is rejected. |
| `status` * | enum | New ingestion state. | `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`. |
| `timestamp` | datetime | When the transition happened. | Optional, nullable. Defaults to server time. |
| `error_message` | string | Failure detail, stored on `processing_error`. | Optional, nullable, may be blank. Only meaningful with `FAILED`. |

| Status | When | `message` |
|---|---|---|
| **200** | Applied. | `ETL status updated successfully.` |
| **400** | `etl_id` does not match the object's current run — a stale callback. **Ignored deliberately.** | `Failed to update ETL status. The etl_id does not match the current tracking etl_id.` |
| **400** | `object_id` is not part of this knowledge base, or a field is missing/invalid. | `File with this ID was not found in this Knowledge Base.` |
| **401** / **403** | Not an authenticated ETL system caller. | — |
| **404** | No such knowledge base. | `Not found.` |

> **The stale-`etl_id` rejection is the concurrency guard.** If a knowledge base is edited while
> ingestion is running, the edit mints a new `etl_id` and every remaining callback from the old run
> is refused — which is why a slow run cannot overwrite the state of a newer one.

> **ETL callers are not tenant-scoped.** The ETL service reaches every tenant's knowledge bases by
> design, since it processes all of them. Its token is a high-value credential.

---

### 14. The outbound ingestion payload

For whoever builds the ETL service. This is what **this backend sends out**, not an endpoint you
call.

- **Method**: `POST` to `ETL_SERVER_URL`
- **Headers**: `Authorization: Bearer <ETL_SYSTEM_TOKEN>`, `Content-Type: application/json`
- **Timeout**: 10 seconds

```json
{
  "task_name": "knowledge_base_ingestion_20ab4a80-…_36c7c81e-…",
  "payload": {
    "id": "20ab4a80-bbf8-4c9e-9c42-79138832db8a",
    "name": "Acme Ingestion",
    "tenant": { "id": "7bf26cf4-…", "name": "Alpha Corp", "subdomain": "alpha-corp" },
    "etl_id": "36c7c81e-f85f-4de7-85eb-aba467037fa7",
    "is_processed": false,
    "is_deleted": false,
    "callback_update_url": "https://api.example.com/api/v1/core/knowledge-base/20ab4a80-…/update-etl-status/",
    "texts": [
      {
        "id": "78c8c81e-…",
        "title": "Setup",
        "description": "Guides to configure REST endpoints",
        "is_processed": false,
        "processing_status": "PENDING",
        "processing_error": null,
        "etl_id": "36c7c81e-…",
        "is_deleted": false
      }
    ],
    "files": [
      {
        "id": "18c8c81e-…",
        "file": "https://bucket.s3.amazonaws.com/…?X-Amz-Expires=86400…",
        "original_name": "setup.pdf",
        "extension": ".pdf",
        "file_size_in_bytes": 102400,
        "is_processed": false,
        "processing_status": "PENDING",
        "processing_error": null,
        "etl_id": "36c7c81e-…",
        "is_deleted": false
      }
    ],
    "websites": [
      {
        "id": "28c8c81e-…",
        "url": "https://acme-setup-guides.org",
        "is_processed": false,
        "processing_status": "PENDING",
        "processing_error": null,
        "etl_id": "36c7c81e-…",
        "is_deleted": false
      }
    ],
    "question_answers": [
      {
        "id": "38c8c81e-…",
        "title": "",
        "answer": "Yes, we support deep nesting.",
        "questions": [
          { "id": "48c8c81e-…", "question": "Does this support nested creation?", "is_deleted": false }
        ],
        "is_processed": false,
        "processing_status": "PENDING",
        "processing_error": null,
        "etl_id": "36c7c81e-…",
        "is_deleted": false
      }
    ]
  }
}
```

> **The envelope is `{task_name, payload}`.** The knowledge base itself — including `etl_id` and
> `callback_update_url` — is nested **inside** `payload`, not alongside it. (Earlier versions of
> this document showed a `{etl_id, callback_update_url, data}` envelope; that never matched the
> code.)

**Expected ETL behaviour**, by convention rather than enforcement:

- Process only items whose `etl_id` matches the run's `etl_id` and whose `processing_status` is
  `PENDING`.
- Skip items already `is_processed: true`, or carrying a different `etl_id`.
- Remove items where `is_deleted: true`.
- Report each item's progress to `callback_update_url` (§13), echoing the `etl_id` you were given.

> **`file` URLs in this payload expire after 24 hours.** Download promptly, or request a fresh
> payload — an ingestion run that stalls for a day will find its links dead.

> **`callback_update_url` is built from `CALLBACK_URL_BASE` when set**, otherwise from the request
> host (forced to HTTPS outside localhost), otherwise from `BACKEND_PROJECT_URL`. Set
> `CALLBACK_URL_BASE` explicitly in any deployment behind a proxy.

---

## Component field reference

Every component carries the full ingestion block (`is_processed`, `processing_status`,
`processing_error`, `etl_id`, and the four timestamps) as **read-only**. Only writable fields are
listed here.

### `texts[]`

| Field | Type | Description | Validation |
|---|---|---|---|
| `title` * | string | Heading for this block. | Required on create. Max **255**. |
| `description` | string | The body text. No length limit. | Optional, nullable. |

### `files[]`

| Field | Type | Description | Validation |
|---|---|---|---|
| `id` * | UUID | A file uploaded via §6 or §7–8. | Required when attaching. Must be in your tenant. |
| `file` | file | Only for an inline `flag: "create"` in a multipart request. | Required if creating without an `id`; otherwise omit. |

### `websites[]`

| Field | Type | Description | Validation |
|---|---|---|---|
| `url` * | string | Page for the ETL service to crawl. | Required. Valid URL, max **500**. **Unique per tenant** among live rows. |

### `question_answers[]`

| Field | Type | Description | Validation |
|---|---|---|---|
| `answer` * | string | The response the bot should give. | Required. No length limit. |
| `questions` | array | Phrasings that should trigger this answer, each `{"question": "..."}`. | Optional. If present, must not be empty. |

> **`title` exists on the Q&A model but cannot be set through this API** — it is absent from the
> serializer, so every Q&A pair created here stores an empty title. It appears in the ETL payload
> always blank.

---

## Configuration

| Setting | Default | Effect |
|---|---|---|
| `ETL_SERVER_URL` | *(required)* | Where ingestion is POSTed. No default — the app will not start without it. |
| `ETL_SYSTEM_TOKEN` | `dummy_etl_system_token_for_local_dev` | Bearer token sent to the ETL service, and the credential it authenticates back with. |
| `DATA_UPLOAD_MAX_MEMORY_SIZE` | `11534336` (11 MiB) | The upload ceiling. User-facing messages call it "10 MB". |
| `USE_S3_STORAGE` | derived from the S3 settings | When enabled, `file` fields render as 24-hour presigned URLs. |
| `CALLBACK_URL_BASE` | unset | Overrides the callback URL handed to the ETL service. |
| `BACKEND_PROJECT_URL` | `http://localhost:8000` | Last-resort base for the callback URL. |

The ETL call uses a fixed **10-second** timeout that is not configurable.

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | Ingestion is a blocking outbound HTTP call inside the request. | Create/update/delete latency includes up to 10s of ETL timeout, and a slow ETL service degrades the API. |
| 2 | Unresolvable component ids are skipped silently. | A typo'd or foreign id produces a `201`/`200` with fewer components than requested, and no warning. |
| 3 | `discard-files` reports every failure as `403`. | A storage outage is indistinguishable from an authorisation failure. |
| 4 | `bulk-delete` uses a hyphen where every other resource uses an underscore. | Clients generating the URL from a shared helper will 404. |
| 5 | Q&A `title` is unsettable through the API. | The field always reaches the ETL service blank. |

## Related

- [Agent API](./agent.md) — attaches knowledge bases to bots
- [Agent Template API](./agent_template.md)
- [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md)

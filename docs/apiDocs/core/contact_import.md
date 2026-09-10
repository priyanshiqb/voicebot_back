# Contact Import API

## Overview
Staged bulk import of contacts from CSV: **upload → parse → review → resolve → commit**.
Nothing is written to the `Contact` table until you explicitly commit, so duplicates and
malformed rows are caught while they are still cheap to fix.

**Base Path:** `/api/v1/core/contact-imports/`
**Row edits:** `/api/v1/core/contact-import-rows/`

Parsing and committing run **asynchronously**. Both endpoints return `202` — poll the
session until its `status` settles.

> **Sessions are tenant-wide, not private.** Any colleague in the tenant can review,
> resolve, commit, or discard an import. Every transition records who did it
> (`created_by`, `committed_by`, `discarded_by`, and `updated_by` per row).

---

## CSV format

Download the template: `GET /api/v1/core/contact-imports/template/`

**CSV by default; `?file_format=xlsx` for a workbook.** The filename always matches the bytes —
`contacts_template.csv` or `contacts_template.xlsx`. The CSV carries a UTF-8 BOM and CRLF line
endings so Excel opens it without mangling non-ASCII names.

> The default was briefly `xlsx` while clients still saved the download as `contacts_template.csv`.
> That put a zip container behind a `.csv` name, and Excel — which sniffs the content — refused it
> with *"The file format and extension of 'contacts_template.csv' don't match."*

| Column | Required | Notes |
|---|---|---|
| `first_name` | | |
| `last_name` | | |
| `phone_number` | **yes** | International (`+919876543210`) or national (`9876543210`, parsed with the session region) |
| `email_address` | | |
| `company` | | |
| `tags` | | **Pipe**-delimited: `vip\|lead` (a comma would collide with the delimiter) |
| `notes` | | |

**Headers are forgiving.** `First Name`, `firstname`, `Mobile`, `E-Mail`, `Organisation`
and similar aliases are recognised, case-insensitively. Pass `column_map` to override.

**Encoding and delimiter are auto-detected** — UTF-8 with or without a BOM (what Excel
writes), cp1252 fallback, and `,` / `;` / tab delimiters. Quoted fields containing commas
and newlines are handled correctly.

**Consent and do-not-call columns are ignored.** If the file contains `do_not_call`,
`consent_status`, or similar, they are dropped and a notice is added to the session.
Re-uploading a stale list must not resurrect someone who opted out.

---

## Session status

```
UPLOADED ──▶ PARSING ──▶ READY ──▶ COMMITTING ──▶ COMPLETED
                 │          │            │
                 ▼          ▼            ▼
              FAILED    DISCARDED   PARTIAL / FAILED
                 │          │
                 └──▶ EXPIRED ◀──────── (TTL sweep)
```

| Status | Meaning |
|---|---|
| `UPLOADED` | Created; parsing queued |
| `PARSING` | Being read |
| `READY` | Reviewable. Rows may be edited **only** in this state |
| `COMMITTING` | Claimed for commit |
| `COMPLETED` | All rows written; staging rows and the CSV are purged |
| `PARTIAL` | Some rows failed; **everything is retained** so you can see which |
| `FAILED` | The file itself could not be read |
| `DISCARDED` | Abandoned by a user |
| `EXPIRED` | TTL passed before commit (default 24h) |

The session row itself always survives as an audit record.

---

## Row status

| Status | Meaning |
|---|---|
| `PENDING` | Clean; will create a new contact |
| `INVALID` | Malformed — missing/unparseable phone, bad email, or a name that is already taken |
| `CONFLICT_EXISTING` | Phone matches a live contact |
| `CONFLICT_DUPLICATE` | Phone appears more than once in this file |
| `CONFLICT_ARCHIVED` | Phone matches a soft-deleted contact |
| `RESOLVED` | Conflict settled by an explicit resolution |
| `SKIPPED` | Will not be written |
| `COMMITTED` | Written successfully |
| `FAILED` | Failed during commit (reason in `error_message`) |

> `status` is **never writable by the client.** It is re-derived from validation after
> every edit. Allowing a client to set it would let a row be marked resolved without the
> duplicate actually being fixed, which then fails at commit.

> **A taken name is `INVALID`, not a `CONFLICT_*`.** Contact names are unique per tenant
> (see [Contacts → Name uniqueness](./contact.md#name-uniqueness)), and a row is flagged when
> its name is already held by a live contact (`A contact with this name already exists.`) or
> appears twice in the file (`This name appears more than once in the file.`).
>
> The conflict statuses exist so you can choose between creating and updating, and neither
> choice resolves a name collision — the only way out is to edit the name, which is what
> `INVALID` asks for. `PATCH` the row's `first_name` / `last_name` and it revalidates to
> `PENDING`. Rows left `INVALID` are simply not written.

---

## 1. Request an upload URL
**`POST /api/v1/core/contact-imports/request-upload-url/`**

Reserves a row and hands back a short-lived presigned `PUT` URL. The file bytes go **directly to
storage**, never through this API — so a 50 MB CSV never occupies a request worker.

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `filename` * | string | The user's original filename. Used for the stored name and to derive the extension. | Sanitized server-side — a traversal attempt like `../../evil.csv` lands inside the tenant's own prefix like any other upload. |
| `content_type` * | string | MIME type, e.g. `text/csv`. Baked into the presigned URL, so the actual `PUT` must send a matching `Content-Type` header. | Must be a supported spreadsheet type. |
| `file_size_in_bytes` * | integer | Size of the file you are about to upload. A **declaration** — see the note below on which mode actually enforces it. | Must be > 0 and ≤ `CONTACT_IMPORT_MAX_FILE_BYTES` (default **10 MB**). Rejected here *before* any upload happens. |

```json
{
  "status": true,
  "message": "Upload URL generated successfully.",
  "data": {
    "file_id": "9c1e85f6-…",
    "upload_method": "PUT",
    "upload_url": "https://<acct>.blob.core.windows.net/<container>/t-7bf2…/contact_imports/…?sv=…&sp=c&sig=…",
    "upload_fields": {},
    "upload_headers": {
      "x-ms-blob-type": "BlockBlob",
      "Content-Type": "text/csv"
    },
    "expires_in_minutes": 15,
    "max_bytes": 10485760,
    "object_key": "t-7bf26cf4-…/contact_imports/9c1e85f6-…_contacts.csv"
  }
}
```

| Field | Meaning |
|---|---|
| `file_id` | **The handle for every later step.** Confirm and import with this. |
| `upload_method` | Always `PUT`. Declared so you branch on it rather than hardcoding the verb. |
| `upload_url` | Where to send the file. Short-lived — request a new one rather than caching it. |
| `upload_headers` | **Send every entry verbatim.** Azure refuses the upload without `x-ms-blob-type`. |
| `upload_fields` | Always `{}`. Held the S3 POST policy's form fields; Azure has no equivalent. |
| `expires_in_minutes` | Ticket lifetime (`UPLOAD_URL_TTL_MINUTES`, default 15). |
| `max_bytes` | The server's ceiling for this flow. **Not** enforced during the upload — see below. |
| `object_key` | Where the file will live in storage. **Informational only** (see below). |

> **`object_key` is output, never input.** It is derived server-side from the row's own id and a
> sanitized filename, so nothing about it is client-controlled. `confirm-upload` resolves the file
> by `file_id` **alone** and ignores any `object_key` a client sends back — so knowing the key
> grants nothing, and it cannot be used to name (and then import) another tenant's file.
>
> Use it for display or debugging. Do not build flows that send it back.

| Status | When | `message` |
|---|---|---|
| **200** | Row reserved; upload with the returned URL. `upload_status` is `PENDING`. | `Upload URL generated successfully.` |
| **400** | Missing field, unsupported `content_type`, or size over the limit. | the field's message |
| **403** | Caller's account has no tenant mapping. | `Your user account is not associated with any active tenant.` |
| **500** | Storage refused to mint a URL. The row is marked `FAILED` — request a new one. | `Failed to generate upload URL.` |

### Sending the file

Send the body raw to `upload_url` with the headers from `upload_headers`.

```js
await fetch(data.upload_url, {
  method: "PUT",
  headers: data.upload_headers,
  body: file,
});   // success is 201
```

> **Send every header in `upload_headers`.** Azure rejects a block-blob PUT without
> `x-ms-blob-type: BlockBlob`, and that rejection comes from the storage account rather
> than from this API — so omitting it looks like a storage outage, not a client bug.
> Success is **`201 Created`**.
>
> `upload_fields` is always `{}`. It held the signed form fields of the S3 POST policy the
> previous backend used; Azure Blob has no equivalent. It stays in the envelope so clients
> that read it unconditionally keep working.

> **The size limit is not enforced during the upload.** An Azure SAS carries no size
> condition at any API version, so a ticket issued for a 1 MB file will accept a 500 MB
> one. `confirm-upload` measures the stored object and deletes it if it overruns — but the
> bytes have already been transferred by then. **Check the size client-side against
> `max_bytes` before uploading.**

---

## 2. Confirm the upload
**`POST /api/v1/core/contact-imports/confirm-upload/`**

Tells the server the bytes have landed. Until this succeeds the file is not importable.

| Field | Type | Description | Validation |
|---|---|---|---|
| `file_id` * | UUID | The handle from step 1. | Must resolve **within your tenant** — another tenant's id is a 404, not a 403, so it never confirms the id exists. |

Verifies the object is really in storage and re-checks its **actual** size against
`CONTACT_IMPORT_MAX_FILE_BYTES` — the declared size in step 1 was a client claim, so it is
re-measured here against what was really uploaded.

| Status | When | `message` |
|---|---|---|
| **200** | Confirmed; marked ready to import. Body is the file record. | — |
| **400** | Nothing at that key — the `PUT` failed or was never made. | `File not found in storage. Did the upload succeed?` |
| **400** | The uploaded file exceeds the size limit. | size-limit message |
| **403** | Caller's account has no tenant mapping. | `Your user account is not associated with any active tenant.` |
| **404** | Unknown `file_id`, or one belonging to another tenant. | `Import file not found.` |

---

## 3. Start the import
**`POST /api/v1/core/contact-imports/`** → `202`

```json
{
  "file_id": "…",
  "default_region": "US",
  "conflict_strategy": "SKIP",
  "column_map": { "Cell Phone": "phone_number" }
}
```

| Field | Required | Notes |
|---|---|---|
| `file_id` | yes | Must be `CONFIRMED` |
| `default_region` | no — falls back to `CONTACT_DEFAULT_PHONE_REGION` (default `IN`) | ISO-3166 alpha-2. Pass it to import a list from another country |
| `conflict_strategy` | no | `SKIP` (default), `UPDATE`, `FAIL` |
| `column_map` | no | CSV header → canonical field |

**A region is mandatory.** It resolves from `default_region`, else
`CONTACT_DEFAULT_PHONE_REGION` (default `IN`). If neither resolves — or the value is not
a recognised region code — the request is rejected with **400** and no session is created.

### Other rejections
| Status | Cause |
|---|---|
| 400 | Upload not confirmed |
| 400 | An import was already started for this file |
| 400 | Too many open sessions (`CONTACT_IMPORT_MAX_OPEN_SESSIONS`, default 3) |
| 404 | `file_id` belongs to another tenant |

---

## 4. Poll the session
**`GET /api/v1/core/contact-imports/{id}/`**

```json
{
  "status": true,
  "data": {
    "id": "…",
    "status": "READY",
    "default_region": "US",
    "conflict_strategy": "SKIP",
    "counts": {
      "total": 4812, "valid": 4775, "conflicts": 31, "invalid": 6,
      "created": 0, "updated": 0, "skipped": 0, "failed": 0
    },
    "notices": ["Consent and do-not-call columns are ignored by import and were skipped: do_not_call"],
    "created_by_email": "alex@example.com",
    "expires_at": "2026-07-30T09:12:00Z"
  }
}
```

Use `counts` to render a summary without paging through every row.
`GET /api/v1/core/contact-imports/` lists all sessions for the tenant.

---

## 5. Review the rows
**`GET /api/v1/core/contact-imports/{id}/rows/?status=CONFLICT_EXISTING`**

Paginated. `status` filters by row status; `search` matches the normalized phone.

---

## 6. Resolve conflicts

### Edit one row
**`PATCH /api/v1/core/contact-import-rows/{row_id}/`**

```json
{ "parsed_data": { "phone_number": "+14155559999" }, "resolution": "CREATE" }
```

Only `parsed_data` and `resolution` are accepted. After the edit the row is re-validated
and its status recomputed:

* fixed to a free number → `PENDING`
* fixed to another taken number → still `CONFLICT_EXISTING`
* edited to an unparseable number → `INVALID`
* `resolution: "SKIP"` → `SKIPPED`
* `resolution: "UPDATE"` on a live conflict → `RESOLVED`

Returns **409** if the session is not `READY`.

### Resolve in bulk
**`POST /api/v1/core/contact-imports/{id}/bulk-resolve/`**

```json
{ "status": "CONFLICT_EXISTING", "resolution": "SKIP" }
```

Applies one resolution to every row with that status.

### Resolutions

| Resolution | Effect |
|---|---|
| `CREATE` | Insert a new contact |
| `UPDATE` | Merge onto the existing contact |
| `SKIP` | Leave the existing contact untouched |

**`UPDATE` safety rails**
1. The session default is `SKIP` — nothing is ever overwritten unless explicitly chosen.
2. **Blank cells never overwrite populated fields.** A CSV with an empty `company`
   column will not wipe existing company values.
3. `consent_status`, `consent_source`, `consent_at`, `do_not_call`, and `opted_out_at`
   are never touched by import.

---

## 7. Commit
**`POST /api/v1/core/contact-imports/{id}/commit/`** → `202`

```json
{ "ignore_unresolved": false }
```

Refuses with **409** while conflicts remain, unless `ignore_unresolved: true` — those
rows are then recorded as `SKIPPED`.

Also **409** if the session is not `READY` (already committing, already committed,
discarded, or expired). This is what makes a double-clicked commit safe: the second
request loses the race and no contacts are duplicated.

Conflicts are **re-checked inside the commit transaction**, not trusted from parse time.
If someone created a matching contact in the meantime, the row is skipped rather than
duplicated or erroring.

Poll until `COMPLETED` (staging rows and the CSV are purged) or `PARTIAL` (everything
retained so failures can be inspected).

---

## 8. Discard
**`POST /api/v1/core/contact-imports/{id}/discard/`**

Deletes the staging rows and the uploaded file. The session remains as an audit record
with `discarded_by` and `discarded_at`. Returns **409** if already terminal.

---

## Expiry
Sessions expire after `CONTACT_IMPORT_SESSION_TTL_HOURS` (default 24). A Celery beat task
(`contacts-cleanup-expired-imports`, every 10 minutes) purges their rows and files and
marks them `EXPIRED`. Expired sessions cannot be committed.

---

## Configuration

| Setting | Default | Purpose |
|---|---|---|
| `CONTACT_DEFAULT_PHONE_REGION` | `IN` | Primary market (ISO-3166 alpha-2). Override per environment if a deployment serves another country |
| `CONTACT_IMPORT_MAX_ROWS` | 50,000 | Hard row ceiling |
| `CONTACT_IMPORT_MAX_FILE_BYTES` | 10 MB | Upload ceiling |
| `CONTACT_IMPORT_SESSION_TTL_HOURS` | 24 | Session lifetime |
| `CONTACT_IMPORT_MAX_OPEN_SESSIONS` | 3 | Concurrent open sessions per tenant |
| `CONTACT_IMPORT_COMMIT_BATCH_SIZE` | 1000 | Rows per commit savepoint |
| `CONTACT_MAX_TAGS_PER_CONTACT` | 25 | Tag ceiling |

Requires a Celery worker **and** beat scheduler.

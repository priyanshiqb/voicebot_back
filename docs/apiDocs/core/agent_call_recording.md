# Agent Call Recording API Documentation

The MP3 of a call. Because audio files are large, they are uploaded **directly to blob storage** rather than
through this API — a two-step flow that hands the client a temporary signed URL, then verifies what
actually arrived.

Three things to know before you start:

- **These endpoints live on the call-log viewset.** There is no `/agent-call-recordings/` route;
  the paths are under `/api/v1/core/agent-call-logs/`. A recording has no list, retrieve, update, or
  delete endpoint of its own — it is only ever read nested inside a
  [call log](./agent_call_log.md).
- **A recording is created before, and independently of, its call log.** Upload first, then pass
  the returned id as `recording_file` when you create the log. An uploaded recording that is never
  referenced simply sits there.
- **Both steps must resolve the same tenant.** For an agent token that means sending the same
  `X-Tenant-ID` and the same `agent` on both calls — a key authorised against one tenant and
  confirmed against another is precisely what the checks here exist to prevent.

**Limits:** MP3 only (`audio/mpeg` or `audio/mp3`), maximum **50 MB**.

> **VB_AGENT access:** both endpoints are open to agent tokens (`request_upload_url`,
> `confirm_upload`), and `X-Tenant-ID` is **mandatory** for them. Ordinary users may also call these
> endpoints; their tenant comes from their mapping and `agent` is not required.
> See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/agent-call-logs/request-upload-url/` | Reserve a row, get a presigned `PUT` URL |
| `POST` | `/agent-call-logs/confirm-upload/` | Verify the object landed and mark it usable |

There is deliberately **no delete**. A recording is call evidence; removing it is an
administrative action, not an API one.

---

## The upload flow

```
1. POST /request-upload-url/   →  { file_id, upload_method, upload_url, upload_headers, max_bytes }
2. send the MP3 bytes          →  straight to blob storage, with upload_headers
3. POST /confirm-upload/       →  { file_id }  →  recording is CONFIRMED
4. POST /agent-call-logs/      →  recording_file: <file_id>
```

Step 2 goes to Azure Blob storage, not to this API. Send the body raw to `upload_url` with
**every header from `upload_headers`**; success is `201`.

```js
await fetch(data.upload_url, {
  method: "PUT",
  headers: data.upload_headers,   // { "x-ms-blob-type": "BlockBlob", "Content-Type": "audio/mpeg" }
  body: file,
});
```

> Azure rejects a block-blob PUT without `x-ms-blob-type`, with a `400` that comes from the
> storage account rather than from this API. `upload_fields` is always `{}` — it held the S3
> POST policy's form fields, which Azure has no equivalent for.

### `upload_status`

| Status | Meaning |
|---|---|
| `PENDING` | Row reserved, upload outstanding. Set by step 1. |
| `CONFIRMED` | Object verified in the container. Set by step 3, and the default for rows created any other way. |
| `FAILED` | The presigned URL could not be issued, or the uploaded object was oversized and deleted. |

> **A `PENDING` recording can still be attached to a call log.** Nothing checks `upload_status` when
> you set `recording_file`, so a log can reference a recording whose bytes never arrived. Confirm
> first.

---

## Endpoint Details

### 1. Request an Upload URL

Reserves a recording row and returns a short-lived signed (SAS) URL to `PUT` the audio to.

- **Path**: `POST /api/v1/core/agent-call-logs/request-upload-url/`
- **Headers**: `Authorization: Bearer <token>`, plus `X-Tenant-ID: <tenant uuid>` for agent tokens

### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `filename` * | string | The recording's name, e.g. `call_123.mp3`. Used for the extension and the stored object key. | Required. Max **255**. Sanitised to a basename before the key is built, so a crafted value cannot steer the upload outside your tenant's prefix. |
| `content_type` * | enum | MIME type. Comes back in `upload_headers`; send it from there rather than reconstructing it. | Required. Only `audio/mpeg` or `audio/mp3`. Anything else → 400. |
| `file_size_in_bytes` * | integer | Declared size. A **declaration**, not the enforced ceiling — the real limit is `max_bytes` (50 MB), enforced during the upload only under `POST` and otherwise at confirm. | Required. Must be **≥ 1**. |
| `agent` | UUID | Which bot produced the recording. | **Required for agent tokens**; ignored for ordinary users. Must exist and belong to the tenant named in `X-Tenant-ID`. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Upload URL generated successfully.",
    "data": {
      "file_id": "123e4567-e89b-12d3-a456-426614174000",
      "upload_method": "PUT",
      "upload_url": "https://<acct>.blob.core.windows.net/<container>/t-7bf2…/call_recordings/…?sv=…&sp=c&sig=…",
      "upload_fields": {},
      "upload_headers": {
        "x-ms-blob-type": "BlockBlob",
        "Content-Type": "audio/mpeg"
      },
      "expires_in_minutes": 15,
      "max_bytes": 52428800,
      "object_key": "t-7bf26cf4-…/call_recordings/123e4567-…_call_123.mp3"
    },
    "errors": null
  }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | URL issued. The row exists with `upload_status: PENDING`. | `Upload URL generated successfully.` |
| **400** | `content_type` not an MP3 type, `file_size_in_bytes` below 1, or a required field missing. | first validation error |
| **400** | Agent token: `agent` omitted. | `agent field is required for VB_AGENT.` |
| **400** | Agent token: `X-Tenant-ID` missing, malformed, or naming no active tenant. | `This header is required for agent tokens.` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | Ordinary user with no active tenant mapping. | `No active tenant mapping.` |
| **403** | Agent token: the `agent` belongs to a different tenant than `X-Tenant-ID`. | `The specified agent does not belong to the requested tenant.` |
| **404** | Agent token: no such `agent`. | `Agent not found.` |
| **500** | The storage service could not issue a URL. The row is marked `FAILED`. | `Failed to generate upload URL.` |

> **Keep `file_id`; ignore `object_key`.** `object_key` is **deprecated**, returned only so older
> clients that echo it back keep working. Step 2 resolves the recording by `file_id` alone.

> **The declared size is not checked against 50 MB here.** Only `≥ 1` is enforced at this step; the
> real ceiling is applied in step 2 against the object's actual size. Do not rely on this call to
> reject an oversized file.

> **A `500` leaves a `FAILED` row behind.** Retry with a fresh `request-upload-url` rather than
> reusing the id — the failed row has no usable URL.

---

### 2. Confirm the Upload

Call after your `PUT` to blob storage succeeds. Verifies the object really arrived, records its true size,
and marks the row usable.

- **Path**: `POST /api/v1/core/agent-call-logs/confirm-upload/`
- **Headers**: `Authorization: Bearer <token>`, plus `X-Tenant-ID: <tenant uuid>` for agent tokens

### Request fields

| Field | Type | Description | Validation |
|---|---|---|---|
| `file_id` * | UUID | The id from step 1. **Preferred and sufficient on its own.** | Required unless `object_key` is supplied. Must belong to the resolved tenant. |
| `object_key` | string | **Deprecated.** The legacy way to identify the upload. | Used only when `file_id` is absent. Must sit inside the resolved tenant's `call_recordings` prefix. |
| `original_name` | string | Display name for the recording. | Optional with `file_id` (the name from step 1 is kept). **Required** on the legacy `object_key` path. Max **255**. |
| `extension` | string | Override the derived extension. | Optional, may be blank. Max **10**. Derived from `original_name` when omitted. |
| `agent` | UUID | Which bot produced the recording. | **Required for agent tokens**, and must resolve to the same tenant as in step 1. Ignored for ordinary users. |

- **Success Response (201 Created)**:
  ```json
  {
    "status": true,
    "message": "File confirmed and saved successfully.",
    "data": {
      "id": "123e4567-e89b-12d3-a456-426614174000",
      "tenant": { "id": "7bf2…", "name": "Alpha Corp", "subdomain": "alpha-corp" },
      "file": "https://<acct>.blob.core.windows.net/<container>/…?sv=…&sp=r&se=…&sig=…",
      "original_name": "call_123.mp3",
      "extension": ".mp3",
      "file_size_in_bytes": 2411008,
      "is_processed": false,
      "created_at": "2026-06-23T17:38:59.000Z",
      "updated_at": "2026-06-23T17:39:01.000Z",
      "deleted_at": null,
      "is_deleted": false
    },
    "errors": null
  }
  ```

| Status | When | `message` |
|---|---|---|
| **201** | Verified and recorded. Now usable as `recording_file`. | `File confirmed and saved successfully.` |
| **400** | Neither `file_id` nor `object_key` supplied. | `Provide file_id (preferred) or object_key.` |
| **400** | `original_name` missing on the legacy `object_key` path. | `This field is required when using object_key.` |
| **400** | The object is not in the container — the `PUT` never landed. | `File not found in storage. Did the upload succeed?` |
| **400** | The **actual** object is over 50 MB. **The object is deleted from blob storage** and the row marked `FAILED`. | `File size exceeds the limit of 50 MB.` |
| **400** | Agent token: `agent` omitted, or `X-Tenant-ID` missing/invalid. | `agent field is required for VB_AGENT.` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | Ordinary user with no active tenant mapping. | `No active tenant mapping.` |
| **403** | Agent token: the `agent` belongs to a different tenant than `X-Tenant-ID`. | `The specified agent does not belong to the requested tenant.` |
| **404** | `file_id` unknown or another tenant's. | `Recording not found.` |
| **404** | Agent token: no such `agent`. | `Agent not found.` |
| **404** | An `object_key` outside the resolved tenant's prefix. | `File not found in storage. Did the upload succeed?` |
| **500** | The storage service could not be queried. | `Failed to verify file upload.` |

> **The declared size from step 1 is overwritten with the object's real size.** `file_size_in_bytes`
> in the response is what storage actually holds, not what you claimed — so it is safe to display.

> **Oversized uploads are deleted, not merely rejected.** Exceeding 50 MB removes the object from
> the container and marks the row `FAILED`, so a rejected upload cannot linger as an orphan you are
> billed for.

> **A foreign `object_key` returns 404, not 403.** Deliberate — a 403 would confirm the object
> exists in some other tenant.

> **The tenant is resolved before anything touches blob storage.** The oversize branch deletes the object it
> was handed, so an unauthorised key must never reach it.

---

### 3. Attaching a Recording to a Call Log

Pass the `file_id` as `recording_file` when creating the log:

```json
{
  "provider_call_id": "call-001",
  "agent": "b4e1…",
  "caller_number": "+919388025735",
  "direction": "inbound",
  "recording_file": "123e4567-e89b-12d3-a456-426614174000"
}
```

On read, the log expands it:

```json
"recording_file": {
  "id": "123e4567-e89b-12d3-a456-426614174000",
  "original_name": "call_123.mp3",
  "extension": ".mp3",
  "file_size_in_bytes": 2411008,
  "file": "https://<acct>.blob.core.windows.net/<container>/…?sv=…&sp=r&se=…&sig=…"
}
```

> **`file` is a presigned download URL valid for 24 hours**, regenerated on every read. Never cache
> or persist it — store the recording `id` and re-fetch the log when you need a fresh link.

> **A recording may be referenced by more than one call log.** Nothing enforces a one-to-one
> relationship, so the same id can be attached repeatedly. That is not a supported pattern; it is
> simply not prevented.

> **Deleting a recording nulls the link, it does not delete the log.** The relation is `SET_NULL`,
> so `recording_file` becomes `null` and the call history survives.

---

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | Recordings have no list, retrieve, or delete endpoint. | They can only be read nested inside a call log. Orphaned uploads — reserved or confirmed but never attached — are invisible through the API and accumulate. |
| 2 | `upload_status` is not exposed by the serializer. | A client cannot tell a `PENDING` recording from a `CONFIRMED` one, and nothing blocks attaching an unconfirmed recording to a log. |
| 3 | The 50 MB ceiling is not checked at step 1. | An oversized upload is only rejected after the bytes have been transferred to blob storage. |
| 4 | `content_type` is not re-verified at step 2. | A file whose declared type was MP3 but whose content is not is stored regardless. |
| 5 | There is no `discard` counterpart to the Knowledge Base flow. | Failed and abandoned uploads cannot be cleaned up through the API. |

## Related

- [Agent Call Log API](./agent_call_log.md) — where recordings are attached and read
- [Knowledge Base API](./knowledge_base.md) — the same two-step upload pattern for documents
- [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md)

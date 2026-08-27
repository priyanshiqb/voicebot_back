# Frontend Migration — S3 ➜ Azure Blob Uploads

Object storage moved from Cloudflare R2 (S3 API) to **Azure Blob Storage**. The upload
*architecture* is unchanged — still three calls, still direct-to-storage, still no object key
in the client's hands. What changed is **how step 2 sends the bytes**.

> ## The one-line version
>
> **Step 2 must now send the headers the API gives you in `upload_headers`.** Everything else
> is either backwards-compatible or a status-code detail.

---

## 1. Is my code affected?

Search your codebase for where you upload to `upload_url`. If you find either of these, you
must change it:

```js
// ❌ hardcoded headers — will now fail with a 400 from Azure
headers: { "Content-Type": contentType }

// ❌ the POST/multipart branch — this mode no longer exists
if (data.upload_method === "POST") { … FormData … }
```

If you already spread `data.upload_headers` into the request, you are done.

---

## 2. The required change

### Before

```js
const ticket = (await api.post("/knowledge-base/request-upload-url/", meta)).data.data;

const res = await fetch(ticket.upload_url, {
  method: "PUT",
  headers: { "Content-Type": file.type },
  body: file,
});
if (res.status !== 200) throw new Error("upload failed");
```

### After

```js
const ticket = (await api.post("/knowledge-base/request-upload-url/", meta)).data.data;

// Client-side size check: the server can no longer reject an oversize body
// mid-upload, so this is the only fast feedback the user gets. See §5.
if (file.size > ticket.max_bytes) {
  throw new Error(`File is too large. Maximum is ${ticket.max_bytes} bytes.`);
}

const res = await fetch(ticket.upload_url, {
  method: ticket.upload_method,      // "PUT"
  headers: ticket.upload_headers,    // ← the change. Pass through verbatim.
  body: file,
});
if (!res.ok) throw new Error("upload failed");   // 201, not 200
```

**Do not** filter, rename, lowercase or reorder the entries in `upload_headers`, and do not
merge your own `Content-Type` over it. Pass the object through as it arrived. Today it holds
two entries; treating it as opaque means a future addition needs no frontend release.

---

## 3. What `upload_headers` contains

```json
"upload_headers": {
  "x-ms-blob-type": "BlockBlob",
  "Content-Type": "application/pdf"
}
```

`x-ms-blob-type` is **mandatory** — Azure has no default. Omitting it fails the upload with:

```
HTTP 400
<Error><Code>MissingRequiredHeader</Code>
<Message>An HTTP header that's mandatory for this request is not specified.</Message></Error>
```

That response comes from the **storage account, not from our API**, so it will not appear in
backend logs and it is not a backend outage. If uploads start failing this way, check that the
headers are being forwarded before raising it with the backend team.

---

## 4. Full field reference

The step-1 response envelope is identical apart from one added field:

| Field | Status | Notes |
|---|---|---|
| `file_id` / `media_id` | unchanged | The handle for confirm and every later step |
| `upload_method` | unchanged | Now always `"PUT"`. Still branch on it rather than hardcoding |
| `upload_url` | unchanged shape | Now an Azure SAS URL (`?sv=…&sp=c&sig=…` instead of `?X-Amz-…`). Treat as opaque |
| **`upload_headers`** | **NEW** | **Must be sent.** See §3 |
| `upload_fields` | unchanged | Now always `{}`. Kept so code reading it unconditionally does not break. Ignore it |
| `expires_in_minutes` | unchanged | 15 |
| `max_bytes` | unchanged | But now advisory at upload time — see §5 |
| `object_key` | unchanged | Still deprecated, still ignore it |

Steps 1 and 3 (`request-upload-url`, `confirm-upload`) are **completely unchanged** — same
request bodies, same responses, same error shapes.

---

## 5. `max_bytes` is no longer enforced during the upload

This is a real behavioural regression and worth understanding rather than working around.

The S3 backend could sign a `content-length-range` into a POST policy, so the object store
itself rejected an oversize body **while it was uploading**. **An Azure SAS carries no size
condition at any API version.** This is a limitation of Azure, not a configuration choice, and
it cannot be restored.

What this means for you:

- A ticket issued for a 1 MB file will accept a 500 MB one.
- The overage is caught at **step 3 (`confirm-upload`)**, which measures the stored object,
  deletes it, and fails the row — *after* the user has waited for the whole transfer.
- **So check `file.size` against `max_bytes` before uploading.** It is now the only place a
  user gets fast feedback, and without it someone can wait several minutes for an upload that
  was always going to be rejected.

Server-side enforcement still exists and still protects storage. It just costs the transfer.

---

## 6. Status codes

| Step | Before (S3) | After (Azure) |
|---|---|---|
| Step 2 success | `200` | **`201 Created`** |
| Step 2, POST mode | `204` | mode removed |
| Missing required header | n/a | `400` from Azure |
| Expired ticket | `403` | `403` |

Prefer `if (!res.ok)` over `res.status === 200`.

---

## 7. Things that did *not* change

- The three-call flow, and the fact that bytes never pass through our API.
- You still never send or receive a usable object key. `confirm-upload` resolves by id alone.
- Ticket lifetime is still 15 minutes; request one when the user starts uploading.
- Still do **not** attach your JWT to `upload_url` — it is already signed, and an
  `Authorization` header alongside a SAS token is rejected.
- Download URLs are still short-lived signed URLs generated per request. Do not cache them.
- Extension/content sniffing at confirm is unchanged: renaming `report.pdf` to `photo.jpg`
  still passes steps 1 and 2 and is still rejected at step 3.

---

## 8. Testing your change

Ask the backend team for a local Azurite environment, or hit any deployed environment. A
correct implementation shows:

1. Upload succeeds with `201`.
2. Removing `x-ms-blob-type` from the request produces a `400` from the storage host — this is
   worth verifying deliberately once, so the failure is recognisable if it appears in the wild.
3. Uploading the same ticket twice fails the second time with `403`. This is intentional: the
   ticket is signed with *create* permission, so a replayed or leaked URL cannot overwrite an
   object that was already confirmed. **Retry by requesting a fresh ticket**, never by reusing
   the old URL.

---

## Related

- [File Upload — Complete Integration Flow](file-upload.md) — the full contract
- [Knowledge Base](../apiDocs/core/knowledge_base.md) ·
  [Catalog Media](../apiDocs/core/catalog_item_media.md) ·
  [Contact Import](../apiDocs/core/contact_import.md) ·
  [Call Recordings](../apiDocs/core/agent_call_recording.md)
- [Azure Blob Storage Service](../architecture/azure-blob-storage-service.md) — the backend side

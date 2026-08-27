# File Upload — Complete Integration Flow

Every file in this platform is uploaded the same way, through four endpoints that share
one contract. This page is the whole picture: what to send, what comes back, what breaks.

> **Object storage is Azure Blob.** Step 2 has one requirement that did not exist under
> the previous S3 backend: **you must send the headers the ticket gives you in
> `upload_headers`**, including `x-ms-blob-type: BlockBlob`. Azure rejects the upload
> without it — and the rejection comes from the storage account, not from this API, so it
> looks like a storage outage rather than a client bug. See
> [Frontend migration guide](frontend-azure-blob-migration.md).

---

## The shape

Files never pass through this API. You ask for a ticket, upload straight to object
storage, then tell us it arrived.

```
1. POST …/request-upload-url/   →  { file_id | media_id, upload_method, upload_url, … }
2. PUT  <upload_url>            →  the bytes, straight to storage (not to this API)
3. POST …/confirm-upload/       →  { file_id | media_id }  →  row becomes CONFIRMED
4. attach the id to its parent  →  (not needed for catalog media — see below)
```

**Why three calls instead of one.** The bytes go straight to storage, so a 50 MB
recording never occupies a Django worker, never hits a request timeout, and never costs
app-tier bandwidth. The row is created at step 1 rather than step 3 so the object key can
stay server-side — which is why **you never send or receive an object key**. A client
that cannot name a key cannot name someone else's.

**Only `CONFIRMED` files are ever served or processed.** A row that never reaches step 3
is invisible, and is deleted with its object after **24 hours**.

---

## The four flows

| What you are uploading | Base path |
|---|---|
| Catalog images / video / documents | `/api/v1/core/catalog-item-media/` |
| Knowledge Base documents | `/api/v1/core/knowledge-base/` |
| Contact import CSV | `/api/v1/core/contact-imports/` |
| Call recordings (MP3) | `/api/v1/core/agent-call-logs/` |

All require a JWT. Only the call-recording flow also accepts machine (`VB_AGENT`) tokens.

---

## Step 1 — Request a ticket

`POST <base>/request-upload-url/`

### Common fields

Every flow takes these three:

| Field | Type | Required | Notes |
|---|---|---|---|
| `filename` | string ≤255 | ✅ | The extension decides what is allowed. Sanitised server-side to a basename — `../../x.pdf` cannot steer the key |
| `content_type` | string ≤100 | ✅ | **Signed into the ticket.** Step 2 must match it exactly or storage rejects the signature |
| `file_size_in_bytes` | integer ≥1 | ✅ | Send `file.size`. Checked against the flow's ceiling before any URL is issued |

### Per-flow extras

**Catalog media** — `POST /api/v1/core/catalog-item-media/request-upload-url/`

| Field | Type | Required | Notes |
|---|---|---|---|
| `item` | uuid | ✅ | **The item must already exist.** Another tenant's item reads as `No such item.` |
| `media_type` | enum | — | `IMAGE` (default) · `VIDEO` · `DOCUMENT`. Decides the extension allowlist |
| `alt_text` | string ≤255 | — | Accessibility text / caption |
| `is_primary` | bool | — | Default `false`. Can also be set at confirm |

**Call recording** — `POST /api/v1/core/agent-call-logs/request-upload-url/`

| Field | Type | Required | Notes |
|---|---|---|---|
| `content_type` | enum | ✅ | Only `audio/mpeg` or `audio/mp3` |
| `agent` | uuid | conditional | **Required for `VB_AGENT` tokens**, ignored for ordinary users. Must belong to the tenant in `X-Tenant-ID` |

**Knowledge Base** and **contact import** take only the three common fields.

### Response

```json
{
  "status": true,
  "message": "Upload URL generated successfully",
  "data": {
    "media_id": "7f3a91cd-…",
    "upload_method": "PUT",
    "upload_url": "https://<acct>.blob.core.windows.net/<container>/t-<tenant>/catalog/<item>/<uuid>_photo.jpg?sv=…&sp=c&sig=…",
    "upload_fields": {},
    "upload_headers": {
      "x-ms-blob-type": "BlockBlob",
      "Content-Type": "image/jpeg"
    },
    "expires_in_minutes": 15,
    "max_bytes": 10485760
  },
  "errors": null
}
```

| Field | Meaning |
|---|---|
| `media_id` / `file_id` | **The handle for every later step.** Catalog uses `media_id`; the other three use `file_id` |
| `upload_method` | Always `PUT`. Declared rather than implied so you branch on it instead of hardcoding the verb |
| `upload_url` | Where the bytes go. Object storage, not this API |
| `upload_headers` | **Send every entry verbatim.** Azure refuses the upload without `x-ms-blob-type` |
| `upload_fields` | Always `{}`. Held the S3 POST policy's signed form fields; Azure has no equivalent. Kept so clients reading it unconditionally do not break |
| `expires_in_minutes` | 15. Request a fresh ticket rather than caching one |
| `max_bytes` | The server's ceiling for this flow |

KB, contact import and call recording also return a deprecated `object_key`. **Ignore
it.** It is returned only so older clients keep working; confirm resolves by id alone.

---

## Step 2 — Send the file

Pass `upload_headers` straight through. Do not filter it, do not reorder it, and do not
substitute your own `Content-Type` — spread the object as-is:

```js
const res = await fetch(data.upload_url, {
  method: "PUT",
  headers: data.upload_headers,   // { "x-ms-blob-type": "BlockBlob", "Content-Type": … }
  body: file,
});
if (!res.ok) throw new Error("upload failed");   // success is 201
```

Do **not** send your JWT to `upload_url`. It is a signed storage URL, and an
`Authorization` header alongside a SAS token is rejected.

> **A missing `x-ms-blob-type` is a `400` from Azure**, with a body reading
> `An HTTP header that's mandatory for this request is not specified`. Nothing in this
> API is involved, so it will not appear in our logs — if uploads fail this way, check
> that the headers are being forwarded before suspecting the backend.

> **`max_bytes` is not enforced during the upload.** A ticket issued for a 1 MB file will
> accept a 500 MB one. An Azure SAS carries no size condition at any API version, so this
> cannot be fixed by configuration. The overage is caught at step 3, which measures the
> stored object, deletes it and fails the row — but the bytes have already been
> transferred by then. **Check the file size client-side against `max_bytes` before
> uploading**; it is the only place a user gets fast feedback.

---

## Step 3 — Confirm

`POST <base>/confirm-upload/`

**Catalog media** — `{"media_id": "…"}`, plus optional `"is_primary": true` to make it the
item's thumbnail in the same call.

**The other three** — `{"file_id": "…"}`.

> KB and call recording still accept a legacy `object_key` instead of `file_id` (with
> `original_name` then required). **Do not build on it** — it is the pattern that allowed
> cross-tenant reads and deletes, and it is logged on every use.

### What confirm actually does

1. Verifies the object exists in storage
2. **Measures it** and applies `max_bytes` to the real size — over the limit, the object
   is deleted and the row marked `FAILED`
3. **Checks the leading bytes against the extension** — a `.jpg` containing a PDF is
   deleted and rejected
4. Records the measured size and flips the row to `CONFIRMED`

Steps 2 and 3 are why a `PUT` upload can still fail at confirm even though storage
accepted it.

Confirming twice is safe — the second call returns `"Upload already confirmed"`.

---

## Step 4 — Attach it to something

This is where the flows genuinely differ.

**Catalog media — nothing to do.** `item` was required at step 1, so the media is already
attached. `POST /catalog-item-media/` returns `405`; the upload flow is the only way in.

**Knowledge Base** — pass the ids when creating or updating a KB:

```json
{ "name": "Acme Guides", "files": [{ "id": "<file_id>", "flag": "create" }] }
```

**Contact import** — start the import with the id:

```json
POST /api/v1/core/contact-imports/     { "file_id": "<file_id>", "default_region": "IN" }
```

Rejected with `400` if the file is not `CONFIRMED`, or if an import already exists for it.

**Call recording** — reference it on the call log:

```json
POST /api/v1/core/agent-call-logs/     { "recording_file": "<file_id>", … }
```

> A KB file or recording that is confirmed but never attached is an **orphan**. It exists,
> costs storage, and belongs to nothing. Use `POST /knowledge-base/discard-files/` with
> `{"file_ids": […]}` when a user abandons the wizard.

---

## Limits

| Flow | Max size | Allowed extensions | Other caps |
|---|---|---|---|
| Catalog `IMAGE` | 10 MB | `.jpg` `.jpeg` `.png` `.webp` `.gif` `.avif` | 10 media per item |
| Catalog `VIDEO` | 10 MB | `.mp4` `.webm` `.mov` | 10 media per item |
| Catalog `DOCUMENT` | 10 MB | `.pdf` `.docx` `.xlsx` `.txt` `.csv` | 10 media per item |
| Knowledge Base | 11 MB | `.pdf` `.docx` `.txt` | — |
| Contact import | 10 MB | `.csv` only | 3 open sessions, 50 000 rows |
| Call recording | 50 MB | `.mp3` (`audio/mpeg`, `audio/mp3`) | — |

**Unconfirmed uploads count toward the catalog per-item cap.** Otherwise requesting a
hundred tickets and never confirming is an unmetered way past it.

---

## Errors

### Step 1

| Status | Meaning |
|---|---|
| `400` | Extension not allowed, size over the ceiling, per-item cap reached, or a missing field |
| `400` | `item` belongs to another tenant → `No such item.` (404-shaped on purpose) |
| `401` / `403` | Missing JWT, or a machine token on a flow that refuses one |
| `500` / `503` | Storage could not issue a ticket. The row is marked `FAILED` — request a new one |

### Step 3

| Status | Meaning | What to do |
|---|---|---|
| `400` | Object not in storage | The upload did not land. Start again from step 1 |
| `400` | Over `max_bytes` (measured) | The object was deleted. Row is `FAILED` |
| `400` | Contents contradict the extension | The object was deleted. Row is `FAILED` |
| `404` | Unknown id, or another tenant's | Not a permissions hint — the id simply does not resolve |
| `503` | Storage could not be read | **Retry.** The row is still `PENDING` and the object is still there |

`503` is always safe to retry. `400` is terminal for that ticket — the object is gone and
the row cannot be revived.

---

## Things that will catch you out

1. **Forward `upload_headers` unchanged.** This is the one genuinely new requirement.
   Dropping `x-ms-blob-type` fails every upload with a `400` from Azure.
2. **Do not attach your JWT to `upload_url`.** It is already signed.
3. **Tickets expire in 15 minutes.** Request one when the user actually starts uploading,
   not when the form opens.
4. **Success is `201`, not `200`.** Azure answers `201 Created` for a block-blob PUT.
   Check `res.ok` rather than `res.status === 200`.
5. **The extension must match the bytes.** Renaming `report.pdf` to `photo.jpg` gets
   through step 1 and step 2 and is rejected at step 3.
6. **Catalog needs the item first.** There is no way to upload media and attach it later.
7. **Ignore `object_key`.** Deprecated everywhere it still appears.
8. **Abandoned uploads vanish after 24 hours** — row and object both.

---

## Row lifecycle

```
request-upload-url  →  PENDING  ──confirm──▶  CONFIRMED   (served / processed)
                          │
                          ├──rejected at confirm──▶  FAILED   (object deleted)
                          │
                          └──never confirmed, 24h──▶  swept   (row + object deleted)
```

Only `CONFIRMED` rows appear in listings or reach the processing pipeline.

---

## Related

- [Catalog Item Media](../apiDocs/core/catalog_item_media.md)
- [Knowledge Base](../apiDocs/core/knowledge_base.md)
- [Contact Import](../apiDocs/core/contact_import.md)
- [Call Recordings](../apiDocs/core/agent_call_recording.md)
- [Frontend migration guide](frontend-azure-blob-migration.md) — what changed in the move to Azure Blob
- [Azure Blob Storage Service](../architecture/azure-blob-storage-service.md) — the backend side
- [Maintenance Commands](../commands/README.md) — the sweeps that collect what is left behind

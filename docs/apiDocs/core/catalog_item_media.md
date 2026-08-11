# Catalog Item Media API Documentation

Photos, video and documents attached to catalog items.

- **Base path**: `/api/v1/core/catalog-item-media/`
- **Auth**: JWT. Machine (`VB_AGENT`) tokens are refused.
- **Scope**: tenant-filtered. Another tenant's media is **404**.

> ## Uploads are two-step, and the object key never crosses the wire
>
> 1. `POST /request-upload-url/` → creates a `PENDING` row, returns `{media_id, upload_url}`
> 2. `PUT` the bytes straight to `upload_url`
> 3. `POST /confirm-upload/` with `{media_id}` → verifies and flips to `CONFIRMED`
>
> **You never receive or send an object key.** It is built server-side from the tenant and item
> ids and read back from this tenant-scoped row at confirm time.
>
> That is not ceremony. The stateless alternative — hand the client a key and accept one back —
> is what allowed one tenant to confirm *and delete* another tenant's Knowledge Base files. A
> client cannot name a key it was never given.
>
> There is no plain `POST /` — a media row without a file is not worth creating.

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/core/catalog-item-media/request-upload-url/` | Step 1 — get a presigned PUT URL |
| `POST` | `/core/catalog-item-media/confirm-upload/` | Step 2 — confirm it arrived |
| `GET` | `/core/catalog-item-media/` | List, filter |
| `GET` | `/core/catalog-item-media/{id}/` | Retrieve one |
| `PATCH` | `/core/catalog-item-media/{id}/` | Caption, order, primary flag |
| `DELETE` | `/core/catalog-item-media/{id}/` | Soft delete |
| `POST` | `/core/catalog-item-media/bulk_delete/` | Soft delete several by id |
| `POST` | `/core/catalog-item-media/` | **405** — use the upload flow |

---

## 1. Request an upload URL

`POST /api/v1/core/catalog-item-media/request-upload-url/`

| Field | Type | Required | Validation |
|---|---|---|---|
| `item` | uuid | ✅ | Must belong to the caller's tenant |
| `media_type` | enum | — | `IMAGE` (default) \| `VIDEO` \| `DOCUMENT` |
| `filename` | string | ✅ | ≤255. Extension must match the media type |
| `content_type` | string | ✅ | ≤100. Use the **same value** on the PUT |
| `file_size_in_bytes` | int | ✅ | ≥1, ≤`CATALOG_MAX_MEDIA_BYTES` (default 10 MB) |
| `alt_text` | string | — | ≤255 |
| `is_primary` | bool | — | Default `false` |

### Allowed extensions

| `media_type` | Extensions |
|---|---|
| `IMAGE` | `.jpg` `.jpeg` `.png` `.webp` `.gif` `.avif` |
| `VIDEO` | `.mp4` `.webm` `.mov` |
| `DOCUMENT` | `.pdf` `.docx` `.xlsx` `.txt` `.csv` |

Checked **before** a presigned URL is issued, so an unwanted type never reaches the bucket at all.

### 200 OK

```json
{
  "status": true,
  "message": "Upload URL generated successfully",
  "data": {
    "media_id": "7f3a91cd-…",
    "upload_method": "POST",
    "upload_url": "https://…/<bucket>",
    "upload_fields": {
      "key": "t-<tenant>/catalog/<item>/<uuid>_photo.jpg",
      "Content-Type": "image/jpeg",
      "policy": "eyJleHBpcmF0aW9u…",
      "x-amz-algorithm": "AWS4-HMAC-SHA256",
      "x-amz-credential": "…",
      "x-amz-date": "…",
      "x-amz-signature": "…"
    },
    "expires_in_minutes": 15,
    "max_bytes": 10485760
  },
  "errors": null
}
```

**Branch on `upload_method`; do not assume it.** It is deployment configuration
(`UPLOAD_PRESIGN_METHOD`) and can change without an API version bump. In `PUT` mode the
same response carries `"upload_method": "PUT"`, a direct object URL, and an empty
`upload_fields`.

Note what is still **not** in that response: an object key you could send back. The `key`
inside `upload_fields` is signed into the policy — it cannot be swapped for another — and
`confirm-upload` refuses to accept a key at all. It reads one from the tenant-scoped row.

### Error responses

| Status | When | Body `errors` |
|---|---|---|
| `400` | Extension not allowed for the media type | `{"filename": [".bmp is not a supported image format. Allowed: .avif, .gif, .jpeg, .jpg, .png, .webp."]}` |
| `400` | File too large | `{"file_size_in_bytes": ["At most 10 MB per file."]}` |
| `400` | Item already at the media cap | `{"item": ["'Nike Air Max' already has 10 media files; the maximum is 10. Delete one first — unconfirmed uploads count and are swept on request."]}` |
| `400` | `item` from another tenant | `{"item": ["No such item."]}` |
| `503` | Object storage unreachable | The row is marked `FAILED` |

**Unconfirmed uploads count toward the cap.** Otherwise requesting a hundred upload URLs and never
confirming them is an unmetered way past it.

---

## 2. Send the file

Direct to the object store. Nothing passes through this API. The ticket expires in
**15 minutes** (`UPLOAD_URL_TTL_MINUTES`).

### `upload_method: "POST"` — the default going forward

Every entry in `upload_fields` becomes a form field, and **the file part must be last** —
the object store ignores anything after it. Success is **`204 No Content`**, not `200`.

```js
const form = new FormData();
for (const [name, value] of Object.entries(data.upload_fields)) {
  form.append(name, value);
}
form.append("file", file);            // last, always

const res = await fetch(data.upload_url, { method: "POST", body: form });
if (res.status !== 204) throw new Error("upload failed");
```

Do not set a `Content-Type` header on the request — the browser must set the multipart
boundary itself.

`max_bytes` is signed into the policy, so the store rejects an oversize body **during**
the upload with `EntityTooLarge`. That is the point of this mode: `file_size_in_bytes` at
step 1 is a declaration, and a declaration is not a limit.

### `upload_method: "PUT"` — legacy

```
PUT <upload_url>
Content-Type: <the same content_type you declared>

<binary body>
```

Success is `200`. `upload_fields` is empty. **The size ceiling is not enforced here** — a
ticket issued for a 1 MB file will accept a 500 MB one, and the overage is caught at
step 3, after it has been transferred and stored. It is deleted then, but you have
already paid for the transfer.

---

## 3. Confirm the upload

`POST /api/v1/core/catalog-item-media/confirm-upload/`

```json
{ "media_id": "7f3a91cd-…" }
```

`media_id` and nothing else. Optionally `is_primary: true` to make it the item's thumbnail in the
same call.

The server verifies the object actually arrived, records its **real** size (the declared size at
step 1 is a fallback, not a guarantee), and flips the row to `CONFIRMED`. Only `CONFIRMED` media
is served.

**Idempotent** — a retried confirm after a dropped response returns 200 with
`"Upload already confirmed"` rather than failing.

### 200 OK

```json
{
  "status": true,
  "message": "Upload confirmed",
  "data": {
    "id": "7f3a91cd-…",
    "tenant": { "id": "…", "name": "Acme" },
    "item": "ffc111cd-…",
    "media_type": "IMAGE",
    "media_type_label": "Image",
    "file_url": "https://…?X-Amz-…",
    "original_name": "photo.jpg",
    "extension": ".jpg",
    "file_size_in_bytes": 184320,
    "upload_status": "CONFIRMED",
    "alt_text": "Paneer butter masala, full portion",
    "is_primary": true,
    "sort_order": 0,
    "is_active": true,
    "created_at": "2026-08-11T09:02:11.441Z"
  },
  "errors": null
}
```

| Status | When |
|---|---|
| `404` | Unknown `media_id`, or it belongs to another tenant. **404 not 403** — a 403 would confirm it exists |
| `400` | The object is not in storage. The row is marked `FAILED`; upload and try again |
| `503` | Object storage unreachable during verification |

---

## 4. List and filter

`GET /api/v1/core/catalog-item-media/`

| Parameter | Type | Description |
|---|---|---|
| `search` | string | Matches `original_name`, `alt_text`, the item's `name` |
| `ordering` | string | `sort_order`, `created_at`, `file_size_in_bytes`; `-` prefix for descending |
| `item` | uuid | |
| `media_type` | enum | `IMAGE` \| `VIDEO` \| `DOCUMENT` |
| `upload_status` | enum | `PENDING` \| `CONFIRMED` \| `FAILED` |
| `is_primary` | bool | |
| `is_active` | bool | |

`?upload_status=PENDING` is how you find abandoned uploads — a client that requested a URL and
never finished.

To list only what is safe to display, use
[`GET /core/catalog-items/{id}/media/`](./catalog_item.md#6-media-for-one-item), which returns
confirmed media only.

`file_url` is a **presigned GET URL valid for 24 hours**, regenerated on every read. If object
storage is unavailable the stored key is returned instead rather than failing the whole listing —
a catalog page must not 500 over one thumbnail.

---

## 5. Update

`PATCH /api/v1/core/catalog-item-media/{id}/`

Only `alt_text`, `is_primary`, `sort_order` and `is_active` are writable. The file, its key,
`upload_status`, `extension` and `file_size_in_bytes` belong to the upload flow — a client able to
flip `upload_status` to `CONFIRMED` could publish a key with nothing behind it.

Setting `is_primary: true` demotes the item's current primary in the same transaction.

---

## 6. Delete

`DELETE /api/v1/core/catalog-item-media/{id}/`

Soft delete. `is_primary` is cleared on the way out, so restoring the row later does not collide
with whatever was made primary in the meantime.

Media is also soft-deleted automatically when its parent item is deleted.

---

## Related

- [Catalog Items](./catalog_item.md)
- [Catalog Variants](./catalog_variant.md)
- [Catalog Categories](./catalog_category.md)
- [S3 Storage Service](../../architecture/s3-storage-service.md)

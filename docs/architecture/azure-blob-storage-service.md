# Azure Blob Storage Service Architecture & Request Profiling

`AzureBlobStorageService` (`utils/blob_storage/service.py`) handles every interaction with object
storage. Azure Blob is the backend in all environments; [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite)
serves local development via the compose `emulators` profile.

It replaced an S3/boto3 service (Cloudflare R2 in every environment, MinIO locally). Method
names and return shapes were kept identical through that change, which is why
`core/tests/test_storage_service_contract.py` exists — every upload flow mocks this service,
so the *shape* of what it returns is a contract that no other test can catch drifting.

---

## Configuration Variables

Set in `config/settings/base.py`, documented per-variable in `.env.example` §2.8.

* `AZURE_ACCOUNT_NAME`: Storage account name (`devstoreaccount1` for Azurite).
* `AZURE_ACCOUNT_KEY`: Shared account key. **Required** — see *Why the account key* below.
* `AZURE_CONTAINER`: The single global container holding all client uploads.
* `AZURE_CONNECTION_STRING`: Optional for a real account, **required for Azurite**, whose
  endpoint puts the account in the URL path (`http://azurite:10000/devstoreaccount1`) rather
  than in the subdomain (`https://<acct>.blob.core.windows.net`).
* `AZURE_ENDPOINT_SUFFIX`: Defaults to `core.windows.net`; differs only on sovereign clouds.
* `AZURE_PUBLIC_ENDPOINT`: Browser-reachable host that signed URLs are rewritten to.

`USE_AZURE_STORAGE` is derived all-or-nothing from the first three. Miss one and Django falls
back to `FileSystemStorage` silently — uploads land on the container's own disk and vanish on
restart. `manage.py check` raises `core.W002` for exactly this outside `DEBUG`.

### Why the account key

Signing a **service SAS** requires the shared key. A managed identity can serve
django-storages, but it cannot sign a service SAS without first fetching a user delegation
key — a different code path that is not implemented here.

---

## Two things Azure cannot do that S3 could

Both are load-bearing, and both are easy to reintroduce as bugs.

**A SAS cannot carry a size condition.** S3's presigned POST signed a `content-length-range`
into the policy, so the object store itself refused an oversize body *mid-upload*. Azure has
no equivalent at any API version. Upload tickets are therefore PUT-only, and the per-flow
`max_bytes` ceiling is enforced entirely at confirm — measure with `get_file_disk_usage`, then
`delete_blob_direct` if it is over. That costs the transfer but bounds what is stored.
`UPLOAD_PRESIGN_METHOD=POST` is a `core.E001` error rather than a silent downgrade.

**A SAS cannot pin `Content-Type` on a write.** The `rsct` field overrides the header on
*reads* only. The real defence against a mislabelled upload was never the declared header —
it is the magic-byte check at confirm (`get_object_head_bytes` into `utils/files/signatures.py`),
which reads the bytes and does not care what anyone claimed.

### `x-ms-blob-type` is mandatory

Azure rejects a block-blob PUT without `x-ms-blob-type: BlockBlob`. The request is made by the
browser, not by us, so the only way to get the header there is to declare it in the ticket —
hence `upload_headers` in the upload ticket envelope. A client that drops it gets a `400` from
the storage account rather than from this API, so the failure looks like a storage outage
rather than a version skew. Azurite is deliberately **not** run with `--loose`, so local
development catches this rather than hiding it.

### Uploads are signed `create`, not `write`

`create` refuses a Put Blob against a blob that already exists. A leaked or replayed ticket
therefore cannot overwrite an object that has since been confirmed — a bound the presigned PUT
it replaced did not have. Widening this to `write` to fix a permission error silently removes
the control.

> The refusal is `403`, not the `409 BlobAlreadyExists` you might expect — verified against
> both Azurite and a real storage account, which agree here. Azure evaluates SAS
> authorization *before* blob existence, so a `create`-only SAS aimed at a blob that already
> exists is an authorization failure rather than a conflict. `409` only arises from an
> already-authorized write using `If-None-Match: *`.

---

## API Request Profiling

Azure Blob bills per 10,000 operations, split into **Write** (`Put Blob`, `List Blobs`) and
**Read** (`Get Blob`, `Get Blob Properties`) classes. Delete is not billed.

| Function | Description | Azure SDK call | Write ops | Read ops | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`__init__`** | Builds the client | None | **0** | **0** | Purely local; does not contact the storage account. |
| **`get_presigned_url_for_object`** | Read SAS for one blob | `generate_blob_sas` | **0** | **0** | Signed locally with the account key. Zero network cost. |
| **`get_presigned_put_url`** | Upload SAS for one blob | `generate_blob_sas` | **0** | **0** | Likewise local. The client's own PUT is billed to us as 1 Write. |
| **`get_upload_headers`** | Headers the client must send | None | **0** | **0** | Pure dict construction. |
| **`verify_object_exists`** | Does the blob exist | `exists` | **0** | **1** | A `HEAD` under the covers. |
| **`get_file_disk_usage`** | Size, content type, mtime | `get_blob_properties` | **0** | **1** | Missing blob reports zero rather than raising. |
| **`get_object_head_bytes`** | First N bytes only | `download_blob(offset, length)` | **0** | **1** | Ranged read: identifying a 50 MB recording costs 64 bytes of transfer. |
| **`iter_objects`** | Walk a tenant prefix | `list_blobs` | **N** (1 per 5,000 blobs) | **0** | SDK paginates internally; still a generator. |
| **`delete_blob_direct`** | Permanently remove a blob | `delete_blob` | **0** | **0** | Delete is **not billed**. |

### Egress is the line that changed

R2's headline feature was zero egress. Azure charges roughly **$0.087/GB** beyond the
account-wide 100 GB/month free tier. Operation pricing is close enough to ignore; egress is
not. Two paths pull whole objects out:

* The ETL/RAG service downloads every KB file via its 24-hour read SAS on **every** `run_etl()`
  — and `run_etl` runs on every knowledge-base save, not only when a file changes.
* Every user-facing download of a call recording or catalog image.

At development volume this is inside the free tier. At production volume, KB re-ingestion is
the one to watch, because it re-downloads unchanged files. Skipping the re-fetch when `etl_id`
is unchanged is the obvious follow-up.

> Verify current Azure list prices for your region before quoting these numbers; they move.

---

## Deliberate omissions

Three methods from the S3 service were **not** ported, and should not be reintroduced without
re-reading these reasons:

* **`get_presigned_url_for_folder`** — porting it would be a security downgrade. Azure's
  container-list SAS does not sign the `prefix` parameter, so a holder could edit the prefix
  and list the whole container across every tenant. S3's SigV4 *did* cover `prefix`. If a
  prefix listing is ever needed, return the listing from the API instead.
* **`create_bucket_if_not_exists`** — provision the container in Terraform or the portal.
  Locally: `az storage container create --name voicebot-local --connection-string "$AZURE_CONNECTION_STRING"`.
* **`get_folder_disk_usage`** — `KBFiles.get_tenant_file_stats` already answers this from the
  database, without walking the container.

---

## Behavioural differences that bite

* **Delete is not idempotent.** `delete_blob` raises `ResourceNotFoundError` where S3's
  `delete_object` returned success. The pending-upload sweep and both oversize-rejection paths
  delete objects they do not know still exist, so `delete_blob_direct` swallows the miss to
  preserve the old contract. Letting it surface turns a no-op into a 500.
* **Errors are types, not code strings.** `except ClientError` with
  `e.response["Error"]["Code"] == "404"` becomes `except ResourceNotFoundError`. Note that
  `ResourceNotFoundError` subclasses `HttpResponseError`, so order the handlers accordingly.
* **Host rewriting is now legitimate.** `AZURE_PUBLIC_ENDPOINT` swaps scheme and netloc on a
  signed URL. Under SigV4 that was a gamble, because S3 signs the `Host` header. An Azure
  service SAS signs the canonicalized resource `/blob/<account>/<container>/<blob>` and **not**
  the host, so the signature survives. Only the host moves — the path is preserved, which the
  emulator's `/devstoreaccount1` prefix depends on.

---

## Object keys

Unchanged from the S3 layout: `t-<tenant>/<folder>/<uuid>_<name>`, built and authorized by
`utils/blob_storage/keys.py`, which has no SDK dependency and moved across verbatim. Every flow
that builds or accepts an object key must go through that module — it is what stops a caller
naming another tenant's key, which would otherwise allow both cross-tenant reads and
cross-tenant deletes.

---

## CORS

Browsers upload directly to the storage account, so the **account** needs CORS. It is set per
account, not per container, and Azurite needs it too.

```bash
az storage cors add --account-name <acct> --services b \
    --methods PUT GET HEAD OPTIONS \
    --origins "https://app.example.com" \
    --allowed-headers "x-ms-blob-type,content-type" \
    --exposed-headers "*" --max-age 3600
```

Omitting `x-ms-blob-type` from `--allowed-headers` fails the preflight, and the browser error
does not say which header was rejected.

---

## Local development

The `emulators` compose profile starts Azurite on port 10000 (`AZURITE_BLOB_PORT`), data under
`.data/azurite`. Azurite ships no web console — browse blobs with Azure Storage Explorer or
`az storage blob list`.

* The container is **not** created for you. Run the `az storage container create` above once.
* Set `AZURE_PUBLIC_ENDPOINT` so SAS URLs are reachable from a browser: the `azurite` compose
  hostname does not resolve outside the Docker network.
* **Known trap:** a stale `.data/azurite` directory from an earlier run makes the container
  restart-loop with `Critical error happens during GC`. Delete it; it is disposable.

---

## Cost optimization notes

* **Presigned URLs are free.** `get_presigned_url_for_object` and `get_presigned_put_url` are
  local cryptography — no network call, no billed operation. Generate them freely in serializers
  and API responses.
* **The ranged read is the cheap check.** `get_object_head_bytes` costs one Read op and 64 bytes
  of egress, which is what makes content verification affordable on every confirm.
* **Watch `iter_objects`.** It is the only Write-class operation the app issues outside uploads.
  The orphan audit walks a whole tenant prefix, so run it on a schedule rather than per request.

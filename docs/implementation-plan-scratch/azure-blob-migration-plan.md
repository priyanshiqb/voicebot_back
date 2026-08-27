# Migrate object storage from Cloudflare R2 / S3 to Azure Blob

**Status:** IMPLEMENTED — kept as the record of why the migration is shaped the way it is.
The current-state documentation is
[`docs/architecture/azure-blob-storage-service.md`](../architecture/azure-blob-storage-service.md),
which superseded `s3-storage-service.md`. References below to `utils/s3_storage/`,
`S3StorageService` and boto3 describe the code as it was *before* this landed; they are
history, not instructions.

**Scope:** replace the storage backend, keep the upload architecture · **Data:** existing
objects were disposable (development), so no data migration — see §10 and
`manage.py purge_pre_migration_files`.

---

## 1. Recommended shape

**Replace `utils/s3_storage/` with `utils/blob_storage/`, keeping every method name and
return shape identical.** Do not build a provider abstraction.

The upload architecture — reserve a `PENDING` row, hand out a short-lived ticket, client
uploads direct to storage, confirm verifies existence + size + magic bytes — is provider
agnostic and stays exactly as it is. What changes is the ~200 lines behind
`S3StorageService`, the Django `STORAGES` backend, the env vars, and the local emulator.

Two things make this cheap, and they are worth naming because they are the reason this is a
two-day job rather than a two-week one:

- **Object keys need no change.** `t-<tenant>/<folder>/<uuid>_<name>` is a legal Azure blob
  name as-is. `utils/s3_storage/keys.py` has no boto3 import — it is pure string handling —
  so it moves file-for-file. No model change, no migration, no key-format churn.
- **The ETL service is already decoupled.** [`kb_etl.py:49-58`](../../core/serializers/kb_etl.py#L49-L58)
  hands the ETL a *presigned URL*, not an object key or a bucket name. An Azure SAS URL is an
  ordinary HTTPS GET URL, so `voicebot-etl` needs **zero changes** and does not need to be
  deployed in lockstep.

Why not a pluggable `StorageBackend` interface with S3 and Azure implementations: you are
committing to Azure and throwing the R2 data away. An abstraction for one live provider is
speculative generality that every future reader has to step through. If Azure is ever
reversed, `git revert` on one self-contained commit is the cheaper undo.

---

## 2. The one real blocker: presigned POST cannot be ported

`get_presigned_post` ([`service.py:155-207`](../../utils/s3_storage/service.py#L155-L207))
signs a `content-length-range` condition into an S3 POST policy, so the object store rejects
an oversize body **during** the upload. **Azure Blob has no equivalent.** A SAS token carries
permissions, expiry, resource scope and optional IP/protocol restrictions — there is no
signable size condition for a block blob at any API version.

What this costs in practice: **nothing today, and it forecloses an upgrade path.**

| | Today (R2) | After (Azure) |
|---|---|---|
| `UPLOAD_PRESIGN_METHOD` | `PUT` ([base.py:606](../../config/settings/base.py#L606)) | `PUT` — the only option |
| Size enforced during upload | No | No |
| Size enforced at confirm | Yes — `get_file_disk_usage` then `delete_blob_direct` | Unchanged |
| `core.W001` warning fires | Yes | Yes |

You are already running PUT-only, so no control that is live today is lost. What is lost is
the `POST` option that [`checks.py:42-55`](../../core/checks.py#L42-L55) explicitly tells the
next developer to switch on "once the frontend sends multipart form uploads." On Azure that
sentence becomes false and must be rewritten, or someone will spend a day trying to implement
it.

**Decision: ship PUT-only, delete the POST branch, and correct the check's hint.** Keeping a
`get_presigned_post` that raises `NotImplementedError` is worse — it leaves `UPLOAD_METHOD_POST`
reachable from config and turns a settings typo into a 500 on every upload endpoint.

Two mitigations that partly close the gap, both cheap:

- **Issue `create`-only SAS, not `write`.** Azure's `c` permission refuses a Put Blob against
  a blob that already exists. A leaked ticket then cannot overwrite anything — a bound the
  current presigned PUT does not have.
- **Keep the confirm-time delete path.** It already exists and already works; it is what
  bounds storage cost when someone ignores `max_bytes`.

The alternative — proxying uploads through Django with a hard byte cap — gives back true
in-flight enforcement but costs double bandwidth and gives up direct-to-storage entirely.
Not recommended for a size limit that confirm already catches within seconds.

---

## 3. Second frontend-visible change: `x-ms-blob-type`

An Azure SAS PUT **requires** the `x-ms-blob-type: BlockBlob` request header. Without it Azure
answers `400 An HTTP header that's mandatory for this request is not specified`.

The ticket envelope has nowhere to put that today. Add a field:

```diff
  {
    "upload_method": "PUT",
    "upload_url": "...",
    "upload_fields": {},        # stays, always empty — kept so the schema doesn't churn
+   "upload_headers": {"x-ms-blob-type": "BlockBlob", "Content-Type": "application/pdf"},
    "expires_in_minutes": 15,
    "max_bytes": 10485760
  }
```

Declared once in [`upload_ticket_fields()`](../../core/serializers/uploads.py#L20) and picked
up by all four `request-upload-url` endpoints automatically — that shared-fields helper exists
precisely so this is one edit rather than four.

**This is a breaking change for the frontend and must ship coordinated.** A client that sends
the raw body without the header gets a 400 from Azure, not from us, so the failure will look
like a storage outage rather than a version skew. Put `upload_headers` in the response
*before* switching the storage backend, populated from the ticket, so the frontend can adopt
it against the still-working R2 stack.

---

## 4. Cost model changes — read before committing

R2's headline feature is **zero egress**. Azure Blob charges for it.

| | Cloudflare R2 | Azure Blob (Hot, LRS) |
|---|---|---|
| Egress | **Free** | ~$0.087/GB after the 100 GB/month account-wide free tier |
| Write ops | $4.50 / million (Class A) | ~$0.055 / 10k = $5.50 / million |
| Read ops | $0.36 / million (Class B) | ~$0.0044 / 10k = $0.44 / million |
| Delete | Free | Free ("other" ops, but Delete Blob is not billed) |
| Storage | $0.015/GB-mo | ~$0.018/GB-mo |

The operation pricing is close enough to ignore. **Egress is the line that changes.** Two
paths in this codebase pull whole objects out of storage:

- The ETL/RAG service downloads every KB file via the 24h presigned URL on every `run_etl()` —
  and `run_etl` is called on *every* knowledge-base save, not only on file change.
- Every user-facing download of a call recording or catalog image.

At development volume this is inside the free tier and irrelevant. At production volume, KB
re-ingestion is the one to watch, because it re-downloads unchanged files. Worth a follow-up
to skip re-fetch when `etl_id` is unchanged — out of scope here, noted so it isn't a surprise.

Verify current Azure list prices for your region before quoting these numbers; they move.

---

## 5. Inventory

Everything that touches object storage, and what happens to it.

### 5.1 Rewrite

| File | Change |
|---|---|
| `utils/s3_storage/service.py` → `utils/blob_storage/service.py` | `S3StorageService` → `AzureBlobStorageService`. Same public methods, same return dicts. ~200 lines rewritten |
| `utils/s3_storage/keys.py` → `utils/blob_storage/keys.py` | **Moved verbatim.** No boto3 dependency; the one comment mentioning "MinIO on a filesystem backend" becomes "Azurite on a filesystem backend" |
| `config/settings/base.py:223-273` | `S3_*` → `AZURE_*`; `USE_S3_STORAGE` → `USE_AZURE_STORAGE`; `STORAGES["default"]` → `storages.backends.azure_storage.AzureStorage` |
| `core/services/uploads.py` | Drop the `UPLOAD_METHOD_POST` branch in `build_upload_ticket`; return `upload_headers` |
| `core/serializers/uploads.py` | Add the `upload_headers` field |
| `core/checks.py:42-55` | Rewrite the `core.W001` hint — POST is not an available upgrade on Azure |
| `pyproject.toml` | `- boto3`, `- django-storages[s3]`; `+ django-storages[azure]`, `+ azure-storage-blob>=12.24` |
| `docker-compose/local-storage.yml` | MinIO → Azurite |
| `.env` / `.env.example` / `.env.local.example` | §2.8 rewritten; MinIO block (§1.7) → Azurite |

### 5.2 Delete rather than port — dead code

Confirmed to have **no callers anywhere** outside the class itself:

- `get_presigned_url_for_folder` — and porting it would be a security downgrade. Azure's
  container-list SAS does not sign the `prefix` parameter, so a holder can edit the prefix and
  list the whole container across tenants. S3's SigV4 *does* cover `prefix`. Delete it; if a
  prefix listing is ever needed, return the listing from the API rather than a SAS.
- `create_bucket_if_not_exists` — provision the container in Terraform/portal instead.
- `get_folder_disk_usage` — `KBFiles.get_tenant_file_stats` already answers this from the DB.

### 5.3 Rename-only — import path and symbol

Ten call sites, all mechanical:

`core/views/knowledge_base.py` · `core/views/catalog_item_media.py` ·
`core/views/contact_import.py` · `core/views/agent_call_log.py` ·
`core/serializers/agent_call_recording.py` · `core/serializers/kb_etl.py` ·
`core/serializers/kb_files.py` · `core/serializers/catalog_item_media.py` ·
`core/services/uploads.py` · `core/services/contact_import.py:839`

Plus the `USE_S3_STORAGE` reads in `config/urls.py:56` and the four serializers.

### 5.4 Untouched — and worth confirming they stay untouched

- All four models and their `upload_to` callables. Key format is unchanged.
- `find_orphaned_objects`, `sweep_pending_uploads`, and the three audit/cleanup management
  commands — they call `iter_objects` and `record.delete()`, both of which keep their contract.
- `utils/files/signatures.py` and the whole content-verification path.
- **`voicebot-etl`.** It consumes a signed HTTPS URL; a SAS URL is one.
- No migrations. No model fields change.

---

## 6. API mapping

The full translation, so the rewrite is transcription rather than design.

| Method | boto3 today | azure-storage-blob | Notes |
|---|---|---|---|
| `__init__` | `boto3.client("s3", …)` | `BlobServiceClient.from_connection_string(...)` | Hold a `ContainerClient`; derive `BlobClient` per call |
| `get_presigned_url_for_object` | `generate_presigned_url("get_object")` | `generate_blob_sas(permission=BlobSasPermissions(read=True))` | Build `f"{blob.url}?{sas}"`. Local, no network call — same as today |
| `get_presigned_put_url` | `generate_presigned_url("put_object")` | `generate_blob_sas(permission=BlobSasPermissions(create=True))` | `create` not `write` — see §2 |
| `get_presigned_post` | `generate_presigned_post` | — | **No equivalent.** Deleted |
| `verify_object_exists` | `head_object` → `404` | `blob.get_blob_properties()` → `ResourceNotFoundError` | |
| `get_file_disk_usage` | `head_object` | `blob.get_blob_properties()` | `.size`, `.content_settings.content_type`, `.last_modified` |
| `get_object_head_bytes` | `get_object(Range=…)` | `blob.download_blob(offset=0, length=n).readall()` | Map `ResourceNotFoundError` **and** `InvalidRange` to `b""` — the docstring contract is "empty means nothing to check" |
| `iter_objects` | `list_objects_v2` paginator | `container.list_blobs(name_starts_with=prefix)` | SDK paginates internally; still a generator. `.name` / `.size` / `.last_modified` |
| `delete_blob_direct` | `delete_object` | `blob.delete_blob()` | Swallow `ResourceNotFoundError` — S3 delete is idempotent, Azure's raises |

Two behavioural differences that will bite if missed:

- **Error types.** Every `except ClientError as e: … e.response["Error"]["Code"] == "404"`
  becomes `except ResourceNotFoundError`. There is no code-string comparison. `delete_blob`
  raising on a missing blob is the specific regression risk — S3 returns success, and the
  sweep and both oversize-rejection paths call delete on objects that may already be gone.
- **Host rewriting is now safe.** The `S3_CUSTOM_DOMAIN` netloc-swap
  ([`service.py:113-118`](../../utils/s3_storage/service.py#L113-L118)) is dubious under SigV4,
  which signs the `Host` header. An Azure SAS signs `/blob/{account}/{container}/{blob}` and
  **not** the host, so the equivalent `AZURE_PUBLIC_ENDPOINT` rewrite is legitimate rather than
  a signature gamble. Keep it — Azurite in Docker needs it, since a browser cannot resolve the
  `azurite` container name.

---

## 7. Configuration

### 7.1 Settings

```python
# Azure Blob Storage
AZURE_ACCOUNT_NAME     = env("AZURE_ACCOUNT_NAME", default=None)
AZURE_ACCOUNT_KEY      = env("AZURE_ACCOUNT_KEY", default=None)
AZURE_CONTAINER        = env("AZURE_CONTAINER", default=None)
AZURE_CONNECTION_STRING= env("AZURE_CONNECTION_STRING", default=None)   # Azurite convenience
AZURE_PUBLIC_ENDPOINT  = env("AZURE_PUBLIC_ENDPOINT", default=None)     # was S3_CUSTOM_DOMAIN

USE_AZURE_STORAGE = all([AZURE_ACCOUNT_NAME, AZURE_ACCOUNT_KEY, AZURE_CONTAINER])
```

Keep the existing all-or-nothing derivation and the silent `FileSystemStorage` fallback —
it is documented behaviour that the tests rely on. But **add a system check** alongside
`core.W001` that warns when `USE_AZURE_STORAGE` is False outside `DEBUG`: the current failure
mode is a production deploy writing to container-local disk and losing every upload on
restart, discovered only when a file 404s.

`AZURE_ACCOUNT_KEY` is required for SAS generation. A managed identity / `AZURE_TOKEN_CREDENTIAL`
can serve django-storages but **cannot sign a service SAS** without first fetching a user
delegation key — a different code path. Use the account key now; note the delegation-key
upgrade as future work if key-less auth becomes a requirement.

### 7.2 CORS — required, easy to forget

Browsers upload direct to Azure, so the *storage account* needs CORS. It is set per account,
not per container, and Azurite needs it too.

```bash
az storage cors add --services b --methods PUT GET HEAD OPTIONS \
  --origins "https://app.example.com" \
  --allowed-headers "x-ms-blob-type,content-type" \
  --exposed-headers "*" --max-age 3600 \
  --account-name <acct>
```

Omitting `x-ms-blob-type` from `--allowed-headers` fails the preflight, and the browser error
says nothing about which header was rejected.

### 7.3 Local dev — Azurite replaces MinIO  ✅ DONE

Shipped: [`docker-compose/local-storage.yml`](../../docker-compose/local-storage.yml). Blob
service only, on port 10000 (`AZURITE_BLOB_PORT`), data under `.data/azurite`. The Caddy route
`azurite{ENV_NAME}.{DOMAIN}` replaces the two MinIO routes — Azurite ships no web console, so
browse local blobs with Azure Storage Explorer or `az storage blob list`.

Two flags carry the reasoning:

- `--skipApiVersionCheck` — azure-storage-blob ships new `x-ms-version` values faster than
  Azurite adopts them, and the mismatch surfaces as an opaque 400.
- **not** `--loose` — loose mode stops enforcing required headers, so an upload missing
  `x-ms-blob-type: BlockBlob` would pass locally and fail against real Azure. Verified: a PUT
  without that header returns 400 from Azurite as configured, which is the behaviour §3 needs
  local dev to catch.

Credentials are Azurite's built-in development account rather than being wired to the app's
variables the way MinIO's root user was:

    AccountName  devstoreaccount1
    AccountKey   Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==

Publicly documented by Microsoft and recognised by every Azure tool. The cost is that app and
emulator *can* now drift — MinIO's reuse of `S3_ACCESS_KEY_ID` made that impossible. Documented
in `.env.example` §1.7 instead.

Azurite's URLs are account-in-path (`http://azurite:10000/devstoreaccount1/<container>/<blob>`)
where production is subdomain-style (`https://<acct>.blob.core.windows.net/<container>/<blob>`).
The SDK handles both from a connection string, but anything parsing the URL by hand must not
assume one shape. The same quirk applies to SharedKey/SAS signing: against the emulator the
account name appears **twice** in the canonicalized resource
(`/devstoreaccount1/devstoreaccount1/container/blob`), because it is account + full URL path.
The SDK handles this too — it only matters if hand-signing.

Set `AZURE_PUBLIC_ENDPOINT` so SAS URLs handed to a browser are reachable from outside the
compose network, exactly as `S3_CUSTOM_DOMAIN` does today.

**Known trap:** a `.data/azurite` directory left by an earlier Azurite run makes the container
restart-loop with `Critical error happens during GC` — the stale database references extents
that no longer exist. Delete the directory; it is disposable. Noted in the runbook.

---

## 8. Tests

Roughly 80 tests touch storage across ten files. Two distinct kinds of work:

**Mechanical (~60 tests).** Anything doing `@patch("core.views.knowledge_base.S3StorageService")`
just needs the symbol renamed. `MagicMock`-based tests in `test_orphaned_object_audit.py`,
`test_pending_upload_sweep.py`, the catalog and contact-import suites do not care what the
backend is.

**Real rewrites (~35 tests).** The ones that build fake boto3 responses:

| File | Tests | Work |
|---|---|---|
| `test_storage_service_contract.py` | 9 | Highest value in the suite and needs the most care. It exists because a confirm flow once read `stats["size_in_bytes"]` from a dict keyed `total_size_bytes` and every test passed. Fake `BlobProperties` replaces the `{"ContentLength": …}` dicts; **the asserted key names must not change** |
| `test_upload_tickets.py` | 24 | ~10 test `get_presigned_post` directly and get **deleted** with it. The rest re-point at `generate_blob_sas`, plus new coverage for `upload_headers` |
| `test_upload_content_verification.py` | 29 | Ranged-read stubs become `download_blob(offset, length)`. Add the `InvalidRange` → `b""` case |
| `test_upload_key_security.py` | 19 | Patch strings only — these assert on key ownership, which is unchanged |

Add three tests that only matter on Azure:

1. `delete_blob_direct` on a missing blob returns cleanly rather than raising — the S3-vs-Azure
   idempotency difference, exercised by the sweep and both rejection paths.
2. A ticket's SAS carries `create` and not `write` — the §2 mitigation, which is silently lost
   if someone "fixes" a permission error by widening it.
3. `upload_headers` contains `x-ms-blob-type: BlockBlob`.

Run with `--settings=config.settings.test_postgres`; the default SQLite test DB is broken for
this project.

---

## 9. Execution order

Sequenced so nothing is broken for more than one step, and the frontend change lands first.

| # | Step | Reversible? |
|---|---|---|
| 1 | Provision the Azure account + container; apply CORS (§7.2); collect the account key | n/a |
| 2 | Add `upload_headers` to the ticket envelope, populated for the *current* S3 stack (`{"Content-Type": …}`). Frontend adopts it against working R2 | Yes — additive |
| 3 | ~~Swap the compose emulator MinIO → Azurite~~ **done** — compose, Caddy route, `init-volumes.sh`, `.env.example` §1.7, `.env.local.example` §2.2, runbook, docker guide | Yes |
| 4 | Add `django-storages[azure]` + `azure-storage-blob`; **leave boto3 installed** so the branch stays runnable while the rewrite is in progress | Yes |
| 5 | Write `utils/blob_storage/` (service + moved keys). Delete the three dead methods and the POST branch | Yes |
| 6 | Repoint the ten call sites; rename `USE_S3_STORAGE`; swap `STORAGES["default"]` | Yes |
| 7 | Rewrite the tests (§8). Full suite green | Yes |
| 8 | Update `core/checks.py` W001 hint; add the "storage not configured in production" check | Yes |
| 9 | **Point of no return:** flip `.env` to Azure; drop the R2 bucket; purge the four file-owning tables (§10) | No |
| 10 | Delete `utils/s3_storage/`; drop `boto3` and `django-storages[s3]` from `pyproject.toml` | Yes (revert) |
| 11 | Rewrite `docs/architecture/s3-storage-service.md` → `azure-blob-storage-service.md` with an Azure operation-class table; update `.env.example` §1.7/§2.8 and `server-requirements.md` | Yes |

Steps 1–8 are additive and can land on `feat-azure-blob-storage` without disturbing anyone.
Step 9 is the only irreversible one.

---

## 10. Discarding the existing data

You have said the R2 data is disposable. Making that explicit so it is a decision rather than
an accident:

Rows in `KBFiles`, `AgentCallRecording`, `CatalogItemMedia` and `ContactImportFile` store an
object key in `file`. After the switch every one of those keys points at a blob that does not
exist in Azure. Leaving them creates rows that 404 on download and that
`find_orphaned_objects` cannot help with — it finds objects with no row, not rows with no
object.

Purge them as part of step 9:

```python
# manage.py shell --settings=config.settings.local
from core.models.knowledge_base import KBFiles
from core.models.agent_call_recording import AgentCallRecording
from core.models.catalog_item_media import CatalogItemMedia
from core.models.contact_import import ContactImportFile

for model in (KBFiles, AgentCallRecording, CatalogItemMedia, ContactImportFile):
    print(model.__name__, model.all_objects.count())
    # all_objects: soft-deleted rows own keys too. Loop, don't bulk-delete —
    # BaseQuerySet.delete() refuses bulk deletes and each delete() tries its blob.
    for row in model.all_objects.all():
        row.file = ""            # blob is already gone; skip the pointless delete call
        row.save(update_fields=["file"])
        row.delete()
```

Run this **after** `.env` points at Azure, so a stray delete cannot reach into R2. Then delete
the R2 bucket `voicebot-local2-bucket` and revoke its access key from the Cloudflare dashboard
— the key currently sits in `.env` in plaintext and outlives the migration otherwise.

Knowledge bases will need re-ingesting through the ETL afterwards; that is a re-upload, not a
recovery.

---

## 11. Open questions

1. **Container-per-tenant, or one container with tenant prefixes?** The plan assumes one
   container, matching today's single bucket, because `is_object_key_owned_by_tenant` already
   enforces the tenant boundary in code and the sweep/audit paths iterate a prefix. Azure makes
   per-container SAS scoping easy, which is a genuinely stronger boundary — but it also means
   container provisioning on tenant creation and a rewrite of `iter_objects`. Recommend
   deferring; note it as the natural hardening step if tenant isolation is ever audited.
2. **Storage account region and redundancy tier.** LRS is assumed. Not a code question, but it
   sets the egress and storage numbers in §4.
3. **Confirm the frontend owns the `x-ms-blob-type` change** (§3) and can ship it before step 9.
   This is the only cross-team dependency in the plan.

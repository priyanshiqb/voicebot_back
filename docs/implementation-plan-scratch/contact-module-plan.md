# Contact Module — Implementation Plan (v3)

Living plan. v1 was architecturally sound but had five defects that crash or corrupt data,
one inherited cross-tenant security hole, and several enterprise gaps. v2 resolved those.
v3 locks the four open decisions and **expands scope to remediate the pre-existing
cross-tenant upload hole in Knowledge Base and Call Recordings** (§21).

**Status:** design complete, decisions locked, ready to build.

---

## 1. Scope

Deliver a tenant-scoped `Contact` entity with full CRUD, export, and phone lookup, plus a
resumable, reviewable, asynchronous bulk-import pipeline with conflict resolution — and fix
the existing presigned-upload authorization bug across the codebase.

**In scope (P0):** contact model + CRUD + export + lookup; E.164 normalization; two-step S3
upload; async parse → review → resolve → async commit; conflict detection (existing,
in-file duplicate, archived); skip/update/create resolution; TTL cleanup; consent & DNC
fields; `contact` FK on `AgentCallLog` + `BookingParticipant`; **KB/recording upload
remediation**; admin; docs; tests.

**In scope (P1):** column-mapping UI support, tag facet endpoint.

**Out of scope:** DNC enforcement at dial time (this module *records* consent; the voice
runtime must *honour* it — §14), merge/dedupe of pre-existing contacts, custom fields, xlsx
import (CSV only; xlsx is export-only).

---

## 2. Decisions (locked)

| # | Decision | Rationale |
|---|---|---|
| D1 | Store both `phone_number` (raw) and `phone_e164` (normalized). All uniqueness, conflict detection, and lookup use `phone_e164`. | Otherwise `+1 (555) 123-4567` and `+15551234567` are two contacts and conflict detection is decorative. |
| D2 | `request-upload-url` creates the `ContactImportFile` server-side and returns `file_id`; `confirm-upload` accepts **`file_id`, never `object_key`**. | Closes the hole described in §21. The client never names an S3 key, so it cannot name someone else's. |
| D3 | Parse and commit run as Celery tasks; endpoints return `202` + status for polling. | A 50k-row parse in a request thread is a worker timeout → 502. Celery already wired ([core/tasks.py](../../core/tasks.py), `CELERY_BEAT_SCHEDULE` at [base.py:434](../../config/settings/base.py#L434)). |
| D4 | Row `status` is **always server-derived**. Client sends `data` edits and a `resolution` verb. | A client-writable status lets a row be "resolved" without fixing the duplicate → `IntegrityError` at commit. |
| D5 | Conflicts **re-validate inside the commit transaction**. | Parse and commit are separated by minutes to hours; another import or API create can land in between. |
| D6 | On commit the **session survives as an immutable audit record**; only rows and the S3 blob are purged. | "Who imported these 4,000 contacts, from what file, and what was skipped?" is a procurement question. |
| D7 | Flat route for row PATCH (`/contact-import-rows/{id}/`). | `drf-nested-routers` is not a dependency; every url module is a flat `DefaultRouter` ([custom_voice.py](../../core/urls/custom_voice.py)). |
| D8 | Consent + DNC ship in the **initial migration**. | Outbound voice product. Backfilling consent onto a populated table has no correct default. |
| **D9** | **Re-import updates existing contacts — `UPDATE` is supported, with three safety rails (§9).** | See "Recommendation" below. |
| **D10** | **Import sessions are tenant-wide, not private to the creator.** `created_by` is retained for attribution only. | Your call. Consequences handled in §12.3 — concurrent editing is now a live scenario, so the commit lock and row-edit guard carry more weight. |
| **D11** | **A phone region is mandatory.** `POST /contact-imports/` rejects with **400** if no region resolves. A number that still fails to normalize makes the row **`INVALID`**. | Your call: "it should be there; if it is not there mark as error." Enforced at both levels. |
| **D12** | **`contact` FK added to `AgentCallLog` and `BookingParticipant` now (P0).** | Your call. Contacts stop being an island. |
| **D13** | **KB + Call Recording upload flows are remediated as part of this work**, in two stages (§21). | Your call. Stage 1 is a non-breaking hotfix; stage 2 aligns them with D2. |
| D14 | CSV only for import. | Encoding + delimiter detection is already a large surface; xlsx doubles the failure modes for a format users can "Save As" out of. |

### Recommendation on D9 — yes, support UPDATE

Without it, the most common real flow — *re-uploading an updated contact list* — is
impossible: every existing contact becomes a conflict the user must hand-edit one at a time.
A 2,000-contact refresh would be 2,000 manual edits. That is not a shippable product.

But naive UPDATE is how bulk importers destroy customer data, so it ships with three rails:

1. **Session default is `SKIP`, never `UPDATE`.** Overwriting is always an explicit choice,
   per-session or per-row. No silent mutation.
2. **Blank cells never overwrite populated fields.** A CSV with an empty `company` column
   must not wipe every company value in the database. Only non-empty incoming values are
   applied. This is the single most common bulk-import data-loss bug.
3. **`consent_status`, `do_not_call`, and `opted_out_at` are never writable by import.**
   Someone re-uploading a stale list must not flip `do_not_call` back to `False` on a person
   who opted out. Consent changes go through the API or admin, with attribution. This is a
   compliance landmine, not a preference.

An `UPDATE` also writes `updated_by` and leaves the prior values recoverable through the
session audit record (D6).

---

## 3. New dependencies

| Package | Why | Notes |
|---|---|---|
| `phonenumbers>=8.13` | E.164 parsing/validation (D1). No stdlib equivalent. | **New — add to `pyproject.toml`.** |
| `charset-normalizer>=3.4` | CSV encoding detection. | Present transitively via `requests`. **Declare explicitly** — do not rely on a transitive pin. |

`tablib`, `openpyxl`, `django-import-export`, `celery`, `boto3` are already declared.

---

## 4. Settings (`config/settings/base.py`)

```python
# Contacts & Contact Import
CONTACT_DEFAULT_PHONE_REGION      = env.str("CONTACT_DEFAULT_PHONE_REGION", default="")
CONTACT_IMPORT_MAX_ROWS           = env.int("CONTACT_IMPORT_MAX_ROWS", default=50_000)
CONTACT_IMPORT_MAX_FILE_BYTES     = env.int("CONTACT_IMPORT_MAX_FILE_BYTES", default=10 * 1024 * 1024)
CONTACT_IMPORT_SESSION_TTL_HOURS  = env.int("CONTACT_IMPORT_SESSION_TTL_HOURS", default=24)
CONTACT_IMPORT_MAX_OPEN_SESSIONS  = env.int("CONTACT_IMPORT_MAX_OPEN_SESSIONS", default=3)
CONTACT_IMPORT_COMMIT_BATCH_SIZE  = env.int("CONTACT_IMPORT_COMMIT_BATCH_SIZE", default=1000)
CONTACT_MAX_TAGS_PER_CONTACT      = env.int("CONTACT_MAX_TAGS_PER_CONTACT", default=25)
```

Per **D11**, `CONTACT_DEFAULT_PHONE_REGION` should be set to your primary market (ISO-3166
alpha-2, e.g. `IN`, `US`) in every deployed environment. Left blank, every import must pass
`default_region` explicitly or it is rejected with a 400. Document this in the deployment
checklist alongside the other required env vars.

Add to `CELERY_BEAT_SCHEDULE`:

```python
"contacts-cleanup-expired-imports": {
    "task": "core.tasks.cleanup_expired_contact_imports",
    "schedule": 600.0,  # every 10 minutes
},
```

---

## 5. Data model

### 5.1 `core/models/contact.py` — `Contact(TenantBaseModel)`

Tier 2 (fully audited + soft delete).

| Field | Type | Notes |
|---|---|---|
| `first_name` | `CharField(120, blank=True)` | |
| `last_name` | `CharField(120, blank=True)` | |
| `phone_number` | `CharField(32)` | Raw as supplied — display only. |
| `phone_e164` | `CharField(20, db_index=True)` | Normalized. **Identity key.** Never client-writable. |
| `phone_region` | `CharField(2, blank=True)` | ISO-3166 alpha-2 resolved during normalization. |
| `email_address` | `EmailField(254, blank=True)` | |
| `company` | `CharField(200, blank=True)` | |
| `tags` | `ArrayField(CharField(50), default=list, blank=True)` | GIN index; count clamped in serializer. |
| `notes` | `TextField(blank=True)` | |
| `source` | `CharField(choices, default=MANUAL)` | `MANUAL / IMPORT / API / CALL`. |
| `consent_status` | `CharField(choices, default=UNKNOWN)` | `UNKNOWN / GRANTED / DENIED / WITHDRAWN`. **Not import-writable (D9).** |
| `consent_source` | `CharField(200, blank=True)` | |
| `consent_at` | `DateTimeField(null=True)` | |
| `do_not_call` | `BooleanField(default=False, db_index=True)` | **Not import-writable (D9).** |
| `opted_out_at` | `DateTimeField(null=True)` | **Not import-writable (D9).** |
| `last_contacted_at` | `DateTimeField(null=True)` | Written by the voice runtime. |

**Constraints & indexes**

```python
constraints = [
    models.UniqueConstraint(
        fields=["tenant", "phone_e164"],
        condition=models.Q(is_deleted=False),
        name="unique_active_contact_phone_tenant",
    ),
]
indexes = [
    models.Index(fields=["tenant", "phone_e164"]),
    models.Index(fields=["tenant", "email_address"]),
    GinIndex(fields=["tags"]),
]
```

Partial condition matches the house pattern ([agent.py:96](../../core/models/agent.py#L96),
[event_template.py:102](../../core/models/event_template.py#L102)) and means a soft-deleted
contact does **not** permanently burn its phone number.

**Required overrides**

- `save()` — derive `phone_e164` / `phone_region` before every write. Never trust a
  caller-supplied `phone_e164`.
- `restore()` — raise `ValidationError` if an active contact already holds the same
  `phone_e164`. Without it, restoring an archived contact whose number was re-imported
  raises a raw `IntegrityError` out of Django admin.

### 5.2 `core/models/contact_import.py`

**`ContactImportFile(TenantBaseModelCore)`**

| Field | Notes |
|---|---|
| `file` | `FileField(upload_to=...)` — mirrors [agent_call_recording.py](../../core/models/agent_call_recording.py). |
| `original_name`, `extension`, `file_size_in_bytes` | `original_name` sanitized (§12.4). |
| `upload_status` | `PENDING / CONFIRMED / FAILED`. Created `PENDING` at `request-upload-url`. |
| `created_by` | FK to user — attribution. |

**`ContactImportSession(TenantBaseModelCore)`**

| Field | Notes |
|---|---|
| `import_file` | FK → `ContactImportFile`, `PROTECT`. Guarded so one file cannot back two sessions. |
| `created_by` | FK to user. Attribution only — access is tenant-wide (D10). |
| `status` | §6.1. |
| `default_region` | `CharField(2)` — **required**, resolved at start (D11). |
| `conflict_strategy` | `SKIP / UPDATE / FAIL`. **Defaults to `SKIP`** (D9 rail 1). |
| `column_map` | `JSONField(default=dict)` — CSV header → model field. |
| `expires_at` | `DateTimeField(db_index=True)`. |
| `total_rows`, `valid_rows`, `conflict_rows`, `invalid_rows` | Written by the parse task. |
| `created_count`, `updated_count`, `skipped_count`, `failed_count` | Written by the commit task. |
| `error_message` | Populated on `FAILED`. |
| `committed_at`, `committed_by`, `discarded_at`, `discarded_by` | **`committed_by` matters under D10** — anyone in the tenant can commit, so record who did. |

**`ContactImportRow(TenantBaseModelCore)`**

| Field | Notes |
|---|---|
| `session` | FK, `CASCADE`, `db_index=True`. |
| `row_number` | 1-based line offset in the source CSV, for user-facing errors. |
| `raw_data` | `JSONField` — verbatim cells. Never mutated. |
| `parsed_data` | `JSONField` — cleaned/typed. Editable. This is what commits. |
| `phone_e164` | `CharField(20, blank=True, db_index=True)` — denormalized for the duplicate scan. |
| `status` | §6.2. Server-derived only (D4). |
| `resolution` | `CREATE / UPDATE / SKIP`, nullable. |
| `conflict_contact` | FK → `Contact`, nullable. |
| `updated_by` | **Added for D10** — shows which colleague last edited a row. |
| `error_message` | |

Index on `(session, status)` — the rows list filters on it on every page load.

### 5.3 Linkage (D12)

Add to `AgentCallLog` and `BookingParticipant`:

```python
contact = models.ForeignKey(
    "core.Contact", on_delete=models.SET_NULL,
    null=True, blank=True, related_name="...", db_index=True,
)
```

Resolved by `phone_e164` at write time; `SET_NULL` so deleting a contact never destroys call
or booking history. Backfill for existing rows is a separate data migration — **not** part of
the schema migration, so a slow backfill cannot block a deploy.

---

## 6. State machines

### 6.1 Session status

```
UPLOADED ──▶ PARSING ──▶ READY ──▶ COMMITTING ──▶ COMPLETED
                 │          │            │
                 ▼          ▼            ▼
              FAILED    DISCARDED   PARTIAL / FAILED
                 │          │
                 └──▶ EXPIRED ◀──────── (TTL sweep)
```

- Row mutations allowed **only** in `READY`.
- `commit` allowed **only** from `READY`, transitioning to `COMMITTING` under
  `select_for_update()` — the double-commit guard.
- `PARTIAL` = some rows committed, some failed. Session **and rows retained** for
  inspection, unlike `COMPLETED`.

### 6.2 Row status

```
PENDING              — parsed clean, no conflict, will be created
INVALID              — malformed (missing/unnormalizable phone, bad email)
CONFLICT_EXISTING    — phone matches an active Contact
CONFLICT_DUPLICATE   — phone appears more than once within this file
CONFLICT_ARCHIVED    — phone matches a soft-deleted Contact
RESOLVED             — re-validated clean, or has an explicit resolution
SKIPPED              — skipped by choice
COMMITTED            — terminal, succeeded
FAILED               — terminal, failed at commit (with reason)
```

v1's three states could not express `INVALID` — "row 47 has no phone" and "row 48 is a
duplicate" collapsed together — and had no terminal states, making a partial commit
unrecoverable.

---

## 7. API surface

### 7.1 Contacts — `/api/v1/core/contacts/`

| Method | Path | Notes |
|---|---|---|
| `GET` | `/` | `search_fields = ["first_name","last_name","phone_e164","email_address","company"]`; `filterset_fields = ["is_active","do_not_call","consent_status","source","tags"]`. |
| `POST` | `/` | |
| `GET` | `/{id}/` | |
| `PATCH` / `PUT` | `/{id}/` | |
| `DELETE` | `/{id}/` | Soft delete via `BaseViewSet.perform_destroy`. |
| `POST` | `/bulk_delete/` | Inherited. |
| `GET` | `/dropdown/` | Inherited. |
| `GET` | `/export/?format=csv\|xlsx` | Via `ImportExportMixin` — **with CSV-injection escaping** (§12.2). |
| `GET` | `/lookup/?phone=+15551234567` | Normalizes then exact-matches `phone_e164`. For inbound-caller resolution by the voice runtime. |
| `GET` | `/tags/` | Distinct tag facet with counts. P1. |

### 7.2 Imports — `/api/v1/core/contact-imports/`

| Method | Path | Returns | Notes |
|---|---|---|---|
| `GET` | `/template/` | `text/csv` | Header + example row + documented phone format. |
| `POST` | `/request-upload-url/` | `{file_id, upload_url, expires_in}` | Creates `ContactImportFile(PENDING)`. **Never returns an object key.** |
| `POST` | `/confirm-upload/` | `{file_id, ...}` | Body: `{file_id}` only. Verifies existence + size, flips to `CONFIRMED`. |
| `POST` | `/` | `202 {session_id, status}` | Body: `{file_id, default_region?, conflict_strategy?, column_map?}`. **400 if no region resolves (D11).** Enqueues parse. |
| `GET` | `/` | list | **All sessions in the tenant (D10)**, with `created_by` shown. |
| `GET` | `/{id}/` | session + counts | **Poll target.** Lets the UI show "4,812 rows, 37 conflicts" without paging rows. |
| `GET` | `/{id}/rows/` | paginated | `?status=CONFLICT_EXISTING`, `?search=`. |
| `POST` | `/{id}/bulk-resolve/` | counts | `{status, resolution}`. Resolving 500 conflicts one PATCH at a time is unusable. |
| `POST` | `/{id}/commit/` | `202` | `select_for_update` + status guard; records `committed_by`. |
| `POST` | `/{id}/discard/` | `200` | Purges rows + S3 blob; session → `DISCARDED`, records `discarded_by`. |

### 7.3 Import rows — `/api/v1/core/contact-import-rows/`

| Method | Path | Notes |
|---|---|---|
| `PATCH` | `/{row_id}/` | Body: `{parsed_data?, resolution?}`. **`status` not writable.** Server re-runs validation + conflict check and derives status. Rejected unless the parent session is `READY`. |

### 7.4 Throttling

Import start/commit get a scoped throttle following
[public_reschedule.py:21](../../core/views/public_reschedule.py#L21) — these are the expensive
endpoints.

---

## 8. Async pipeline (`core/tasks.py`)

### `parse_contact_import(session_id)`

1. Lock session, guard, → `PARSING`.
2. Stream from S3. Detect encoding (`utf-8-sig` → `utf-8` → `charset_normalizer`). Sniff the
   delimiter with `csv.Sniffer` (Excel writes `;` in many locales).
3. Resolve headers against `column_map`, then a built-in alias table (`"First Name" |
   "firstname" | "given_name"` → `first_name`; `"Phone" | "Mobile"` → `phone_number`).
   Unmapped required column → session `FAILED`, clear message, no rows written.
4. `FAILED` if the row count exceeds `CONTACT_IMPORT_MAX_ROWS`.
5. Per row: normalize phone using the session region → **`INVALID` on failure (D11)**;
   validate email; clamp tags; **drop any consent/DNC columns (D9 rail 3)**.
6. **In-file duplicate pass** — group by `phone_e164`; the first occurrence keeps its status,
   later ones become `CONFLICT_DUPLICATE`. *(v1 missed this: two identical phones in one CSV
   both passed review, then `bulk_create` raised `IntegrityError` and killed the commit.)*
7. **Existing-contact pass** — chunked `phone_e164__in` against active contacts →
   `CONFLICT_EXISTING` + `conflict_contact`; a second pass against
   `all_objects.filter(is_deleted=True)` → `CONFLICT_ARCHIVED`.
8. `bulk_create` rows in batches; write counts; → `READY`.

### `commit_contact_import(session_id)` — §9

### `cleanup_expired_contact_imports()`

Beat task, every 10 min. For sessions past `expires_at` in a non-terminal state:

- Delete rows and the S3 object; mark `EXPIRED`; **keep the session row** (D6).
- Use **`all_objects`** for the bulk row delete. `BaseQuerySet.delete()` raises
  `Exception("Bulk delete is not allowed. Use soft_delete() instead.")`
  ([base_model_core.py:6](../../utils/models/base_model_core.py#L6)) — v1's
  `clean_expired_sessions()` would have thrown on its first run.
- Delete the S3 object in `transaction.on_commit()` and **tolerate 404**. Inside the
  transaction, a failed S3 call rolls back the DB cleanup and the beat job wedges on the same
  poison session every 10 minutes forever.

---

## 9. Commit algorithm

```
lock session FOR UPDATE
assert status == READY                     # rejects double-click and concurrent colleagues (D10)
set status = COMMITTING, committed_by = user; save; release lock

for batch in rows.filter(status in {PENDING, RESOLVED}).order_by(row_number).iterator(chunk_size=1000):
    with transaction.atomic():             # savepoint per batch, not per file
        phones = [r.phone_e164 for r in batch]

        # RE-VALIDATE (D5) — parse-time flags are stale by now
        live = {c.phone_e164: c for c in Contact.objects.filter(tenant=t, phone_e164__in=phones)}

        to_create, to_update = [], []
        for row in batch:
            existing = live.get(row.phone_e164)
            if existing is None:
                to_create.append(build_contact(row))       # tenant/created_by set explicitly
            elif row.resolution == UPDATE:
                to_update.append(merge(existing, row))     # see rails below
            else:
                row.status = SKIPPED

        try:
            Contact.objects.bulk_create(to_create, batch_size=1000)
            Contact.objects.bulk_update(to_update, MUTABLE_FIELDS, batch_size=1000)
        except IntegrityError:
            retry_individually(batch)      # concurrent writer won; mark each COMMITTED or FAILED
    mark batch COMMITTED
```

`merge(existing, row)` implements the D9 rails:

- Applies only **non-empty** incoming values (rail 2).
- `MUTABLE_FIELDS` **excludes** `consent_status`, `consent_source`, `consent_at`,
  `do_not_call`, `opted_out_at`, `phone_e164`, `tenant`, and all audit fields (rail 3).
- Sets `updated_by` and `source` appropriately.

What matters here:

- **Batch savepoints, not one transaction.** One `atomic()` around 50k rows means a single
  bad row discards the entire import; per-row saves means 50k round trips.
- **`bulk_create`/`bulk_update` bypass `save()` and `perform_create`.** `tenant`,
  `created_by`, `updated_by`, and `phone_e164` must be set explicitly — the
  `BaseViewSet.perform_create` audit injection is not in play.
- Terminal: `COMPLETED` if `failed == 0`, else `PARTIAL`.
- On `COMPLETED`: purge rows + S3 blob, keep the session as audit. On `PARTIAL`: **retain
  everything** so the user can see which rows failed and why.
- Idempotent on retry via the `COMMITTING` guard plus per-row terminal statuses.

---

## 10. Contact write path

1. `phone_number` required, non-blank.
2. Resolve region: explicit arg → session `default_region` → `CONTACT_DEFAULT_PHONE_REGION`.
   **If none resolves, error (D11)** — 400 at the API, `INVALID` at the row level.
3. Parse with `phonenumbers.parse(raw, region)`; reject if `not is_valid_number()`.
4. Write `phone_e164 = format_number(E164)` and `phone_region`.
5. Serializer marks `phone_e164`, `phone_region`, `tenant`, and audit fields read-only.
6. Uniqueness surfaces as a clean 400 (`"A contact with this phone number already exists."`),
   not a 500 — catch `IntegrityError` in the serializer/viewset.

---

## 11. CSV handling rules

| Concern | Rule |
|---|---|
| Encoding | `utf-8-sig` (Excel BOM) → `utf-8` → `charset_normalizer`. Undecodable → session `FAILED` with a clear message. |
| Delimiter | `csv.Sniffer` over the first 8 KB, fallback `,`. Handles European Excel's `;`. |
| Quoting | Standard `csv` module — handles embedded newlines and quoted commas. Never hand-split on `,`. |
| Tags column | Pipe-delimited (`sales\|vip`), documented in the template. Comma collides with the delimiter. |
| Blank rows | Skipped silently, not counted. |
| Header | Case-insensitive, trimmed, matched via `column_map` then alias table. |
| BOM / zero-width chars | Stripped from every cell. |
| Consent/DNC columns | **Ignored if present** (D9 rail 3), with a session-level notice. |

---

## 12. Security controls (contact module)

### 12.1 Presigned upload

Per D2, the object key never leaves the server. Full rationale and the remediation of the
existing flows is in §21.

### 12.2 CSV injection

Any exported cell beginning with `= + - @ TAB CR` executes on open in Excel. `first_name`,
`company`, and `notes` are user-supplied and round-trip through export. Prefix with `'` in the
export resource. Applies to `/contacts/export/` and any error-report download.

### 12.3 Authorization under tenant-wide sessions (D10)

Sessions and rows are scoped by tenant only — `TenantBaseViewSet.get_queryset()`
([tenant_base_viewset.py](../../tenant/common/views/tenant_base_viewset.py)) already does exactly
this, so no owner filter is added. The consequences must be handled explicitly:

- **Concurrent editing is now a real scenario.** Two colleagues can review the same session
  simultaneously. The `READY`-only guard on row PATCH plus the `select_for_update` commit lock
  are what keep this safe — they are load-bearing, not belt-and-braces.
- **Attribution everywhere.** `created_by` on the session, `updated_by` on each row,
  `committed_by` / `discarded_by` on the session. Without private ownership, "who committed
  this" is the only way to answer for a bad import.
- **Commit and discard are destructive and tenant-wide.** A colleague can discard your
  half-reviewed session. The API returns a clear conflict error if the session is not in the
  state the caller expected, and the UI should show the owner and last-editor prominently.
- Per-tenant cap of `CONTACT_IMPORT_MAX_OPEN_SESSIONS` concurrent open sessions.
- Row-count and file-size ceilings enforced at both `confirm-upload` and parse.

### 12.4 PII handling

- The uploaded CSV is raw PII in the bucket. Deleted on commit, discard, and expiry. Add an S3
  lifecycle rule on the `contact_imports/` prefix as a backstop.
- `original_name` is sanitized (strip path separators and control chars, clamp length) before
  use in any key or `Content-Disposition` header.
- Soft delete is not erasure. Provide a documented hard-delete path for GDPR right-to-erasure
  (management command, admin-only).

---

## 13. Admin (`core/admin/contact_admin.py`)

`ContactAdmin(TenantBaseAdmin)` — list display with phone/email/company/consent/DNC, filters
on `consent_status` / `do_not_call` / `source`, search on name/phone/email, `phone_e164`
read-only.

Import staging models registered read-only (`has_add_permission = False`,
`has_change_permission = False`) — machine-managed; hand-editing corrupts the state machine.

---

## 14. Integration points

- `contact` FK on `AgentCallLog` and `BookingParticipant` (D12, §5.3).
- **DNC enforcement belongs to the caller, not here.** This module records `do_not_call` and
  `consent_status`; the voice runtime must query and honour them before dialling. Flagged
  explicitly so it doesn't fall between the two services.

---

## 15. Tests (`core/tests/contacts/`)

Run with `--settings=config.settings.test_postgres` — the default SQLite test DB cannot
handle `ArrayField`/GIN.

**Models & normalization**
- `+1 (555) 123-4567`, `+15551234567`, `555-123-4567` + region → same `phone_e164`.
- **No region resolvable → `POST /contact-imports/` returns 400 (D11).**
- **Unnormalizable number with a region → row `INVALID`, not a crash.**
- Partial unique constraint: duplicate active phone blocked; soft-delete then re-create the
  same phone succeeds; `restore()` into a collision raises `ValidationError`.

**Import pipeline**
- Parse: BOM'd UTF-8, `;`-delimited, quoted-with-newlines, mixed-alias headers.
- Conflicts: existing / in-file duplicate / archived, all in one file.
- Row cap and file-size cap rejection.
- Row PATCH cannot set `status`; editing a conflicted phone to a free number flips it to
  `RESOLVED`; editing to another taken number keeps it conflicted.
- Bulk-resolve applies only to the filtered set.

**UPDATE rails (D9)**
- **Blank incoming cell does not wipe a populated field.**
- **A CSV `do_not_call=false` column does not clear an existing opt-out.**
- Default strategy is `SKIP` — an import with no explicit choice creates nothing over an
  existing contact.

**Commit**
- **Double-commit:** two concurrent commits → one succeeds, one rejected, no duplicates.
- **TOCTOU:** insert a colliding contact between parse and commit → row lands `SKIPPED` or
  `FAILED`, never a duplicate, never a 500.
- Partial failure → `PARTIAL`, rows retained, counts correct.
- `UPDATE` mutates the existing contact and does not create a second one.
- Post-`COMPLETED`: rows purged, S3 object deleted, session retained with counts.

**Tenant-wide access (D10)**
- User B in the same tenant **can** list, PATCH, and commit user A's session.
- User in a different tenant **cannot** (404).
- `committed_by` records the actual committer, not the creator.

**Security**
- Tenant A cannot `confirm-upload` tenant B's `file_id` (404, not 200).
- Export escapes `=cmd()` in a contact name.

**Cleanup**
- Expired session purged, session row retained as `EXPIRED`.
- S3 delete failure does not roll back DB cleanup and does not wedge the beat job.

---

## 16. Documentation

- `docs/apiDocs/core/contact.md` — CRUD, filters, export, lookup.
- `docs/apiDocs/core/contact_import.md` — full flow, state machines, conflict matrix, every
  error code, and the D9 update rails.
- `docs/flows/07-contact-import.md` — end-to-end walkthrough (matches `docs/flows/`).
- Add all three to [docs/_sidebar.md](../_sidebar.md) under **🌐 Core APIs**.
- Update `docs/scheduling-deployment-checklist.md` (or a new deployment note) with
  `CONTACT_DEFAULT_PHONE_REGION` as a required env var per D11.
- §21 stage 1 requires a changelog note; stage 2 requires updating
  `docs/apiDocs/core/knowledge_base.md` and `agent_call_recording.md`.

---

## 17. Build order

| Phase | Contents | Gate |
|---|---|---|
| 0 | **§21 stage 1 hotfix** — prefix validation on KB + recording confirm-upload | Cross-tenant tests green; ships independently |
| 1 | `phonenumbers` dep, `Contact` model + normalization + constraints, migration | Normalization + constraint tests green |
| 2 | Contact CRUD, serializer, viewset, export w/ escaping, lookup, admin, docs | CRUD tests green |
| 3 | Import models + migration, S3 upload endpoints (D2), template | Security tests green |
| 4 | Parse task + session/row read endpoints | Parse tests green |
| 5 | Row PATCH, bulk-resolve | Resolution + D9 rail tests green |
| 6 | Commit task + discard + cleanup beat task | Concurrency tests green |
| 7 | D12 linkage FKs + backfill data migration | Linkage tests green |
| 8 | **§21 stage 2** — migrate KB + recordings to the `file_id` pattern | Deprecation window observed |
| 9 | P1: tag facet, column-mapping UI support | |

Phase 0 is independent and should ship first — it is a live vulnerability fix. Phases 1–2
deliver a usable contacts module on their own.

---

## 18. Verification plan

1. CRUD round-trip; confirm `phone_e164` derived and read-only.
2. Create a contact, soft-delete it, re-create the same phone — must succeed.
3. Start an import with no region configured and none supplied — **must 400 (D11)**.
4. Upload a CSV via presigned PUT; confirm with `file_id`.
5. **Attempt `confirm-upload` with another tenant's `file_id` — must 404.**
6. Import a file mixing: new rows, an existing phone, the same phone twice in-file, a row with
   no phone, a malformed number, and an archived-contact phone. Verify all six statuses.
7. **Set strategy `UPDATE` with a CSV whose `company` column is blank and whose `do_not_call`
   column is `false` — confirm existing companies survive and opt-outs are not cleared.**
8. Resolve one conflict by editing; bulk-skip the rest.
9. **As a second user in the same tenant**, open the same session, edit a row, and commit —
   confirm it works and `committed_by` is the second user (D10).
10. **Before committing, create a colliding contact via the API** — commit and confirm no
    duplicate, no 500, accurate `skipped`/`failed` counts.
11. Double-click commit — second request rejected.
12. Confirm rows + S3 object purged, session retained with counts.
13. Let a session expire; confirm the beat sweep purges it and marks it `EXPIRED`.
14. Export contacts; open in Excel; confirm a `=HYPERLINK(...)` name is inert.
15. Place a call log against a known phone; confirm the `contact` FK resolves (D12).

---

## 19. Risk register

| Risk | Mitigation |
|---|---|
| `phonenumbers` rejects valid numbers in some regions | Per-import `default_region`; `INVALID` rows are reviewable and editable, not silently dropped |
| Mandatory region blocks a legitimate all-E.164 import | E.164 numbers normalize regardless of region; the region is only a fallback parser hint. Setting the env var once removes all friction |
| Celery unavailable → sessions stall in `PARSING` | Beat sweep marks sessions stuck > TTL as `FAILED` with a retry path |
| 50k-row commit holds DB connections | Batched savepoints, `iterator(chunk_size=)`, no long-lived transaction |
| Staging tables grow unbounded | Row cap + open-session cap + 10-min TTL sweep |
| **Tenant-wide sessions → a colleague discards your work (D10)** | State guards + full attribution + prominent owner display in the UI. Accepted trade-off of the tenant-wide decision |
| **`UPDATE` overwrites good data** | Three rails in D9 + explicit tests |
| Cross-tenant hole persists in KB/recordings | §21 stage 1 ships in phase 0 |
| ArrayField ties us to Postgres | Already true repo-wide; tests pinned to `test_postgres` |

---

## 20. Appendix — evolution

**v1 → v2:** E.164 normalization; `all_objects` + `on_commit` S3 delete in cleanup; in-file
duplicate detection; commit-time re-validation; double-commit guard; async parse/commit;
`file_id` instead of client `object_key`; server-derived row status; 9 row statuses; session
retained as audit; skip/update/create + bulk-resolve; row/file/session caps; consent + DNC
fields; export, lookup, and session-summary endpoints; flat row route; CSV encoding rules.

**v2 → v3:** D9–D13 locked. `UPDATE` confirmed with three safety rails. Sessions made
tenant-wide with attribution and concurrency guards. Region made mandatory with errors at both
levels. Linkage FKs promoted to P0. **KB + call-recording remediation added as §21 and phase
0.**

---

## 21. Remediation — cross-tenant confirm-upload (KB + Call Recordings)

**Added at your request. This is a pre-existing live vulnerability, not new work.**

### 21.1 The defect

Both flows are **stateless** between the two steps:

| | Knowledge Base | Call Recordings |
|---|---|---|
| Key built at | [knowledge_base.py:192](../../core/views/knowledge_base.py#L192) | [agent_call_log.py:221](../../core/views/agent_call_log.py#L221) |
| Key returned to client | yes | yes |
| Confirm accepts client key | [knowledge_base.py:232](../../core/views/knowledge_base.py#L232) | [agent_call_log.py:261](../../core/views/agent_call_log.py#L261) |
| Ownership check | **none** | **none** |
| Record attached to | caller's tenant | caller's / agent's tenant |

`confirm_upload` verifies only that the object **exists**
([knowledge_base.py:243](../../core/views/knowledge_base.py#L243)) — never that it belongs to the
caller. Three consequences:

1. **Cross-tenant read.** Tenant A confirms tenant B's key and gets a `KBFiles` row *in tenant
   A* pointing at tenant B's object. `KBFilesSerializer` then hands back a presigned GET URL
   for it ([kb_files.py:53](../../core/serializers/kb_files.py#L53)). That is data exfiltration.
2. **Cross-tenant delete.** If the target object exceeds the size limit, `confirm_upload`
   calls `delete_blob_direct(object_key)` on it
   ([knowledge_base.py:251](../../core/views/knowledge_base.py#L251),
   [agent_call_log.py:280](../../core/views/agent_call_log.py#L280)) — destroying another
   tenant's file, with no read required.
3. **Key injection at request time.** `filename` is interpolated into the key unsanitized in
   both flows, and `validate_filename` only checks the extension
   ([kb_files.py:121](../../core/serializers/kb_files.py#L121)). A crafted
   `../../t-{victim}/kb_files/x.pdf` passes. Plain S3 treats `..` literally, but this stack
   targets **MinIO** (per the [S3StorageService](../../utils/s3_storage/service.py) docstring),
   where filesystem-backed deployments may normalize the path.

**Exploit prerequisite:** knowing a victim's object key. Keys are UUID-based, so not
brute-forceable — but they are *not secret*. They are returned to the client, embedded in
every presigned GET URL, and visible to the ETL service, in browser history, logs, and support
tickets. A former employee of tenant B who joins tenant A can replay one. Treat as **high
severity, moderate exploitability**.

### 21.2 Stage 1 — hotfix (non-breaking, ships in phase 0)

Keep the `object_key` contract; add authorization. In **both** `confirm_upload` methods,
immediately after resolving the tenant:

1. Resolve the tenant **first**, before any S3 call. Today the S3 verify-and-delete runs
   *before* tenant resolution in both views — so the destructive
   `delete_blob_direct` fires before anyone asks who the caller is. Reordering alone kills
   consequence 2.
2. Reject unless `object_key.startswith(f"t-{tenant_id}/")` with the **correct** prefix for
   that flow (`kb_files/`, `call_recordings/`). Return **404**, not 403 — do not confirm the
   existence of another tenant's key.
3. Reject keys containing `..`, `//`, backslashes, or control characters.
4. In `request_upload_url`, sanitize `filename` to a basename and strip path separators before
   interpolating it into the key.

Critically, the prefix check must use the **resolved** tenant, not
`request.user.tenant_mapping`. The recordings flow resolves tenant from the `agent` field for
`VB_AGENT` users ([agent_call_log.py:205-218](../../core/views/agent_call_log.py#L205-L218)); a
naive fix using the user's mapping breaks the AI-team upload path.

This is roughly 15 lines per view, requires no frontend change, and closes all three
consequences.

### 21.3 Stage 2 — align with D2 (phase 8, breaking)

Move both flows to the stateful pattern the contact module uses:

- `request-upload-url` creates a `PENDING` record and returns `{file_id, upload_url}`.
- `confirm-upload` accepts `{file_id}`; the key is read from the tenant-scoped DB row and never
  crosses the wire.
- Transition window: accept **either** `file_id` or `object_key` (the latter still
  prefix-validated per stage 1), log usage of the deprecated field, and drop `object_key` once
  frontend telemetry shows zero calls.

`AgentCallRecording` and `KBFiles` both already carry the fields a pending record needs, so
this is a status field plus a migration.

### 21.4 Tests

Add to `core/tests/views/test_knowledge_base.py` and `test_agent_call_log.py`:

- Tenant A confirming tenant B's key → **404**, no record created.
- Tenant A confirming an oversized key belonging to tenant B → 404 and **the object still
  exists** (regression test for consequence 2).
- `filename = "../../etc/x.pdf"` at request time → sanitized or rejected.
- `VB_AGENT` upload path still works end-to-end (regression for the tenant-resolution
  ordering).

### 21.5 Disclosure note

Anything already uploaded under the old flow may have been confirmed by another tenant. After
stage 1, run an audit query for `KBFiles` / `AgentCallRecording` rows whose `file.name` prefix
does not match their own `tenant_id` — that set is the actual blast radius. If it is non-empty,
it is an incident, not a bug fix.

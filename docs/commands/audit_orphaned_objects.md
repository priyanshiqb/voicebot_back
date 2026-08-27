# Orphaned Object Audit

**Command Name:** `audit_orphaned_objects`
**Path:** `core/management/commands/audit_orphaned_objects.py`
**Destructive:** only with `--delete`. Read-only otherwise.

## Purpose

Finds objects sitting in the storage container that **no database row references**.

This is the only tool that walks the container. Everything else walks the database:

| | Starts from | Finds |
|---|---|---|
| `cleanup_pending_uploads` | rows | abandoned uploads, deletes their objects |
| **`audit_orphaned_objects`** | **objects** | **objects with no row at all** |

An object with no row is invisible to every other command, because there is nothing left
pointing at it. It gets there by:

- a **hard delete** from before the models removed their own blobs (`AgentCallRecording`
  and `CatalogItemMedia` had no `delete()` override until recently — deleting a row left
  its bytes behind permanently)
- a **legacy upload path** that wrote to storage without reserving a row
- a request that **uploaded successfully and then failed** before its row was saved
- manual work in a shell or the storage console

Nothing in the product will ever show you these. They accumulate silently and cost money.

## Why not a storage lifecycle policy?

The usual answer to container growth is a lifecycle policy that expires a staging prefix.
It does not fit this container.

Object keys are `t-<tenant>/kb_files/<uuid>_name.pdf` — an object lands on its **final**
key at upload time; there is no staging area. A lifecycle policy filtering on that prefix
would expire confirmed, in-use files along with abandoned ones.

Azure Blob lifecycle rules match on a **literal prefix, not a glob** (and the prefix must
include the container name). Even after introducing a staging segment you would get
`t-<tenant>/_staging/…`, which needs one rule per tenant — and Azure caps a policy at 100
rules, so that ceiling is reached at 100 tenants. The alternative is a restructure to
`_staging/t-<tenant>/…` plus a server-side copy of every object on confirm: a key-layout
migration on a live container, to solve a problem `cleanup_pending_uploads` already
handles for the ordinary case.

So the abandoned-upload path is handled in application code, and this command covers the
gap that path structurally cannot see.

## Safety

Deleting here is judged from the **absence** of a row. That is the inverse of every other
cleanup command, and it means a wrong answer destroys a customer's file rather than
leaking disk. Three guards:

1. **Read-only unless you pass `--delete`.** Run it, read the list, then decide.
2. **Recent objects are skipped.** An object written moments ago may belong to a row that
   is about to be created. Anything newer than `PENDING_UPLOAD_TTL_HOURS` (default 24) is
   ignored.
3. **Undated objects are never touched.** If the backend does not report
   `last_modified`, the object cannot be proven old enough to be safe.

**Soft-deleted rows still own their files.** A soft delete is restorable, so its object is
counted as accounted-for and is never reported. (`core/tests/test_orphaned_object_audit.py`
pins this — it is the single most dangerous mistake this command could make.)

## Usage

### 1. Audit everything (read-only)

```bash
uv run python manage.py audit_orphaned_objects
```

*(Docker: `docker compose exec backend-dev python manage.py audit_orphaned_objects`)*

```
Acme Corp: 3 orphaned object(s), 12.4 MB.
    t-7bf26cf4-…/kb_files/a1b2-old.pdf  (4.1 MB, modified 2026-05-02 09:31:00+00:00)
    t-7bf26cf4-…/call_recordings/c3d4-call.mp3  (8.0 MB, modified 2026-04-18 14:02:11+00:00)
    t-7bf26cf4-…/catalog/9f1e/e5f6.jpg  (0.3 MB, modified 2026-06-01 08:15:43+00:00)
Globex: no orphaned objects.

3 orphaned object(s) holding 12.4 MB. Nothing was deleted — re-run with --delete once
you have read the list above.
```

### 2. One tenant, full listing

```bash
uv run python manage.py audit_orphaned_objects --tenant-id 7bf26cf4-… --verbose
```

Without `--verbose` only the first ten per tenant are listed.

### 3. Widen or narrow the safety window

```bash
uv run python manage.py audit_orphaned_objects --older-than-hours 168   # 7 days
```

Defaults to `PENDING_UPLOAD_TTL_HOURS`. Raise it when you want to be certain nothing
in flight is included; lower it only when investigating.

### 4. Actually delete

```bash
uv run python manage.py audit_orphaned_objects --delete
uv run python manage.py audit_orphaned_objects --tenant-id 7bf26cf4-… --delete
```

Deletes only what the same invocation would have reported. **Read the report first.**

## Arguments

| Flag | Default | Meaning |
|---|---|---|
| `--tenant-id` | every tenant | Restrict the audit to one tenant |
| `--older-than-hours` | `PENDING_UPLOAD_TTL_HOURS` (24) | Ignore objects newer than this |
| `--delete` | off | Delete what is found. Without it, nothing is removed |
| `--verbose` | off | List every orphan instead of the first ten per tenant |

## Scheduling

**Deliberately not scheduled.** Unlike `cleanup_pending_uploads`, this deletes based on
data being missing, so a human reads the output before anything is removed.

Run it periodically as an **audit** — quarterly, or when the storage bill moves
unexpectedly — and treat a non-empty result as something to understand before deleting.
A sudden jump means a code path is writing objects without rows, and deleting the
evidence hides the bug that produced it.

## Related

- [Abandoned Upload Sweep](cleanup_pending_uploads.md) — the scheduled, row-driven counterpart
- [Knowledge Base Files Cleanup](cleanup_orphaned_kb_files.md) — KB files never linked to a Knowledge Base
- `audit_upload_key_ownership` — cross-tenant key check; a non-empty result is a security incident

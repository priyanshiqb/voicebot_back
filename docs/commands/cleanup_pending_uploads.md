# Abandoned Upload Sweep

**Command Name:** `cleanup_pending_uploads`
**Path:** `core/management/commands/cleanup_pending_uploads.py`
**Scheduled:** yes — daily, via Celery Beat (`uploads-cleanup-pending`)
**Destructive:** yes. Use `--dry-run` to preview.

## Purpose

Deletes upload records still `PENDING` past a cutoff — presigned uploads the client
abandoned — **and the objects they left in the container**.

Uploads are two-step: the server reserves a `PENDING` row so the object key can stay
server-side, hands out a presigned ticket, and waits for a confirm. When the confirm never
arrives, that row stays `PENDING` forever.

The row itself is harmless; the API only ever serves `CONFIRMED` records. **The container is
the problem.** The common abandonment is not "the client never uploaded" — it is *the
upload succeeded and the confirm request never arrived*: a closed tab, a dropped
connection. Those bytes are real, and nothing in the product will ever show them to you.

## What it sweeps

| Model | Flow |
|---|---|
| `KBFiles` | Knowledge Base documents |
| `AgentCallRecording` | Call recording MP3s |
| `CatalogItemMedia` | Catalog images, video, documents |

Deliberately excluded:

- **`ContactImportFile`** — owned by an import session whose own TTL sweep purges it. Two
  sweeps racing on one row is worse than one sweep.
- **`FAILED` rows** — terminal with nothing behind them (confirm found no object, or
  presigning itself failed). They cost a row and no storage, and deleting them would only
  hide the diagnosis.

Deletion goes row by row so each model's `delete()` removes its stored object. A bulk
queryset delete would skip that, and is refused outright by `BaseQuerySet.delete()`.

> **Adding a new upload flow?** Add its model to `SWEPT_MODELS` in
> `core/services/uploads.py`. A flow left out leaks bytes silently — there is no error,
> just a container that grows. `core/tests/test_pending_upload_sweep.py` fails if you forget.

## Usage

### 1. Preview

```bash
uv run python manage.py cleanup_pending_uploads --dry-run
```

*(Docker: `docker compose exec backend-dev python manage.py cleanup_pending_uploads --dry-run`)*

```
Knowledge Base file: nothing to sweep.
Call recording: would delete 2 pending record(s).
    id=c3d4e5f6-…
    id=a1b2c3d4-…
Catalog media: nothing to sweep.
```

### 2. Run it

```bash
uv run python manage.py cleanup_pending_uploads
```

### 3. Override the safety window

```bash
uv run python manage.py cleanup_pending_uploads --older-than-hours 1
```

Defaults to `PENDING_UPLOAD_TTL_HOURS` (24). **Must stay above the upload ticket
lifetime** (`UPLOAD_URL_TTL_MINUTES`, default 15) or the sweep deletes uploads still in
flight — `manage.py check` refuses a configuration where it does not (`core.E003`).

### 4. One tenant

```bash
uv run python manage.py cleanup_pending_uploads --tenant-id 7bf26cf4-…
```

## Arguments

| Flag | Default | Meaning |
|---|---|---|
| `--older-than-hours` | `PENDING_UPLOAD_TTL_HOURS` (24) | Only sweep records older than this |
| `--dry-run` | off | Report what would be deleted without deleting it |
| `--tenant-id` | every tenant | Restrict to one tenant |

## Scheduling

Already scheduled — `CELERY_BEAT_SCHEDULE["uploads-cleanup-pending"]` runs
`core.tasks.cleanup_pending_uploads` daily. The command and the beat task share one
implementation (`core.services.uploads.sweep_pending_uploads`), so running it by hand and
letting the scheduler run it cannot drift apart.

Requires Celery Beat to be running:

```bash
celery -A config beat -l info
```

## Related

- [Orphaned Object Audit](audit_orphaned_objects.md) — the object-driven counterpart, for bytes with no row at all
- [Knowledge Base Files Cleanup](cleanup_orphaned_kb_files.md) — KB files never linked to a Knowledge Base

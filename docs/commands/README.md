# Maintenance Commands

Management commands for storage hygiene and upload integrity. All are run with
`uv run python manage.py <name>`, or inside Docker with
`docker compose exec backend-dev python manage.py <name>`.

## Which one do I want?

Four commands sound similar and do different things. Pick by **what you are starting
from**:

| I want to… | Command | Destructive? |
|---|---|---|
| Collect uploads a client abandoned | [`cleanup_pending_uploads`](cleanup_pending_uploads.md) | Yes — `--dry-run` to preview. **Already scheduled daily** |
| Find bucket objects no database row knows about | [`audit_orphaned_objects`](audit_orphaned_objects.md) | No, unless `--delete` |
| Delete KB files never attached to a Knowledge Base | [`cleanup_orphaned_kb_files`](cleanup_orphaned_kb_files.md) | Yes — `--dry-run` to preview |
| Check whether any row references another tenant's object | `audit_upload_key_ownership` | **No — read-only** |

## The two that are easily confused

`cleanup_pending_uploads` and `audit_orphaned_objects` both delete unused objects, but
they approach from opposite ends and neither can see what the other sees:

```
cleanup_pending_uploads   starts from ROWS    → PENDING rows past their TTL, and their objects
audit_orphaned_objects    starts from OBJECTS → objects with no row pointing at them at all
```

A row-driven sweep can never find an object whose row was hard-deleted, because there is
nothing left to walk from. That is the entire reason the second command exists.

Their risk profiles are inverted too, which is why only one is scheduled:

- `cleanup_pending_uploads` deletes from the **presence** of a row it can inspect. Safe to
  automate.
- `audit_orphaned_objects` deletes from the **absence** of a row. A wrong query destroys
  customer files, so it reports by default and a human decides.

## Security

`audit_upload_key_ownership` is not hygiene — it is a blast-radius check for the
cross-tenant `confirm-upload` hole. It reports rows whose stored object key sits outside
their own tenant's prefix.

```bash
uv run python manage.py audit_upload_key_ownership --verbose
```

**A non-empty result is a security incident with disclosure obligations, not a bug
report.** Each row means one tenant could read another tenant's file. Preserve the output,
identify the affected tenants, and follow your disclosure process before deleting
anything.

## Related settings

| Setting | Default | Affects |
|---|---|---|
| `PENDING_UPLOAD_TTL_HOURS` | 24 | Sweep cutoff, and the default audit window |
| `UPLOAD_URL_TTL_MINUTES` | 15 | Upload ticket lifetime. Must stay well below the sweep cutoff — `manage.py check` enforces it |
| `UPLOAD_PRESIGN_METHOD` | `PUT` | `POST` signs the size limit into the upload policy |
| `UPLOAD_VERIFY_CONTENT` | `True` | Checks uploaded bytes against the declared extension at confirm |

See `.env.example` for the full annotated descriptions.

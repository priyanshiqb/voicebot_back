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
| Prune expired login sessions + the JWT blacklist | `purge_expired_sessions` | Yes — `--dry-run` to preview. **Already scheduled daily** |
| Retire and delete dead FCM device tokens | [`purge_device_tokens`](purge_device_tokens.md) | Yes — `--dry-run` to preview. **Already scheduled daily** |

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

## Sessions and the JWT blacklist

`purge_expired_sessions` covers two tables that grew with every login and every
token refresh, and which nothing pruned:

- **`user_sessions`** — one row per login. `is_active` was only ever set `False` by
  an explicit logout, so an expired session stayed `is_active=True` forever. The
  sweep now marks them inactive, then deletes those past
  `USER_SESSION_RETENTION_DAYS`. Rows hold an IP address and a device name, so this
  is a privacy obligation as much as a disk one.
- **SimpleJWT's blacklist** — `ROTATE_REFRESH_TOKENS` and `BLACKLIST_AFTER_ROTATION`
  are both on, so *every refresh* writes an outstanding-token row and blacklists the
  old one. With a 60-minute access token that is roughly eight row pairs per active
  user per day. The sweep runs SimpleJWT's own `flushexpiredtokens`; a blacklist
  entry for an already-expired token protects nothing, since the token is refused on
  `exp` before the blacklist is consulted.

- **Push devices behind dead sessions** — a `DeviceToken` records the login it registered
  under, so this sweep also deactivates devices whose session expired. That is the one
  path a client cannot close for itself: an expired refresh token leaves the app with no
  credentials to call any endpoint with, so it cannot deactivate its own device. Runs
  **between** the demotion and the purge, because purging nulls the link.

```bash
uv run python manage.py purge_expired_sessions --dry-run
uv run python manage.py purge_expired_sessions
uv run python manage.py purge_expired_sessions --older-than-days 90
```

Safe to automate — and already is (`purge-expired-sessions`, daily). Unlike
`audit_orphaned_objects`, it deletes from data it can inspect, and unlike
`core.NotificationLog` a deleted row here re-arms nothing.

## Related settings

| Setting | Default | Affects |
|---|---|---|
| `PENDING_UPLOAD_TTL_HOURS` | 24 | Sweep cutoff, and the default audit window |
| `UPLOAD_URL_TTL_MINUTES` | 15 | Upload ticket lifetime. Must stay well below the sweep cutoff — `manage.py check` enforces it |
| `UPLOAD_PRESIGN_METHOD` | `PUT` | `POST` signs the size limit into the upload policy |
| `UPLOAD_VERIFY_CONTENT` | `True` | Checks uploaded bytes against the declared extension at confirm |
| `USER_SESSION_RETENTION_DAYS` | 30 | How long an expired login session is kept, measured from expiry |
| `USER_SESSION_PURGE_BATCH` | 5000 | Rows deleted per chunk by the session sweep |
| `SESSION_CACHE_TTL_SECONDS` | 60 | How long a positive session check is trusted without a query. Bounds revocation latency for paths that bypass `revoke()` |
| `DEVICE_TOKEN_STALE_DAYS` | 60 | No re-registration in this window ⇒ the token stops receiving pushes |
| `DEVICE_TOKEN_RETENTION_DAYS` | 90 | How long a dead device-token row is kept, measured from last contact |
| `DEVICE_TOKEN_PURGE_BATCH` | 5000 | Rows deleted per chunk by the device-token sweep |

See `.env.example` for the full annotated descriptions.

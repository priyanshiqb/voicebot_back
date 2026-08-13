# `purge_device_tokens`

Retire FCM device tokens that stopped checking in, then delete the long-dead ones.

```bash
uv run python manage.py purge_device_tokens --dry-run
uv run python manage.py purge_device_tokens
```

**Already scheduled** — `device-tokens-sweep` runs this nightly via
`core.tasks.sweep_device_tokens`. Running it by hand is for investigating, for a one-off
retention override, or for the `--verify` probe the scheduled job deliberately skips.

---

## Why this exists

An FCM registration token has **no expiry and cannot be validated offline**. It stays valid
until the app is uninstalled, its data is cleared, the browser drops its service worker,
permission is revoked, or FCM reaps the app instance for inactivity (~270 days). **None of
those events notify us.**

Before this command the only feedback was reactive — a token was retired when a real send
came back `UNREGISTERED`. That leaves two holes:

- **It only fires when we have something to say.** Push here is a low-traffic internal alert
  channel, so a device that died in March sat `is_active=True` until the next reschedule
  alert months later.
- **Nothing ever deleted a row.** `core_device_tokens` grew forever — the same unbounded
  growth `user_sessions` and the JWT blacklist had, and web tokens churn hardest, so the
  accumulation was mostly permanent junk.

---

## What it does, in order

1. **Probe** (only with `--verify`) — a validate-only send per stale token. FCM resolves the
   token and reports what it would have done, **delivering nothing**. Tokens it rejects are
   retired immediately with a distinct reason.
2. **Demote** — active tokens with no contact inside `DEVICE_TOKEN_STALE_DAYS` are marked
   `is_active=False`, with `last_error` recording why.
3. **Purge** — inactive **or** soft-deleted rows whose last contact is older than
   `DEVICE_TOKEN_RETENTION_DAYS` are hard-deleted, in batches.

> **Demote before delete, always.** An active row is never purged however old it is. A token
> is therefore visible as inactive for a full retention window before the row disappears —
> which is what makes "why did my phone stop getting alerts?" answerable.

### "Last contact" is `COALESCE(last_seen_at, created_at)`

`last_seen_at` is nullable, and filtering on it directly would silently skip every NULL row —
everything predating this feature, plus anything created through the admin or a fixture.
Exactly the population most in need of pruning would have been immortal.

---

## Options

| Flag | Effect |
|---|---|
| `--dry-run` | Report what would change; change nothing. Combines with `--verify` (probes, but retires nothing) |
| `--verify` | Ask FCM to validate each stale token first. **One API call per token** |
| `--verify-limit N` | Cap how many tokens `--verify` probes in one run. Default `500` |
| `--older-than-days N` | Override the retention window for the purge only. Staleness still uses the setting |

### Why `--verify` is not scheduled

It costs one FCM API call per stale token, and the age rule already covers the same ground —
a client that has not re-registered in two months has almost certainly rotated. Use it when a
**confirmed** answer is worth the calls: before a large purge, or when investigating whether
a specific team's devices really are gone.

A probe returns three outcomes, and only one of them acts:

| Verdict | Meaning | Action |
|---|---|---|
| **dead** | FCM rejected the token | Retired now, `last_error` says the probe rejected it |
| **alive** | The token still resolves | **Left alone by the probe** — but step 2 still demotes it. The token works; the client stopped checking in, and a device we cannot confirm a client is running is not one we want in the fan-out |
| **inconclusive** | Push unconfigured, quota, or an outage | Nothing. A transient failure is not evidence |

---

## Reading the output

```
Probed: 12 alive, 43 rejected by FCM, 2 inconclusive.
Deactivated 57 stale token(s) (no contact in 60 days); deleted 1204 dead row(s) (retention 90 days).
```

A large first run is normal and expected — it is clearing however long the table went
unpruned. A large *steady-state* number means clients are not re-registering on app start,
which on web means those devices have already gone dark. See
[FCM End-to-End](../flows/push-notifications.md), Part 2.

---

## Safety

Safe to automate, and already is. It deletes from data it can inspect (unlike
[`audit_orphaned_objects`](audit_orphaned_objects.md), which deletes from the *absence* of a
row), and unlike `core.NotificationLog` a deleted row here re-arms nothing — a device whose
row is gone simply re-registers and gets a fresh one.

The worst case is retiring a device that was still reachable. It self-heals on the next app
start, which is exactly the moment the user would notice.

---

## Related

- [FCM End-to-End Flow](../flows/push-notifications.md) — the whole lifecycle
- [Device Tokens API](../apiDocs/core/device_token.md) — the endpoints clients call
- [Maintenance Commands](README.md) — the other sweeps

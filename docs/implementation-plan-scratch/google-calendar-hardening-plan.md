# Google Calendar Integration — Hardening Plan

**Status:** partially delivered — see §0 · **Scope:** `CalendarIntegration` + Google provider + sync
**Baseline:** `contact-campaign` at `e546c8a` + the Outlook removal
**Verified against:** the installed `google-auth-oauthlib` / `oauthlib` sources and the
`calendar.v3` discovery document in `.venv`, not documentation from memory.

---

## 0. Status

The [Outlook removal](outlook-removal-plan.md) has since been executed, which closed several
items here outright. Suite is **395 tests, OK**.

| Item | State |
|---|---|
| Phase 1 — 2 red callback tests | **Done.** `f.oauth_state()` in `core/tests/scheduling/factories.py` now mints state + primes the nonce; the Outlook test was deleted with its module |
| **C1** — second-provider connect 500s | **Done.** Callback looks up on `(tenant, user)` only, with a regression test |
| **C6** — Graph `transactionId` | **Dropped** — no Graph provider |
| **C7** — migration `0035` unique violation | **Moot** — Outlook never shipped, so no dual-provider rows exist; `0035` is applied |
| **D7** — `get_busy` ↔ `account_email` coupling | **Dropped** — no Graph provider |
| **D2** — rotated refresh token discarded | **Open, downgraded to Low.** Google doesn't rotate refresh tokens, so it's no longer a path to unrecoverable `REAUTH_REQUIRED` — but `update_fields` omitting `refresh_token` is still wrong |
| **C2** C3 C4 C5 · **D1** D3 D4 D5 D6 D8 | **Open** — unchanged by the removal |

The highest-value item still open is **D1** (declined consent fails opaquely).

---

## 1. Where this starts

An earlier walkthrough raised six defects. Most already have fixes in the working tree
(uncommitted, plus migration `0035`). This plan does three things:

1. **Verifies** each in-flight fix and finishes the ones that are partial.
2. **Fixes three new defects the changes introduced** — one of which is a 500, one a
   silent regression, and one that cancels out the protection it was added for.
3. **Closes the eight items never addressed**, found in a second pass over the whole path.

**Test state when this was written — 2 red** (both since fixed, see §0):

```
FAIL: test_calendar.CalendarOAuthApiTests.test_callback_creates_integration
      AssertionError: 400 not found in (200, 302)
FAIL: test_calendar_microsoft.MicrosoftOAuthApiTests.test_callback_creates_outlook_integration
      AssertionError: 400 not found in (200, 302)
Ran 35 tests — FAILED (failures=2)
```

Both built a `state` without a `nonce`, so the single-use check rejected them. Expected fallout
from a correct change; the tests needed updating, not the code.

---

## 2. Findings

### 2.1 In-flight fixes — verified correct

| # | Fix | Verdict |
|---|---|---|
| A1 | `calendar.calendars.readonly` added to `GOOGLE_CALENDAR_SCOPES` | **Correct.** `calendars.get` accepts it per the discovery doc. Needs ops follow-through — §3.6 |
| A3 | PKCE verifier cached across `authorization_url` → `exchange_code` | **Correct, and load-bearing.** Confirmed `autogenerate_code_verifier=True` means `authorization_url()` now sends `code_challenge`, so Google *requires* the verifier at exchange. Also confirmed `Flow.fetch_token` does `kwargs.setdefault("code_verifier", …)`, so passing it explicitly wins. Cleanup only — §3.5 |
| A4 | Single-use `nonce` in cache | **Correct.** `CACHES.default` is shared Redis (`django_redis`), so it survives multi-worker; `delete()` returns a truthy count. All three TTLs (state signature, nonce, PKCE) are 600s — consistently aligned |

### 2.2 In-flight fixes — incomplete or newly broken

| # | Finding | Severity |
|---|---|---|
| **C1** | **Connecting a second provider now 500s.** The constraint became `(tenant, user)` but the callback still looks up with `provider=provider` ([calendar_integration.py:146-157](../../core/views/calendar_integration.py#L146-L157)). A host with Google who connects Outlook: filter misses → new row → **IntegrityError** on `save()`, uncaught, after the code was already exchanged | **High** |
| **C2** | **The `max_pages` cap doesn't bound the 410 path.** The reset branch sets `pages_fetched = 0` ([google.py:281](../../core/services/calendars/google.py#L281)), so a provider that keeps returning 410 loops forever — exactly the failure the cap was added to stop | **High** |
| **C3** | **Echo guard regressed.** `_our_event_ids(integration, now)` filters `start_time__gte=now`, so a booking already in progress is no longer recognised as ours and gets written into `ExternalBusyBlock` as a foreign block. Worse, a `syncToken` response ignores `timeMin/timeMax` and returns changes for *all* time, so the guard is now narrower than the data it filters | **Medium** |
| **C4** | **Preflight false-positives on rotation config.** It checks `settings.SCHEDULING_TOKEN_ENCRYPTION_KEY` only, but `crypto._keys()` also accepts `SCHEDULING_TOKEN_ENCRYPTION_KEYS` (plural). Configure rotation the supported way and `connect` returns 500 while the system is fine | **Medium** |
| **C5** | Preflight doesn't check provider credentials, so a blank `GOOGLE_CALENDAR_CLIENT_ID` still ends at a Google error page | **Low** |
| **C6** | Idempotent insert is Google-only. Graph supports `transactionId` for the same purpose; Outlook still double-creates on retry | **Medium** |
| **C7** | Migration `0035` will fail on any tenant where one user holds both a Google and an Outlook row. Needs a pre-check before it reaches production | **Medium** |

### 2.3 Never addressed

| # | Finding | Severity |
|---|---|---|
| **D1** | **Declined consent fails opaquely.** `oauthlib.validate_token_parameters` **raises** `Warning` on any granted-vs-requested scope mismatch unless `OAUTHLIB_RELAX_TOKEN_SCOPE` is set — it is set nowhere in the repo. Google's granular consent lets a host untick individual sensitive scopes, and `include_granted_scopes="true"` can return a superset. Either way: bare `except` → `"Failed to connect calendar."` with no cause, for a user who did nothing wrong | **High** |
| **D2** | **Rotated refresh tokens are discarded.** `MicrosoftCalendarProvider.refresh_access_token` calls `set_tokens(refresh_token=…)` when Graph rotates it, but the caller saves `update_fields=["access_token", "token_expires_at", "updated_at"]` ([tasks.py:209-211](../../core/tasks.py#L209-L211)) — `refresh_token` is dropped every time. Ends in an unrecoverable `REAUTH_REQUIRED` once the original expires | **High** |
| **D3** | **`ExternalBusyBlock` is never pruned.** Nothing deletes past rows, and with a `syncToken` the provider returns changes outside the 60-day horizon, so the table only grows. Every availability query scans it | **Medium** |
| **D4** | **No token-freshness gate before provider calls.** Every call uses the stored access token and relies entirely on the 15-min Beat sweep. If Beat is down, availability silently fails open (hosts become non-calendar-aware) and pushes burn 3 retries. `_credentials()` never passes `expiry`, so google-auth treats the token as eternally valid and won't self-refresh either | **Medium** |
| **D5** | `token_expires_at__lt=soon` silently excludes NULLs, so an integration with no recorded expiry is never refreshed and never flagged | **Low** |
| **D6** | Mirrored events carry only title/start/end — no description, no attendee context. A host sees "Consultation" with no idea who booked | **Low** |
| **D7** | `MicrosoftCalendarProvider.get_busy` sends `schedules: [integration.account_email]`; if that resolved empty, free/busy silently returns nothing. Google uses the `"primary"` literal and has no such coupling | **Low** |
| **D8** | No coverage for any new behaviour: PKCE, nonce reuse, idempotent insert, page cap, provider switch | **Medium** |

---

## 3. Build order

Sequenced so each phase leaves the suite green.

### Phase 1 — Unbreak the suite *(smallest first, no behaviour change)*

- `test_calendar.py::test_callback_creates_integration` and
  `test_calendar_microsoft.py::test_callback_creates_outlook_integration`: add `nonce` to the
  signed payload and seed `oauth_state_{nonce}` in the cache.
- Add a helper in `core/tests/scheduling/factories.py` — `oauth_state(user, tenant, provider)` —
  that mints state and primes the cache, so future tests don't re-learn this.

**Gate:** 35 tests green.

### Phase 2 — The three regressions

**C1 — provider switch.** Drop `provider` from the callback lookup and treat a provider change
as a migration of the row, not a new row:

```python
integration = CalendarIntegration.objects.filter(
    tenant=tenant, user_id=payload["user_id"], is_deleted=False
).first()
switching = integration is not None and integration.provider != provider
```

When `switching`, before saving: stop the old watch, then clear `sync_token`,
`watch_channel_id`, `watch_resource_id`, `watch_token`, `watch_expires_at`, and hard-delete
that integration's `ExternalBusyBlock` rows. A Google `syncToken` handed to Graph is not
merely useless — it produces a wrong full-scan, and stale blocks from the old provider would
keep blocking availability forever.

**C2 — real loop bound.** Replace the page counter reset with a separate allowance:

```python
resets = 0
MAX_RESETS = 1
...
if exc.resp.status == 410 and resets < MAX_RESETS:
    resets += 1
    sync_token, page_token, changes = "", None, []
    continue   # note: pages_fetched keeps counting
```

Keep `max_pages` monotonic. Also raise the cap to 200 — at Google's 250-per-page default,
50 pages is ~12.5k events, and the current `RuntimeError` permanently wedges `sync_token`
for a calendar that exceeds it.

**C3 — echo guard window.** Bound by *end* time with a grace margin, and don't cap the upper
end, because `syncToken` responses ignore the request window:

```python
def _our_event_ids(integration, now):
    return set(Booking.objects.filter(
        tenant=integration.tenant, primary_host=integration.user, is_deleted=False,
        end_time__gte=now - timedelta(days=1),      # in-flight + just-ended
    )...)
```

**Gate:** new tests — connecting Outlook over Google switches the row and clears the old
watch/cache; a repeated 410 raises instead of hanging; an in-progress booking is still
recognised as ours.

### Phase 3 — Declined consent (D1)

Two parts, and the second is what makes the failure legible:

1. `os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")` at the top of
   `core/services/calendars/google.py`, so a scope difference no longer raises from inside
   `fetch_token`.
2. Validate what was actually granted. Add `REQUIRED_GOOGLE_SCOPES = {calendar.events,
   calendar.freebusy}`; after exchange, diff against `creds.scopes` and raise a typed
   `CalendarScopeError(missing=…)` if anything required is absent. The callback catches it
   distinctly from the generic failure and returns a message naming the permission —
   `"Calendar connect needs the 'See free/busy' permission. Please reconnect and leave all
   boxes checked."` — instead of `"Failed to connect calendar."`

`calendar.calendars.readonly` stays **optional**: losing it costs only `account_email`, so a
host who declines it should still connect successfully.

**Gate:** tests for a subset grant (rejected, names the missing scope), a superset grant
(accepted), and a missing-but-optional grant (accepted).

### Phase 4 — Token lifecycle (D2, D4, D5)

- **D2:** save `refresh_token` too. Rather than widening the list at the call site, have
  `refresh_access_token` return the fields it touched, or simply save all four —
  `["access_token", "refresh_token", "token_expires_at", "updated_at"]`. The provider is the
  only thing that knows whether rotation happened, so the caller shouldn't be guessing.
- **D5:** `Q(token_expires_at__lt=soon) | Q(token_expires_at__isnull=True)`.
- **D4:** add `calendar_sync.ensure_fresh_token(integration)` — refresh + persist if
  `token_expires_at` is within 2 minutes — and call it at the top of `run_incremental_sync`,
  `push_booking_to_calendar`, `delete_booking_calendar_event`, and
  `_external_busy_intervals`. Beat becomes an optimisation rather than a dependency. Keep
  the fail-open behaviour in availability: a refresh failure there still yields `[]`.

**Gate:** a stale-token integration refreshes on demand with no Beat run; the rotated
refresh token survives a `refresh_from_db()`.

### Phase 5 — Idempotency parity + cache hygiene (C6, D3)

- **C6:** send `transactionId: str(booking.id)` on Graph `POST /me/events`. Graph rejects a
  duplicate `transactionId` for ~48h, giving the same protection as Google's client-side id.
- **Google 409 edge:** Google retains ids of deleted events, so a booking pushed → cancelled →
  re-pushed to the same host (reassign A→B→A) hits 409, and blind-returning the id records
  success against a `cancelled` event that nobody can see. On 409, `events.get` it and if
  `status == "cancelled"`, `events.update` it back to confirmed.
- **D3:** new Beat task `prune_external_busy_blocks` (daily) deleting `end_time < now - 7d`.
  Use `all_objects` — `BaseQuerySet.delete()` is blocked, and these rows are pure cache with
  no audit value. Confirmed instance `.delete()` in `run_incremental_sync` is *not* blocked,
  so that existing call is fine as written.

### Phase 6 — Cleanups (C4, C5, D6, D7, A3)

- **C4:** replace the two inline settings checks with `crypto.is_configured()` that mirrors
  `_keys()` exactly, so plural/rotation config is accepted.
- **C5:** extend the preflight to the resolved provider's client id / secret / redirect URI.
  Return one clear message listing what's unset.
- **D6:** build a `description` from participant names/emails plus the reschedule link.
  **Deliberately not adding `attendees`** — `core/services/ics.py` already emails invites,
  and Google would send a second, competing invite from the host's account. Worth confirming
  which system should own invitations before going further than a description.
- **D7:** default `schedules` to `"me"` when `account_email` is empty.
- **A3 cleanup:** key the PKCE entry on `nonce` rather than the full signed state blob, and
  delete it after a successful exchange.
- **C4/B4:** wrap `set_tokens()` + `save()` in the callback's try/except. The preflight makes
  the common cause unreachable, but a malformed key still raises from Fernet *after* the code
  is spent, leaving a live grant at Google and no local row.

### Phase 7 — Coverage (D8) and docs

Tests: PKCE round-trip; nonce replay → 400; expired nonce → 400; provider switch; scope
subset/superset; refresh-token rotation persisted; on-demand refresh without Beat;
duplicate-insert idempotency both providers; page cap; prune task.

Docs to update: [02-google-calendar.md](../external-app-creds/02-google-calendar.md) (third
scope, granular consent, reconnect requirement), [04-google-calendar-sync.md](../flows/04-google-calendar-sync.md)
(one-calendar-per-host, provider switch, prune job), [calendar_integration.md](../apiDocs/core/calendar_integration.md)
(new error responses).

---

## 4. Pre-deploy — not code

| # | Action | Why it blocks |
|---|---|---|
| **O1** | Add `calendar.calendars.readonly` to the Google consent screen | Requesting an unlisted sensitive scope fails at consent |
| **O2** | Audit for dual-provider users before migration `0035`:<br>`CalendarIntegration.objects.filter(is_deleted=False).values("tenant","user").annotate(n=Count("id")).filter(n__gt=1)` | `AddConstraint` fails on a live unique violation (C7) |
| **O3** | Set `SCHEDULING_TOKEN_ENCRYPTION_KEY` in `.env` — **currently absent**; only `.env.example` has the placeholder | Nothing can connect at all |
| **O4** | Set `GOOGLE_CALENDAR_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` — also absent from `.env` | Same |
| **O5** | Tell already-connected hosts to reconnect | Existing grants predate the third scope, so `account_email` stays empty for them |
| **O6** | Confirm Celery **worker + beat** are running | Watch setup, token refresh, and every push are tasks |

O3 and O4 are why the integration has never actually run in this environment. Worth settling
before any of the above ships, or none of it can be tested end to end.

---

## 5. Risk register

| Risk | Mitigation |
|---|---|
| `OAUTHLIB_RELAX_TOKEN_SCOPE` hides a genuinely insufficient grant | Explicit required-scope validation (Phase 3) is the real check; the env var only stops the exception |
| Provider switch loses history | The row is reused and audited (Tier 2 `updated_by`); only provider-specific cursors and cache rows are cleared |
| `ensure_fresh_token` adds a refresh to the availability hot path | Only fires within 2 min of expiry; result persisted so concurrent requests see it |
| One-calendar-per-host is a product decision, not just a constraint | Confirm intent — the migration is easy to reverse now, hard once hosts rely on two |
| Prune window too aggressive | 7 days is well past any bookable window; `end_time` based, so long events survive |

---

## 6. Open questions

1. **One calendar per host — intended?** Migration `0035` makes it structural. If hosts should
   be able to connect Google *and* Outlook, revert it and make both `.first()` call sites
   iterate instead ([availability.py:103](../../core/services/availability.py#L103),
   [tasks.py:92](../../core/tasks.py#L92)). Everything in Phase 2's C1 fix assumes one.
2. **Who owns invitations** — our `.ics` emails, or Google/Graph attendee invites? Blocks the
   scope of D6.
3. **Should a declined optional scope block connect?** Plan says no (connect succeeds,
   `account_email` empty). If the UI needs the address, it becomes required.

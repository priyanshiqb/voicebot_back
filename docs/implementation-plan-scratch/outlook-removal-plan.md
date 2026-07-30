# Remove Outlook / Microsoft 365 — Google Calendar only

**Status:** draft for review · **Scope:** delete the Graph provider, keep the provider abstraction
**Companion:** [google-calendar-hardening-plan.md](google-calendar-hardening-plan.md) — this plan
retires three of its work items and de-risks a fourth (§2)

---

## 1. Recommended shape

**Delete the Microsoft implementation. Keep everything that makes it re-addable.**

Concretely: keep `CalendarProvider` (the ABC), keep the `_PROVIDERS` registry, keep the
`provider` field on `CalendarIntegration`, and keep the `Provider` enum — with `GOOGLE` as its
only member. Remove `microsoft.py`, its webhook view and route, its settings, the `msal`
dependency, its tests, and its docs.

Why not go further and drop the `provider` field:

- `Booking.external_provider` records which provider each mirrored event went to. It's plain
  history, and dropping the source enum makes those rows unreadable.
- Django `choices` are **not** a database constraint, so narrowing the enum is a no-op at the
  DB level. There is nothing to gain from removing the column and real cost in re-adding it.
- The whole removal becomes one self-contained commit that `git revert` undoes. Ripping out the
  abstraction turns a future Outlook decision back into a rewrite.

The cost of keeping it is one unused-looking `CharField`. That's the right trade.

---

## 2. Effect on the hardening plan

Removing Outlook isn't just deletion — it retires real work:

| Hardening item | Effect |
|---|---|
| **C1** — second-provider connect 500s | **Resolved.** Outlook never shipped, so no `OUTLOOK` row can exist anywhere; with one provider the callback's `provider=` filter can never miss. Still worth deleting the redundant filter (§5 Phase 2) so the class of bug is impossible rather than merely unreachable |
| **C6** — Graph `transactionId` idempotency | **Dropped.** Google's client-supplied id already covers the only remaining provider |
| **D7** — `get_busy` coupled to `account_email` | **Dropped.** Google uses the `"primary"` literal |
| **D2** — rotated refresh token discarded | **Severity High → Low.** Only Graph rotates refresh tokens; Google doesn't. Still worth fixing (`update_fields` omitting `refresh_token` is wrong either way) but it stops being a path to unrecoverable `REAUTH_REQUIRED` |

---

## 3. Inventory

Everything that references Outlook, confirmed by grep — nothing here is speculative.

**Delete outright**

| Path | Notes |
|---|---|
| `core/services/calendars/microsoft.py` | 357 lines |
| `core/tests/scheduling/test_calendar_microsoft.py` | 14 tests |
| `docs/external-app-creds/03-outlook-microsoft-365.md` | |
| `docs/outlook-calendar-implementation-plan.md` | Superseded — the work shipped, then got reverted by this plan |

**Edit**

| Path | Change |
|---|---|
| `core/services/calendars/__init__.py` | Drop the import, registry entry, `__all__` entry, and "+ Microsoft/Outlook" from the docstring |
| `core/models/calendar_integration.py:16` | Remove the `OUTLOOK` choice; reword the `watch_token` help text (drops "or Microsoft Graph's clientState") → **migration** |
| `core/views/calendar_webhook.py:52-99` | Delete `MicrosoftCalendarWebhookView` |
| `core/urls/calendar_webhook.py` | Drop the `calendar-webhook/microsoft/` route |
| `core/services/calendar_sync.py:20-31` | Collapse `_WEBHOOK_URL_SETTINGS`; keep `webhook_url_for()` as the "" gate that disables webhooks |
| `core/views/calendar_integration.py:76` | Docstring drops `OUTLOOK` |
| `config/settings/base.py:490-512` | Remove 6 `MICROSOFT_*` settings |
| `pyproject.toml:39` + `uv.lock` | Remove `msal>=1.31.0`, then `uv lock` |
| `.env.example:177-187`, `.env.local.example:57` | Remove the Microsoft block |
| `docs/_sidebar.md:32` | Remove the Outlook entry |
| `docs/external-app-creds/README.md` | Remove row 3, the env block, 5 table rows, and the `MICROSOFT_*` code-settings note |
| `docs/external-app-creds/02-google-calendar.md:76` | "Same callback for all providers" no longer applies |
| `docs/apiDocs/core/calendar_integration.md` | Remove `?provider=` docs, §6 Graph webhook, and the dead plan link at :145 |
| `docs/scheduling-deployment-checklist.md` | Remove §4.4, §4.5, 5 env rows, the `msal` mention, and "Google + Outlook" in the Beat table |

**Leave alone**

- `core/migrations/0024_*.py`, `0027_*.py` — historical migrations are immutable. `0024` will
  keep `choices=[('GOOGLE',…),('OUTLOOK',…)]` forever; that's correct and harmless.
- `docs/reports/security_loopholes_report.md`, `docs/scheduling-booking-implementation-plan-v4.md`
  — historical records of what was true when written.
- `Booking.external_provider` — no `choices`, so no constraint and no migration.

---

## 4. Data — nothing to clean up

**Outlook never reached production**, so no host ever completed the OAuth flow against it.
That removes the entire migration/cleanup track this plan originally carried:

| Concern | Status |
|---|---|
| `OUTLOOK` integration rows | None. The code path was never live |
| Graph subscriptions in the field | None — so no ordering constraint on deleting `microsoft.py` |
| Orphaned events in hosts' Outlook calendars | None |
| Outlook-mirrored `Booking.external_provider` rows | None |
| Data migration to soft-delete stale rows | Not needed |

Dev confirms it independently: 3 Google integrations, **0 Outlook**, 0 busy blocks,
0 Outlook-mirrored bookings.

**One cheap sanity check** before Phase 1, only if a staging environment exists that tracked
this branch — it costs a second and turns an assumption into a fact:

```python
CalendarIntegration.all_objects.filter(provider="OUTLOOK").count()   # expect 0
```

If that ever returns non-zero, stop: the Graph subscriptions must be stopped *while*
`microsoft.py` still exists (`DELETE /subscriptions/{id}` needs the client), and this plan
needs the cleanup phase added back. Git history for this file has it.

---

## 5. Build order

### Phase 1 — Remove the code
Everything in §3 in one commit, so `git revert` restores Outlook wholesale.

Order within the commit: `microsoft.py` and its test module first, then the registry, then
settings and the dependency — so any missed reference surfaces as an `ImportError` at check
time rather than a runtime 500.

Then: `python manage.py makemigrations core` for the enum + help-text change, `uv lock` for
the `msal` removal.

### Phase 2 — Harden what's left
- Drop `provider` from the callback's lookup ([calendar_integration.py:146](../../core/views/calendar_integration.py#L146)).
  With one provider the filter is dead weight, and removing it makes the C1 constraint crash
  structurally impossible instead of merely unreachable-by-luck.
- `_resolve_provider` already 400s on `?provider=OUTLOOK` (it validates against
  `Provider.values`). Keep that, but give it an explicit message —
  `"Outlook is no longer supported. Only GOOGLE is available."` — so a stale front-end build
  gets a real answer instead of a generic rejection.

### Phase 3 — Tests
- Delete `test_calendar_microsoft.py`. Nothing in it needs preserving:
  `test_setup_watch_noop_when_only_google_configured` proved per-provider webhook gating, which
  is moot with one provider, and `test_webhook_sync.py::test_setup_watch_noop_without_config`
  already covers the no-config case.
- Add: `?provider=OUTLOOK` → 400 with the new message; `POST /calendar-webhook/microsoft/` →
  404; `Provider.values == ["GOOGLE"]`.
- Confirm `core.services.calendars` imports with `msal` uninstalled — the point of the lazy
  imports, and now the only guard that the dependency is really gone.

**Gate:** full suite. Currently **35 calendar tests, 2 red** for an unrelated reason (the
nonce change — see the hardening plan, Phase 1). Expect ~21 calendar tests after removal.
Fix those 2 first or the signal here is muddy.

### Phase 4 — Docs
All §3 doc edits. Re-validate every `_sidebar.md` link resolves and nothing is orphaned.

---

## 6. Risk register

Now that Outlook never shipped, the remaining risks are all about the removal itself rather
than about anything in the field.

| Risk | Mitigation |
|---|---|
| A missed reference 500s at runtime | Removal order in Phase 1 turns misses into import errors; `manage.py check` + full suite before merge |
| Outlook is wanted back later | One revertible commit; the ABC, registry, and `provider` field all stay |
| Front-end still sends `?provider=OUTLOOK` | Explicit 400 message (Phase 2) rather than a silent default to Google — connecting the wrong calendar type would be worse than an error |
| `msal` removal breaks an unrelated import | It's referenced only by `microsoft.py` (grep-confirmed); the Phase 3 import check catches it either way |
| An Outlook row turns up in staging after all | §4's one-line check; git history for this file holds the cleanup phase |

---

## 7. Open questions

1. **Keep `Provider` as an enum at all?** Recommendation is yes (§1). Say so if you'd rather
   see the field dropped entirely — it changes Phase 1 substantially and isn't reversible.
2. **Is the front-end sending `?provider=`?** If it hardcodes `GOOGLE`, Phase 2's message is
   belt-and-braces. If it has a provider picker, that UI needs removing too — out of scope
   here but it shouldn't ship pointing at a 400.

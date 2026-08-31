# Tenant Console API

Everything about one customer. **Base path:** `/api/v1/ops/tenants/`

The screen support opens when a customer emails. Two endpoints carry the whole
milestone: a list you can find somebody in, and a payload that answers every question
about them without a second request.

---

## Why the API is shaped like this

**The data all existed; nobody could assemble it.** It is spread across eight models in
four apps, three of which carry *frozen snapshots* — so the live plan is not the answer
to "what are they paying?", and the live rate card is not the answer to "what does a
minute cost them?".

| Question | Where the answer is | Where it is **not** |
|---|---|---|
| What are they paying? | `Subscription.plan_snapshot` | `Subscription.plan` |
| What do calls cost? | `Wallet.rate_snapshot` | `CreditRate` |
| Why was this call 12 credits? | that `CreditLedger` row's rating fields | anywhere else |
| Is their balance right? | `SUM(CreditLedger.amount)` per bucket | `Wallet.balance` — that is the cache |

The last row is why the `health` block exists. **The balance is a cache; the ledger is
the truth**, and until this endpoint the only thing checking they agreed was a
management command somebody had to remember to run.

---

## Endpoints

| | | Permission |
|---|---|---|
| `GET` | `/ops/tenants/` | read |
| `GET` | `/ops/tenants/{id}/` | read |
| `GET` | `/ops/tenants/{id}/health/` | read |
| `PATCH` | `/ops/tenants/{id}/` | `tenant.profile` |
| `POST` | `/ops/tenants/{id}/enforcement/` | `enforcement.change` |

Access rules, the audit contract and the response envelope are in
[the console README](./README.md). Every write requires a non-empty `reason` in the
body and leaves one `OperatorAuditLog` row; **reads leave none**, deliberately — a log
of actions that also recorded page views would bury the eleven action types under
thousands of rows.

---

## `GET /ops/tenants/` — the list

One row per tenant, with the columns the list screen needs and **no query per row**.

### Columns

`id` · `name` · `subdomain` · `created_at` · `is_active` · `is_deleted` · `deleted_at`
`plan` · `plan_code` · `subscription_status` · `cycle_ends` · `has_scheduled_change`
`balance` · `wallet_frozen` · `credits_enforced` · `entitlements_enforced` · `last_call_at`

`plan` is read from the **snapshot**, falling back to the live plan only for rows
written before snapshots existed. A tenant on a plan renamed last week is still on what
they bought, and the list has to agree with the 360 or support chases a discrepancy
that is not one.

### Query parameters

| | Values | Notes |
|---|---|---|
| `q` | free text | `name`, `subdomain`, **and owner email** |
| `plan` | a plan code | matched on the live subscription's snapshot |
| `status` | `active` `trialing` `expired` `cancelled` `none` | `none` = no subscription at all |
| `enforcement` | `credits` `entitlements` `both` `neither` | |
| `frozen` | `true` `false` | the wallet |
| `is_active` | `true` `false` | the tenant |
| `include_deleted` | `true` | soft-deleted tenants, excluded by default |
| `ordering` | `-created_at` (default) `created_at` `name` `-name` `balance` `-balance` `last_call_at` `-last_call_at` `cycle_ends` `-cycle_ends` | a whitelist; anything else falls back to the default |
| `page`, `page_size` | | `page_size` capped at 100 |

> **`status=none` finds the interesting rows.** A tenant with no subscription is either
> mid-onboarding or a payment that never completed, and neither appears under any other
> value. It is deliberately **not** the same as `status=expired` — "never signed up" and
> "cycle ended" are different problems, so the list resolves the current-or-lapsed
> subscription the same way `subscriptions.current_or_expired` does.

> **`frozen=false` and `enforcement=neither` include tenants with no wallet or settings
> row.** Those annotate as `NULL`, not `False`, and an `exclude()` would silently drop
> them — which would hide exactly the half-onboarded accounts somebody filtering for
> "not frozen" is looking for.

### Cost

**2 queries, at any page size and any number of tenants.** One statement for the page,
one for the pagination count; every per-tenant scalar is a correlated subquery inside
the first. Asserted as an exact number in `ops/tests/test_tenant_query_budget.py`, at
two different row counts, so a per-row leak fails even if somebody raised the first
assertion.

---

## `GET /ops/tenants/{id}/` — the 360

Eleven blocks, one request. Soft-deleted tenants are readable here — *"my account
vanished"* is a question this console exists to answer.

```jsonc
{
  "tenant":       { "id", "name", "subdomain", "phone_number", "created_at",
                    "is_active", "is_deleted", "deleted_at", "timezone", "language",
                    "referred_by_code", "tenant_referral_code" },
  "enforcement":  { "credits_enforced", "entitlements_enforced", "configured" },
  "owner":        { "id", "email", "name", "last_login", "is_verified", "is_active",
                    "derived": "first_user" },
  "user_count":   1,
  "subscription": { "id", "status", "start_date", "end_date", "days_remaining",
                    "is_expired", "is_current", "plan", "live_plan", "rules",
                    "scheduled_change", "superseded_by", "canceled_at" },
  "wallet":       { "exists", "balance", "allocated_balance", "purchased_balance",
                    "lifetime_granted", "lifetime_consumed", "is_frozen",
                    "frozen_reason", "frozen_at", "low_balance_threshold",
                    "low_balance_notified_at", "current_plan",
                    "rate_snapshot", "rate_snapshot_at" },
  "entitlements": { "rows": [ /* 7 */ ], "reason", "purchased_extras" },
  "payments":     { "rows": [ /* last 10 */ ], "note" },
  "ledger":       [ /* last 20, each with the rating that produced it */ ],
  "usage":        { "window_days", "since", "calls", "minutes", "credits_burned",
                    "campaigns_active", "by_tier" },
  "health":       { "ok", "checks": [ /* 4 */ ] }
}
```

### Things worth knowing

**`owner` is derived, and says so.** There is no owner field — `TenantUserMapping` is a
bare 1:1 with no role and no flag, and the signup user is mapped exactly like every
user added later. The earliest mapping is the account holder for every tenant created
through the signup flow. It carries `"derived": "first_user"` so nobody builds an
ownership transfer on an inference.

**`subscription` is the live row, or the most recent lapsed one.** A tenant whose cycle
ended still has a relationship with us, and that row is precisely what is needed when
the ticket is *"why can't I make calls?"*. `days_remaining` is **signed** — `-3` means
the cycle ended three days ago — and truncated toward zero, so a cycle that ended three
days and one second ago reads `-3`, not `-4`.

**`subscription.plan` is the frozen snapshot; `live_plan` is the current record.**
Showing both is how the console can say a plan has been edited since it was sold.

**`payments` does not include the onboarding order, and the `note` says so.** An
onboarding transaction has `tenant = NULL` by check constraint — the tenant does not
exist until that payment clears — so it is reachable through the signup record only. A
customer counting their receipts will find one more than this list holds.

**`bonus_days` is on the payment row, not the subscription.** Bonus days are computed
at pricing time, applied to `end_date` by `activate()`, and survive only inside that
order's `amount_breakdown`. Surfacing them where they were recorded is honest; a
subscription field that nothing writes would not be.

**`ledger` is read from `all_objects`.** The ledger is append-only, and a row that has
somehow been soft-deleted is the single most important row on the screen.

### Every block degrades on its own

A tenant with no subscription, no wallet and no settings row is **not an edge case** —
it is a signup whose payment never cleared, which is exactly the tenant support is
looking at. The request returns `200` and each block reports its own absence:

| Missing | What comes back |
|---|---|
| Subscription | `"subscription": null`, and `entitlements.reason = "no_subscription"` with any purchased extras still listed |
| Wallet | the zeroed wallet block with `"exists": false` |
| Settings row | `credits_enforced: false`, `entitlements_enforced: false`, `"configured": false` |
| Calls | `usage` counters at `0`, not `null` |

> **A `GET` here creates nothing.** `TenantSettings.for_tenant()` and
> `Wallet.for_tenant()` are `get_or_create` — they exist so the *call path* cannot fail
> on a missing row. Using them on a read would mean opening a tenant's page writes rows,
> and rendering a list of a hundred tenants writes a hundred. Reads use explicit filters
> and fall back to the models' own defaults.

### Cost

**24 queries**, plus one that every authenticated request in the project pays
(`config.middleware` resolving `request.user.tenant_mapping`, which an operator never
has). That is the honest cost of assembling eight sources without lying about any of
them, and it is fine: one screen, one tenant, opened by a human. None of it is per-row —
that is what the list's budget protects.

---

## `GET /ops/tenants/{id}/health/`

The `health` block on its own, for a client that wants to re-check without re-fetching
the whole 360. Four checks; `ok` is true only when every one of them is.

| Code | Fails when | Why it matters |
|---|---|---|
| `WALLET_RECONCILES` | ledger sums ≠ wallet buckets | The balance is a cache. Nothing else checks this per tenant |
| `NO_UNRATED_CALLS` | a call completed >15 min ago with no `USAGE_DEDUCTION` | Revenue quietly not collected |
| `NO_FAILED_PAYMENTS` | a `failed` or `underpaid` order in 30 days | `underpaid` is its own state — a human has to choose between refunding and collecting |
| `LAPSE_CONSISTENT` | an expired subscription with an unfrozen wallet, or an active one frozen *for a lapse* | The tenant keeps spending credits they no longer have a cycle for — or a paying customer is locked out |

**Neither of the first two is reimplemented here.** `WALLET_RECONCILES` calls
`wallet.services.reconciliation.check()`, which the nightly `reconcile_wallets` command
also calls; `NO_UNRATED_CALLS` calls `unrated_for()`, shared with the
`report_unrated_calls` task. Two answers to *"does this balance add up?"* would mean the
screen and the sweep could disagree, and no operator would know which to believe.

`LAPSE_CONSISTENT` keys on `Subscription.status`, not on `end_date`: a cycle whose date
passed an hour ago is waiting for the expiry job, which is not a fault. It also passes a
plan with `allow_use_after_expiry`, which lapses without freezing **by design**.

---

## `PATCH /ops/tenants/{id}/`

Correcting what somebody typed wrong at signup. Requires `tenant.profile`.

```jsonc
{ "reason": "customer rebranded", "name": "Acme Industries" }
```

| Field | |
|---|---|
| `name` | |
| `timezone` | FK; an unknown id is a **field error**, not a 500 |
| `language` | FK; same |

Deliberately not the whole model. `subdomain` is a routing key,
`tenant_referral_code` is somebody else's referral history, and `is_active` is a
lifecycle transition with its own action code. An empty patch is refused — an audit row
recording a change of nothing is worse than no row.

The response is the **whole 360**, so the console holds one object shape whether it
just read or just wrote.

---

## `POST /ops/tenants/{id}/enforcement/`

Requires `enforcement.change`. At least one switch must be present.

```jsonc
{ "reason": "metering period reviewed, rates confirmed", "credits": true }
```

| | |
|---|---|
| `credits` | whether a credit shortfall actually blocks work |
| `entitlements` | whether exceeding a plan limit actually blocks work |

**Both default to off platform-wide.** Metering runs regardless — every call is rated
and written to the ledger from day one — and enforcement is what turns a shortfall into
a refusal. Switching it on for a tenant already over their limits would break a working
account with no warning, so it is a per-tenant decision made after looking at the usage
on this very screen.

Only the switch you name moves; the other keeps its value. The audit row records both
`before` and the resulting state.

> This is the one place a `for_tenant()` is right. A tenant being *given* an enforcement
> setting is exactly when the settings row should come into existence — the ban on
> `get_or_create` is about read paths.

---

## Not in this milestone

| | Why |
|---|---|
| `POST /suspend/` | **`Tenant.is_active` is enforced nowhere.** Nothing in authentication or on the call path reads it, so the endpoint would set a field, write a convincing audit row, return `200`, and change nothing. An operator with a green audit row would not check again. It ships once the flag is real — see §8 of [the module plan](../../implementation-plan-scratch/admin-panel/milestone-2-tenant-360.md) |
| Credit grants, freezes, cycle extensions | Milestone 3 |
| Payment replay | Milestone 4 |
| Manual onboarding | Milestone 5 |

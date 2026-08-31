# Milestone 2 — Tenant List & the 360 View

Module 2 of the admin console: **the screen support opens when a customer emails.**

Milestone 1 (`IsPlatformStaff`, `PlatformRole`, `OperatorAuditLog`) and Module 1
([Plan Configuration](../plan-configuration-module-plan.md)) are built. This is the next
one, and it is deliberately **read-first**: per the umbrella plan, *"Read-only is already
most of the value."*

| | |
| :--- | :--- |
| Umbrella | [Admin Console — Plan](../admin-console-plan.md) §4.1, §4.2 |
| Milestone | 2 of 8 |
| Screens unblocked | #2 Tenant list (P0), #3 Tenant 360 (P0) |
| New models | **None.** One enum member and one optional field, both outside `ops` |
| Shape | `ops/services/tenant/`, `ops/serializers/tenant/`, `ops/views/tenant.py` |
| Status | ✅ **Built.** Endpoints documented at [Tenant Console API](../../apiDocs/ops/tenants.md). `suspend`/`restore` deferred — §8 |

---

## 1. The problem this module solves

**Today, answering "what is going on with this customer?" requires a shell.**

The data all exists. It is spread across eight models in four apps, three of which
(`Wallet`, `Subscription`, `CreditLedger`) carry frozen snapshots that mean the live plan
is *not* the answer to "what are they paying?". A support agent cannot assemble that, and
should not have to:

| Question | Where the answer actually is |
| :--- | :--- |
| What are they paying? | `Subscription.plan_snapshot`, **not** `Subscription.plan` |
| What do calls cost them? | `Wallet.rate_snapshot`, **not** `CreditRate` |
| Why was this call charged 12 credits? | `CreditLedger.rate_applied` / `units` / `unit_type` on that row |
| Are they over a limit? | `entitlements.summary(with_usage=True)` — 7 live counts |
| Did their payment land? | `Transaction.status` + whether `payment_application.apply()` ran |
| Is their balance even right? | `SUM(CreditLedger.amount) == Wallet.balance`, per bucket |

The last row is the one that matters most. **The wallet balance is a cache; the ledger is
the truth**, and the only thing that currently checks the two agree is a management
command run by hand. If it has drifted for one tenant, nobody finds out until somebody
runs `reconcile_wallets` across the whole table.

> **The design decision: the 360 answers "is anything wrong with this tenant?" before
> support has to ask.** That is the `health` block, and it is the part of this module
> worth building carefully. Everything else is assembling records that already exist.

---

## 2. The rule that shapes this module

> ⚠️ **Never reuse the tenant-scoped viewsets.** `TenantBaseViewSet.get_queryset()`
> resolves the tenant from `request.user.tenant_mapping` and raises `PermissionDenied`
> when there is none. A platform operator **has** none — the database constraint
> `ck_platform_role_requires_admin_user_type` and the 1:1 mapping guarantee it.

The failure mode is not an error. `TenantBaseViewSet` is doing exactly what it was built
to do; it is being asked the wrong question. An operator endpoint takes the tenant as a
**path parameter** and reads it with an explicit filter, every time.

This is the same warning already in the docstring of [`ops/views/plan.py`](../../../ops/views/plan.py).
It is repeated here because Module 1 never touched a tenant-scoped model and this module
touches nine of them.

**Corollary — every service in this module takes `tenant_id`, never `request`.** A
service that reads the request is a service that can be handed the wrong tenant by a
future caller and will not notice.

---

## 3. What exists, verified against the code

| | Status | Where |
| :--- | :--- | :--- |
| `Tenant`, `TenantSettings`, `TenantUserMapping` | ✅ | `tenant/models/` |
| `Subscription` with frozen `plan_snapshot` + `rules` property | ✅ | `onboard/models/subscription.py` |
| `subscriptions.current_for` / `current_or_expired` | ✅ | `onboard/services/subscriptions.py:29` |
| `Wallet` with `rate_snapshot`, buckets, freeze state | ✅ | `wallet/models/wallet.py` |
| `CreditLedger`, append-only, with the rating on each row | ✅ | `wallet/models/credit_ledger.py` |
| `entitlements.summary(tenant_id, sub, with_usage=True)` | ✅ | `onboard/services/entitlements.py:104` |
| `Transaction` with `amount_breakdown` | ✅ | `onboard/models/transaction.py` |
| `AgentCallLog` — duration, tier, outcome, mood | ✅ | `core/models/agent_call_log.py` |
| `OperatorAuditLog` + `operator_audit.record()` | ✅ | `user/services/operator_audit.py` |
| `SetPagination` with the standard envelope | ✅ | `utils/views/base_model_viewset.py:19` |
| **A tenant "owner"** | ❌ **does not exist** — see §9.1 |
| **Wallet-vs-ledger check as a callable** | ❌ logic is inline in a management command — §6.2 |
| **Unrated-calls check as a callable** | ❌ logic is inline in a Celery task — §6.2 |
| **`Tenant.is_active` having any effect** | ❌ **enforced nowhere** — §8 |
| **`Subscription.bonus_days_applied`** | ❌ no such field — §9.3 |

Three of those five gaps are the actual work. The rest is routing and aggregation.

---

## 4. Endpoints

```
GET    /api/v1/ops/tenants/                    list, search, filter, sort   (read)
GET    /api/v1/ops/tenants/{id}/               the 360 payload              (read)
GET    /api/v1/ops/tenants/{id}/health/        just the health block        (read)
PATCH  /api/v1/ops/tenants/{id}/               name, timezone, language     tenant.profile
POST   /api/v1/ops/tenants/{id}/enforcement/   {credits, entitlements}      enforcement.change
POST   /api/v1/ops/tenants/{id}/suspend/       is_active = False            tenant.suspend  ⚠️ §8
POST   /api/v1/ops/tenants/{id}/restore/       is_active = True             tenant.suspend
```

**Reads require only `IsPlatformStaff`.** Same rule as Module 1: a view that names no
`required_permission` is a read, and the namespace gate has already run. A console where
support cannot look at a tenant is not a console.

**Writes name their permission, require a `reason`, and leave an audit row.** Identical
mechanics to `PlanConfigurationViewSet` — `WRITE_ACTIONS`, `required_permission` as a
**property** (not something a handler sets; `has_permission` runs during `initial()`,
before the handler is ever called), `_require_reason()`, `_record()`.

`health` is split out as its own endpoint as well as being embedded in the 360, because
it is the block a future platform-health screen wants across many tenants, and because it
is the most expensive part of the payload — a client polling the 360 for freshness should
be able to poll the cheap half instead.

### `tenant.profile` — a new action code

`OperatorAuditLog.Action` has 11 members; `tenant.onboard`, `tenant.suspend` and
`enforcement.change` cover three of the four writes above. **A `PATCH` on name/timezone
has no code**, and inventing an unaudited write would break the one-vocabulary rule that
makes "what somebody may do" and "what is recorded when they do it" the same list.

Add `TENANT_PROFILE = "tenant.profile", "Edited tenant profile"`. One enum member; a
`choices` change needs a migration but touches no data.

---

## 5. The tenant list

### 5.1 Columns

| Column | Source | Notes |
| :--- | :--- | :--- |
| Name, subdomain | `Tenant` | search target |
| Created | `Tenant.created_at` | default sort, newest first |
| Active | `Tenant.is_active` | see §8 before trusting this |
| Plan | active `Subscription.plan_snapshot["name"]` | **snapshot, not `plan.name`** |
| Subscription status | `Subscription.status` | `active` / `trialing` / `expired` / none |
| Cycle ends | `Subscription.end_date` | plus `days_remaining`, computed client-side |
| Scheduled change | `Subscription.scheduled_plan_id is not None` | a boolean pip is enough here |
| Balance | `Wallet.balance` | |
| Frozen | `Wallet.is_frozen` | |
| Enforcement | `TenantSettings.credits_enforced` / `entitlements_enforced` | two pips |
| Last call | most recent `AgentCallLog.started_at` | the "is this account alive?" column |

### 5.2 Annotate, do not loop — and use `Subquery`, not `Count`

The umbrella plan says *"Annotate, do not loop. A naive implementation is 5 queries per
row."* That is right, but the obvious annotation is also wrong here.

`ops.services.plan.config.annotated_queryset()` gets away with `Count(..., distinct=True)`
because it only ever counts. This list needs **scalar values from a specific related row**
— the balance from *the* wallet, the plan name from *the active* subscription — and
joining four one-to-many relations in one `SELECT` multiplies rows before any aggregate
sees them.

**Every per-tenant scalar is a correlated `Subquery`.** They cost no extra round trips —
they are part of the one statement — and none of them can multiply a row:

```python
def annotated_queryset():
    live = Subscription.objects.filter(
        tenant=OuterRef("pk"), status=Status.ACTIVE, is_deleted=False,
    )
    # The lapsed fallback, mirroring `subscriptions.current_or_expired`. Without it an
    # expired tenant annotates as NULL and lands under `status=none` -- filing "cycle
    # ended" and "never signed up" under one value, when they are the two different
    # problems the filter exists to tell apart.
    lapsed = (
        Subscription.objects.filter(tenant=OuterRef("pk"))
        .exclude(status=Status.CANCELLED)
        .order_by("-end_date")
    )

    def current(field):
        return Coalesce(
            Subquery(live.values(field)[:1]), Subquery(lapsed.values(field)[:1])
        )

    wallet = Wallet.objects.filter(tenant=OuterRef("pk"), is_deleted=False)
    settings_row = TenantSettings.objects.filter(tenant=OuterRef("pk"), is_deleted=False)
    last_call = AgentCallLog.objects.filter(tenant=OuterRef("pk")).order_by("-started_at")

    return Tenant.objects.filter(is_deleted=False).annotate(
        plan_name=Subquery(live.values("plan_snapshot__name")[:1]),
        plan_code=Subquery(live.values("plan_snapshot__code")[:1]),
        subscription_status=Subquery(live.values("status")[:1]),
        cycle_ends=Subquery(live.values("end_date")[:1]),
        has_scheduled_change=Exists(live.filter(scheduled_plan__isnull=False)),
        balance=Subquery(wallet.values("balance")[:1]),
        wallet_frozen=Subquery(wallet.values("is_frozen")[:1]),
        credits_enforced=Subquery(settings_row.values("credits_enforced")[:1]),
        entitlements_enforced=Subquery(settings_row.values("entitlements_enforced")[:1]),
        last_call_at=Subquery(last_call.values("started_at")[:1]),
    )
```

Two notes that are easy to get wrong:

**`plan_snapshot__name`, not `plan__name`.** The snapshot is the contract. A tenant on a
plan that was renamed last week is still on what they bought, and the list must agree
with the 360 — which reads the snapshot — or support will chase a discrepancy that is not
one. `plan_snapshot` is nullable, so the column falls back to the live plan's name in the
serializer when the subquery yields `None`.

**`TenantSettings.for_tenant()` must not be used here.** It is a `get_or_create` — calling
it while rendering a list would write a settings row for every tenant that has not needed
one yet, on a `GET`. The subquery reads, and the serializer supplies the model defaults
(`False`, `False`) when the row is absent. Same for `Wallet.for_tenant()`.

### 5.3 Filters, search, sort

Search is one `q` param across `name`, `subdomain`, and owner email — the three things a
support agent has in front of them when a ticket arrives.

```
?q=            icontains on name / subdomain / user_mappings__user__email  (distinct)
?plan=         plan code, matched on the live subscription
?status=       active | trialing | expired | none      (none = no subscription at all)
?enforcement=  credits | entitlements | both | neither
?frozen=       true | false      (wallet)
?is_active=    true | false      (tenant)
?ordering=     -created_at (default) | created_at | balance | -balance | last_call_at | name
```

`status=none` is the filter that finds the interesting rows: a tenant with no subscription
is either mid-onboarding or a payment that never completed, and neither shows up under any
other value.

> ⚠️ **`exclude()` on a subquery annotation drops the NULL rows too** — found by the
> tests, not by reading. A tenant with no wallet annotates `wallet_frozen = NULL`, and
> `.exclude(wallet_frozen=True)` compiles to `NOT (x = true)`, which is `NULL` for a
> `NULL` and therefore never `TRUE`. Those tenants vanished from `?frozen=false` and
> `?enforcement=neither` — the exact half-onboarded accounts the filter is for. Both use
> an explicit `Q(x=False) | Q(x__isnull=True)` instead.

The `?q` join through `user_mappings` is the one place a row can duplicate — a tenant with
three users matching the term appears three times. `.distinct()` on that branch only;
applying it unconditionally forces a `DISTINCT` over the whole annotated select for every
request that does not need it.

### 5.4 Pagination

Reuse `SetPagination` from `utils/views/base_model_viewset.py` — it already emits the
project envelope with a `meta` block, so the list matches every other paginated endpoint
without a second response shape.

Module 1's plan list is deliberately unpaginated (there are five plans). **This one must
paginate**, and `page_size` is capped at 100 by `SetPagination.max_page_size`.

### 5.5 Query budget: **2, at any page size**

One statement for the page, one for the pagination count. That is the number, and it is
the whole point of §5.2.

Following the convention in `wallet/tests/test_query_budget.py`: *"Every budget is an
exact number, not a ceiling. A budget that only fails when it doubles is a budget nobody
notices breaking."* The test creates 25 tenants, asserts 2 queries, then creates 25 more
and asserts 2 again — a per-row leak fails the second assertion even if somebody "fixed"
the first by raising the number.

---

## 6. The 360 payload

One request, everything about one tenant. The rationale from Module 1's editor applies:
three requests to render one screen is also three chances to render it half-loaded.

```jsonc
{
  "tenant":       { "id", "name", "subdomain", "phone_number", "created_at",
                    "is_active", "timezone", "language",
                    "referred_by_code", "tenant_referral_code" },
  "enforcement":  { "credits_enforced", "entitlements_enforced" },
  "owner":        { "id", "email", "name", "last_login", "is_verified",
                    "derived": "first_user", "user_count" },
  "subscription": { "id", "status", "plan": { /* snapshot */ }, "start_date", "end_date",
                    "days_remaining", "is_expired", "scheduled_change",
                    "superseded_by", "rules" },
  "wallet":       { "exists", "balance", "allocated_balance", "purchased_balance",
                    "lifetime_granted", "lifetime_consumed",
                    "is_frozen", "frozen_reason", "frozen_at",
                    "low_balance_threshold", "rate_snapshot", "rate_snapshot_at" },
  "entitlements": [ /* 7 rows: resource, included, additional, limit, used, remaining,
                       recurring_charge */ ],
  "payments":     [ /* last 10 Transactions */ ],
  "ledger":       [ /* last 20 CreditLedger rows, with the rating that produced each */ ],
  "usage":        { "calls_30d", "minutes_30d", "credits_burned_30d",
                    "campaigns_active", "by_tier": { } },
  "health":       { "ok", "checks": [ ] }
}
```

### 6.1 The blocks, and what each is actually read from

**`subscription`** — `subscriptions.current_or_expired(tenant)`, not `current_for`. A
tenant whose cycle ended still has a relationship with us, and the lapsed row is exactly
what support needs when the ticket is *"why can't I make calls?"*. Cancelled rows are
excluded by that helper because they were superseded by a plan change; `superseded_by`
makes the chain navigable, which the service docstring notes is the first thing support
asks for in a rollover dispute.

`plan` in this block is the **snapshot**, with the live plan only as a fallback for
pre-snapshot rows. `rules` comes from `Subscription.rules`, never from `self.plan` — that
property exists precisely so the freeze guarantee does not depend on the discipline of the
last person to touch a caller.

**`wallet`** — read directly, not through `Wallet.for_tenant()` (`get_or_create` again). A
tenant with no wallet row serialises as an empty wallet with `exists: false`, which is
what the model's own docstring says the absent row means.

`rate_snapshot` is included in full. It is the answer to *"what does a minute cost them?"*
and it is not derivable from anything else on the screen — the live `CreditRate` rows may
have been superseded since the card was stamped.

**`entitlements`** — `entitlements.summary(tenant_id, subscription, with_usage=True)`,
unchanged. Its docstring says `with_usage` is *"fine for a settings page, not for a hot
path"*; a 360 opened by a human for one tenant is a settings page. It is 1 query for the
`TenantEntitlement` extras plus 1 per resource = **8**.

If there is no subscription at all, `summary` cannot be called — it dereferences
`subscription.included_entitlement`. The block degrades to `[]` with an explicit
`"reason": "no_subscription"` rather than raising, because a tenant stuck mid-onboarding
is one of the cases this screen exists for.

**`payments`** — last 10 `Transaction` rows for the tenant, newest first, with `status`,
`transaction_type`, `amount`, `razorpay_order_id`, `razorpay_payment_id`, `created_at` and
`amount_breakdown`. The breakdown is included because, per the model, it is *"the only
record of how the number was produced"* when a prorated charge is disputed.

> Onboarding transactions have `tenant = NULL` by design — the tenant does not exist until
> the payment clears, and a check constraint enforces it. They are reachable through
> `temp_user` only, so **the tenant's first payment will not appear in this list.** Say so
> on screen rather than silently showing nine payments where the customer counted ten.

**`ledger`** — last 20 `CreditLedger` rows with `entry_type`, `bucket`, `amount`,
`balance_after`, and the rating that produced each (`operation`, `raw_quantity`, `units`,
`unit_type`, `rate_applied`, `reference_id`, `notes`), `select_related("subscription")`.
Read from `all_objects`, not `objects` — the ledger is append-only and a row that has
somehow been soft-deleted is the single most important row on the screen.

**`usage`** — 30 days, three rollups:

| Metric | Query |
| :--- | :--- |
| `calls_30d`, `minutes_30d`, `by_tier` | one `AgentCallLog` aggregate, `values("service_tier").annotate(Count, Sum("duration_seconds"))` |
| `credits_burned_30d` | one `CreditLedger` aggregate over `USAGE_DEDUCTION` + `USAGE_ADJUSTMENT`, `Sum("amount")` (negative; negate for display) |
| `campaigns_active` | one `Campaign` count, reusing `entitlements._campaign_consumes_allowance()` so the console's idea of "active" is the same one the allowance uses |

That last reuse matters. If the console counts campaigns differently from the allowance
checker, the 360 will show 4 active campaigns beside an entitlement row reading `5 / 5`,
and support will file a bug against the wrong system. Promote the helper out of its
underscore name as part of this step.

### 6.2 `health` — the block worth building carefully

Four checks. Each returns `{code, ok, detail}` and the block is `ok` when all are.

| Check | Rule | Where the logic is today |
| :--- | :--- | :--- |
| `WALLET_RECONCILES` | `SUM(CreditLedger.amount)` per bucket == the wallet's buckets, and their sum == `balance` | ❌ inline in `reconcile_wallets.handle()` |
| `NO_UNRATED_CALLS` | no completed call older than the 15-minute grace with no `USAGE_DEDUCTION` against its `provider_call_id` | ❌ inline in `report_unrated_calls()` |
| `NO_FAILED_PAYMENTS` | no `Transaction` in `failed` / `underpaid` in the last 30 days | — trivial |
| `LAPSE_CONSISTENT` | if the subscription is expired, the wallet is frozen; if it is active, the wallet is not frozen for `subscription_expired` | — new |

The first two need extraction before they can be reused:

**`wallet/services/reconciliation.py`** — `check(wallet, *, sums=None) -> list[problem]`,
holding the per-wallet comparison currently written out in the command's `handle()`. The
command then loops over its two-query bulk `totals` map and calls this per wallet, passing
`sums` so it keeps its *"two queries for the whole table, not three per wallet"* property.
The console calls it for one wallet and lets it do its own aggregate.

**`wallet/tasks/unrated_calls.py`** — extract `unrated_for(tenant_id=None, *, hours=24)`
returning the offending rows; the task keeps the logging and the return count, the console
calls it with a `tenant_id`. `GRACE_MINUTES` stays where it is and is honoured by both.

> **Extract, do not reimplement.** A second copy of the reconciliation rule is worse than
> no console check at all: two answers to *"does this balance add up?"* means the one on
> screen and the one in the nightly job can disagree, and no operator will know which to
> believe.

`LAPSE_CONSISTENT` is new and has never existed anywhere. It is cheap — both facts are
already loaded for other blocks — and it catches the one failure that produces a furious
customer: a subscription that expired while `freeze()` did not run, so the tenant kept
spending credits they no longer had a cycle for.

### 6.3 Query budget: **24, and that is fine** ✅ measured

| Block | Queries |
| :--- | ---: |
| tenant (`select_related` timezone, language) | 1 |
| owner + user count | 2 |
| settings | 1 |
| subscription (`select_related` plan, scheduled_plan) | 1 |
| wallet (`select_related` current_plan) | 1 |
| entitlements — extras + 7 live counts | 8 |
| payments | 1 |
| ledger | 1 |
| usage — calls, credits, campaigns | 3 |
| health — reconcile, unrated (2), failed payments | 4 |
| **total** | **24** |

Plus **one** that every authenticated request in the project pays: `config.middleware`
resolves `request.user.tenant_mapping`, which a platform operator never has. It is not
this module's to fix, but a budget that quietly excludes it should say so — there is a
test asserting it is exactly one.

**This is one screen, for one tenant, opened by a human.** Twenty-four queries is the
honest cost of assembling eight sources without lying about any of them, and cutting it
would mean caching something that has to be current.

What is *not* acceptable is any of this running per row in the list — which is why §5
exists, and why the `health` block is not a list column no matter how much somebody wants
it to be. The umbrella plan flags the same trap for "tenants near a limit" in its §4.6.

Pin the exact number in a query-budget test once built, per the convention.

---

## 7. Where the code goes

```
ops/
  urls/
    tenant.py                    # router registration
    __init__.py                  # + include("ops.urls.tenant")
  serializers/
    tenant/
      __init__.py
      listing.py                 # one list row, reading the §5.2 annotations
      profile.py                 # PATCH: name, timezone, language
      enforcement.py             # the two flags
      detail.py                  # the 360 envelope, composing the blocks below
      blocks.py                  # subscription / wallet / payment / ledger row shapes
  services/
    tenant/
      __init__.py
      listing.py                 # annotated_queryset() + filter/search/sort application
      detail.py                  # assemble() -- the 360, one function per block
      usage.py                   # the three 30-day rollups
      health.py                  # the four checks
      lifecycle.py               # suspend / restore  (§8: Milestone 3)
  views/
    tenant.py                    # TenantConsoleViewSet
  tests/
    test_tenant_list.py
    test_tenant_detail.py
    test_tenant_health.py
    test_tenant_writes.py        # permissions, reason, audit rows
    test_tenant_query_budget.py
```

Plus, outside `ops`, the extractions from §6.2:

```
wallet/services/reconciliation.py       new       -- check(wallet, sums=None)
wallet/tasks/unrated_calls.py           refactor  -- unrated_for(tenant_id, hours)
onboard/services/entitlements.py        refactor  -- promote _campaign_consumes_allowance
user/models/operator_audit_log.py       + Action.TENANT_PROFILE
```

Same reasoning as Module 1's §6, and the same rule: **`ops` owns no models.** The one enum
member goes in `user`, where the model lives. The three files outside `ops` are refactors
that leave their existing callers behind unchanged interfaces.

`health` is a service and not a view helper for the same reason `readiness` is: it has
three callers on the horizon — the 360, the standalone `health/` endpoint, and Milestone
8's platform-wide sweep. Three callers is the point at which a private function in a view
becomes a bug waiting to be duplicated.

---

## 8. ⚠️ `suspend` does not currently suspend anything

`Tenant.is_active` comes from `BaseModelCore` and defaults to `True`. **Nothing reads it.**
A repo-wide search for `tenant.is_active` / `tenant__is_active` outside test fixtures
returns nothing:

* `TenantBaseViewSet.get_queryset()` filters on the mapping, never on the tenant's flag
* the login path checks `CustomUser.is_active`, a different flag on a different model
* `wallet.services.wallet.assert_can_consume` gates on the wallet's freeze state
* `entitlements.is_enforced` gates on `TenantSettings`

So `POST /suspend/` as specified in the umbrella plan would set a field, write a convincing
audit row, return `200`, and change nothing. The tenant's users keep logging in and their
agents keep taking calls.

**This is worse than not shipping it.** An operator who believes they have suspended a
fraudulent account, and has a green audit row proving it, will not check again.

| | Option | Cost | Consequence |
| :--- | :--- | :--- | :--- |
| **A** | **Ship `suspend` in Milestone 3, not here** *(recommended)* | — | Milestone 2 stays reads-plus-settings, which is what it was scoped as. Suspension lands with the other state-changing actions, where it belongs |
| B | Make `is_active` real now: authentication check + `assert_can_consume` check | medium | Touches the login path and the call path — two of the highest-risk surfaces in the system — inside a milestone whose value is read-only |
| C | Ship it as `is_active` only, documented as cosmetic | small | Rejected. An audit trail that records something that did not happen is the one thing `OperatorAuditLog` must never contain |

**Recommendation: A.** `PATCH` and `enforcement/` ship here — both write fields that are
already read by working code, so they are honest the day they land. `suspend` and
`restore` move to Milestone 3 with an explicit prerequisite: *make `Tenant.is_active` deny
authentication and deny consumption, then expose the button.*

---

## 9. Decisions needed before building

### 9.1 What is a tenant's "owner"? — **must be decided**

The umbrella plan's 360 payload has an `owner` block. **There is no owner.**
`TenantUserMapping` is a bare `OneToOneField(user)` + `ForeignKey(tenant)` with no role, no
flag, no ordering. `create_tenant_and_mapping()` creates the tenant and maps the signup
user — and every user added later gets an identical row.

| | Option | Cost |
| :--- | :--- | :--- |
| **A** | **Earliest `TenantUserMapping` by `created_at`** *(recommended)* | none |
| B | Add `TenantUserMapping.is_owner`, backfill to the earliest, enforce one per tenant | small migration |
| C | Rename the block to `primary_contact` and use A | none |

**Recommendation: A, labelled `owner` in the payload with `"derived": "first_user"`.** It
is correct for every tenant created through the signup flow, which is all of them, and it
costs nothing. Add the field (option B) when something other than a console screen needs
to *depend* on the answer — an ownership transfer, or billing-contact routing. Ship
`user_count` alongside so the screen can say "1 of 4 users".

### 9.2 Does the tenant list show soft-deleted tenants? — **recommended: no, with a filter**

`Tenant.objects` already excludes them. Default to that; add `?include_deleted=true` for
the case the console exists to serve — *"this tenant says their account vanished."*
Deleted rows render greyed with `deleted_at`, and every write action is refused on them.

### 9.3 `bonus_days_applied` is not a field — **drop it or derive it**

The umbrella plan's payload lists `subscription.bonus_days_applied`. No such field exists.
Bonus days are computed by `pricing.bonus_days_for()`, passed into
`subscriptions.activate(bonus_days=...)` where they extend `end_date`, and survive only
inside the upgrade order's `Transaction.amount_breakdown["bonus_days"]`.

**Recommendation: derive it.** The `payments` block already carries `amount_breakdown`;
surface `bonus_days` on the payment row that caused the current cycle rather than
pretending the subscription records it. Anything else means a new field written at
activation time, which is Milestone 3+ work at best.

### 9.4 Is the 360 cached? — **recommended: no**

Support reads it while a customer is on the phone. A 30-second cache turns "I granted you
credits, refresh" into a bug report. Twenty-three queries against indexed columns for one
tenant is not the bottleneck this system has.

### 9.5 Does opening a 360 write an audit row? — **recommended: no**

`OperatorAuditLog` records *actions*. Logging reads would bury eleven action types under
thousands of page views and make the table's `tenant` / `-created_at` index useless for the
question it exists to answer. Revisit only if access logging becomes a compliance
requirement, at which point it is a separate table.

---

## 10. Validation and error shapes

Reuse Module 1's mechanics exactly — `ResponseMixin.success` / `.error`, the
`_require_reason()` guard, `_integrity_error()` for constraint violations the serializers
did not anticipate.

| Case | Response |
| :--- | :--- |
| Unknown tenant id | `404`, `get_object_or_404(Tenant, pk=..., is_deleted=False)` |
| Not platform staff | `403`, `IsPlatformStaff.message` |
| Staff without the action | `403`, `HasConsolePermission.message` |
| Write with no `reason` | `400`, `["REASON_REQUIRED"]` |
| `PATCH` to a non-existent timezone/language FK | `400` field error, not a `500` |
| Tenant exists but has no subscription | `200` — `subscription: null`, `entitlements: []` with a reason |
| Tenant exists but has no wallet | `200` — zeroed wallet block, `exists: false` |

The last two are the important ones. **A tenant with nothing configured is exactly the
tenant support is looking at**, and a 360 that 500s on the half-onboarded case is a 360
that fails when it is needed most. Every block degrades independently.

---

## 11. Work breakdown

| | Step | Delivers | Depends on |
| :--- | :--- | :--- | :--- |
| ✅ 0 | `Action.TENANT_PROFILE` enum member + migration `user.0017` | The vocabulary for §4's `PATCH` | — |
| ✅ 1 | `ops/urls/tenant.py` + `TenantConsoleViewSet` skeleton behind `IsPlatformStaff` | The namespace, one more route on the existing router | — |
| ✅ 2 | `services/tenant/listing.py` — `annotated_queryset()`, filters, search, sort | **Screen #2.** Support can find a tenant | 1 |
| ✅ 3 | `GET /tenants/` + `SetPagination` + list serializer | The list renders | 2 |
| ✅ 4 | `services/tenant/detail.py` — tenant, owner, enforcement, subscription, wallet blocks | The top half of the 360 | 1 |
| ✅ 5 | entitlements + payments + ledger blocks | The middle | 4 |
| ✅ 6 | `services/tenant/usage.py` — the three 30-day rollups | The activity block | 4 |
| ✅ 7 | `wallet/services/reconciliation.py` extraction; `reconcile_wallets` rewired to it | One reconciliation rule, two callers | — |
| ✅ 8 | `unrated_for()` extraction from the Celery task | One unrated-calls rule, two callers | — |
| ✅ 9 | `services/tenant/health.py` — four checks + `GET /tenants/{id}/health/` | **The block that makes this screen worth opening** | 7, 8 |
| ✅ 10 | `GET /tenants/{id}/` assembling everything | **Screen #3** | 4, 5, 6, 9 |
| ✅ 11 | `PATCH /tenants/{id}/` + `POST /enforcement/` — reason, permission, audit | Support can flip enforcement without an engineer | 0, 10 |
| ✅ 12 | Query-budget tests: **2** for the list at two page sizes, exact number for the 360 | The §5.5 guarantee cannot silently regress | 3, 10 |
| ✅ 13 | API docs at [`ops/tenants.md`](../../apiDocs/ops/tenants.md) | Matches `ops/plans.md` | 10, 11 |

**All thirteen shipped**, with `suspend`/`restore` held back per §8. Step 9 is what
distinguishes this from a prettier Django admin; steps 7 and 8 were its real cost.

Step 12 is not optional. A list that silently becomes 5 queries per row still *works* — it
just gets slower every month, which is the failure mode nobody catches by looking.

---

## 12. Tests

Following `ops/tests/` conventions — service tests separate from API tests, `operator()`
and `client_for()` helpers reused, `onboard/tests/billing_factories.py` for fixtures.

| File | Covers |
| :--- | :--- |
| `test_tenant_list.py` | Every filter and sort; `status=none`; `?q` across three fields without duplicate rows; soft-deleted excluded by default |
| `test_tenant_detail.py` | Each block against a known fixture; **the four degraded cases** — no subscription, no wallet, no settings row, no calls |
| `test_tenant_health.py` | Each check green and red independently: a deliberately drifted wallet, an unrated call inside and outside the grace window, a failed payment, an expired subscription with an unfrozen wallet |
| `test_tenant_writes.py` | Sub-manager without the action is refused; missing `reason` is `400`; every successful write leaves exactly one `OperatorAuditLog` row with the right `action` and `tenant` |
| `test_tenant_query_budget.py` | **2** for the list at 25 and 50 tenants; the exact number for the 360 |

Three cases worth naming explicitly, because they are the ones that will actually break:

**A tenant with two subscriptions** — one `active`, one `cancelled` by an earlier plan
change. The list must show the active one, and the 360 must show it with `superseded_by`
navigable from the old row. The partial unique constraint guarantees at most one active, so
`.first()` is not a coin flip — but the *cancelled* row is what a naive
`.order_by("-created_at").first()` picks up.

**A tenant whose settings row does not exist.** Roughly every tenant that has never had
enforcement touched. The list must not create one, and the 360 must report
`credits_enforced: false` rather than 500ing or writing.

**A tenant whose only payment is the onboarding one.** `payments` comes back empty because
that row has `tenant = NULL`. Assert the empty list, so nobody later "fixes" it by joining
through `temp_user` without noticing the check constraint that makes it null in the first
place.

Per the repo convention, run these against Postgres —
`--settings=config.settings.test_postgres`.

### What they caught

| | Found by | Fix |
| :--- | :--- | :--- |
| `exclude()` on a NULL subquery annotation dropped tenants with no wallet / no settings row from `?frozen=false` and `?enforcement=neither` | `test_tenant_list` | `Q(x=False) \| Q(x__isnull=True)` — §5.3 |
| `timedelta.days` floors, so a cycle that ended 3 days and 1 second ago reported `days_remaining: -4` | `test_tenant_detail` | truncate toward zero |
| A write on a soft-deleted tenant 404'd before the `TENANT_DELETED` guard ran — inconsistent with the read, which shows that tenant | `test_tenant_writes` | load with `all_objects`, refuse explicitly with 409 |
| The tenant was fetched without `select_related`, so the serializer lazy-loaded its FKs | `test_tenant_query_budget` | `select_related("timezone", "language")` |

**105 tests, all passing**, plus 306 in `wallet` / `core.tests.billing` /
`user.tests.test_platform_staff` and 103 in the entitlement and command suites, to cover
the three extractions in §7.

---

## 13. Decisions — summary

| | Decision | Status |
| :--- | :--- | :--- |
| Never reuse tenant-scoped viewsets | Tenant is a path parameter; services take `tenant_id` | ✅ carried from Module 1 |
| List columns | Correlated `Subquery` per scalar, never `Count` + join | ✅ §5.2 |
| List query budget | **2, at any page size**, asserted as an exact number | ✅ §5.5 — measured |
| Plan name shown | From `plan_snapshot`, falling back to the live plan | ✅ §5.2 |
| `for_tenant()` helpers on a read path | **Forbidden** — they `get_or_create` | ✅ §5.2 |
| 360 cost | **24** queries, uncached, asserted exactly | ✅ §6.3, §9.4 |
| Reconciliation + unrated-calls logic | **Extracted, not reimplemented** | ✅ §6.2 |
| Reads in the audit log | No | ✅ §9.5 |
| `suspend` / `restore` | **Deferred to Milestone 3**, behind making `is_active` real | ⚠️ §8 — needs sign-off |
| Tenant "owner" | Earliest mapping, labelled as derived | ⚠️ §9.1 — needs sign-off |
| `bonus_days_applied` | Derived from the payment's `amount_breakdown`, not a new field | ⚠️ §9.3 — needs sign-off |

**Three open items**, all in §8–§9, and all of them are questions about scope rather than
about design. Everything else is settled and buildable.

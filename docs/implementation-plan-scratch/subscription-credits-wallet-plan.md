# Subscription, Credits & Wallet Ledger — Implementation Plan

**Status:** proposal, not yet built.
**Supersedes:** `plan-upgrade-downgrade-wallet` (the untracked scratch note in this folder).

> **Split out:** the wallet, ledger, rate card and consumption path now have their own
> document — [credit-wallet-module-plan.md](credit-wallet-module-plan.md) — and it is being
> built first. Where the two disagree, **the wallet document wins on anything inside the
> wallet.** Two things here are superseded by it: `CreditLedger` moves to a new `wallet` app
> (§3.3 below), and the balance moves from `Subscription` to a tenant-level `Wallet`, which
> deletes the transfer-and-close-out step in §4.2 — a plan change becomes a *forfeit*
> decision, not a balance move. The rollover *policy* that decides the forfeit amount stays
> here. See the wallet document's §10 for the full seam.

Scope of this document: **data model, services, API surface, background jobs, and build
order** for a credit wallet with an append-only ledger, manual top-ups, and a self-hosted
subscription lifecycle engine (renew / upgrade / downgrade / expire) built on Razorpay
one-off orders.

Out of scope: Razorpay auto-pay mandates (`sub_xxx`), refunds to the payment instrument,
invoicing/PDF generation, usage-based (per-minute) rating unless §12/Q1 says otherwise,
and any change to the voice runner's own billing.

---

## 1. What exists today — verified against the repo

| Concern | Where it lives | State |
| :--- | :--- | :--- |
| Plan catalogue + pricing math | [plan.py](../../onboard/models/plan.py) | Complete. `base_price → discount → net → tax → total` as `@property`. No credit fields. |
| Subscription record | [subscription.py](../../onboard/models/subscription.py) | Exists. Has `status`, `start_date`, `end_date`, `plan_snapshot`. |
| Payment record | [transaction.py](../../onboard/models/transaction.py) | Exists. Razorpay order/payment ids, amount, status. No tenant, no type. |
| Webhook audit | [webhook_log.py](../../onboard/models/webhook_log.py) | Exists. Logs every inbound event. |
| Signup payment flow | [create_order.py](../../onboard/views/onboarding/create_order.py), [verify_payment.py](../../onboard/views/onboarding/verify_payment.py), [webhook.py](../../onboard/views/onboarding/webhook.py) | Working for first purchase only. |
| Serializers for Subscription | [subscription.py](../../onboard/serializers/subscription.py) | All four tiers exist. |
| Credits / wallet / ledger | — | **Nothing.** `grep -ri credit --include=*.py` matches one unrelated test. |

### 1.1 Seven gaps that block the feature

These are not nice-to-haves. Each one breaks the engine described below if left alone.

**G1 — `Subscription.end_date` is never written.** All three creation sites construct the
row with `tenant`, `plan`, `status` only:
[create_order.py:119](../../onboard/views/onboarding/create_order.py#L119),
[verify_payment.py:181](../../onboard/views/onboarding/verify_payment.py#L181),
[webhook.py:104](../../onboard/views/onboarding/webhook.py#L104).
Expiry, renewal windows, and proration are all functions of `end_date`. Without it there is
no billing cycle to reason about.

**G2 — subscription activation is copy-pasted three times.** Credit allocation, ledger
seeding, and snapshot freezing must happen at every activation. Pasting that into three
views guarantees they drift. One service function, three call sites.

**G3 — the webhook is onboarding-only and not idempotent.**
[webhook.py:70-119](../../onboard/views/onboarding/webhook.py#L70-L119) assumes every
captured payment is a signup: it reads `transaction.temp_user` and promotes a user. A
top-up or renewal payment hits the same handler. Worse, `RazorpayWebhookLog.event_id` has
`db_index=True` but **no unique constraint**, and the handler runs outside a transaction
and re-raises on failure ([webhook.py:135](../../onboard/views/onboarding/webhook.py#L135)),
which returns 500 to Razorpay — and Razorpay retries. Retried event + no idempotency key =
credits deposited twice.

**G4 — `RAZORPAY_WEBHOOK_SECRET` is not a setting.** It is only read as
`getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "dummy_webhook_secret")`
([webhook.py:44](../../onboard/views/onboarding/webhook.py#L44)). `config/settings/base.py`
defines `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` (lines 488-489) and nothing else. In
production every real webhook fails signature verification and 400s.

**G5 — nothing stops a tenant holding two ACTIVE subscriptions.** Upgrades create a second
row; without a constraint, "the current subscription" becomes ambiguous and the balance can
be read off the wrong one.

**G6 — no authenticated billing API surface exists.** `onboard/urls/` routes plans (public)
and the signup flow (TempUser-authenticated). There is no viewset or URL for Subscription.
The entire logged-in billing surface is greenfield.

**G7 — `plan_snapshot` freezes pricing but not credit rules.**
[subscription.py:57-72](../../onboard/models/subscription.py#L57-L72) snapshots price
fields. The new rollover flags must join it, or an admin editing a plan silently rewrites
the terms of every live subscription.

### 1.2 Blocker resolutions — decided

All seven are settled. Together they are Phase 0 (§11); nothing below Phase 1 starts until
they land.

| | Blocker | Resolution |
| :--- | :--- | :--- |
| R1 | G1 `end_date` | Service sets it; `save()` backfills nulls |
| R2 | G2 activation | Extract `activate()`, refactor all three signup sites |
| R3 | G3 webhook | Unique `event_id` + `atomic()` + return 200 |
| R4 | G4 secret | Every Razorpay default removed + explicit verify switch + deploy check |
| R5 | G5 active subs | Partial unique constraint + duplicate-resolution migration |
| R6 | G6 API | New top-level `/api/v1/billing/` prefix |
| R7 | G7 snapshot | Extend `plan_snapshot`, read snapshot-first with live fallback |

> **Deployment context: no tenant is live yet.** Confirmed at planning time — nothing has
> onboarded in production. Every data-migration step below (R3's `event_id` de-duplication,
> R5's duplicate-active resolution, `0025`'s `end_date` and snapshot backfill) therefore
> operates on an empty or near-empty table. Keep them anyway — they are cheap, and they are
> what makes the plan safe to re-run against a dev or staging database that *does* have
> junk in it — but treat them as safety nets, not as risky production surgery. No
> maintenance window is needed, and the "rehearse against a production copy" caution
> attached to R3 and R5 does not apply while this stays true.
>
> This also means **breaking changes are free right now**. Two worth taking while they cost
> nothing: see the non-null note in R1 and the tenant-required constraint in §3.4.

#### R1 — `end_date`: service sets, `save()` backfills

`subscriptions.activate()` sets `start_date` and `end_date` explicitly.
`Subscription.save()` fills `end_date` **only when it is null**, so a row created from
Django admin, a shell, or a fixture still gets a valid cycle — and a renewal that has
already extended the date is never silently recomputed.

```python
from dateutil.relativedelta import relativedelta

CYCLE_DELTAS = {
    Plan.BillingCycleChoices.MONTHLY: relativedelta(months=1),
    Plan.BillingCycleChoices.YEARLY:  relativedelta(years=1),
}
```

Calendar arithmetic, not 30 days: a subscription starting 31 Jan renews 28 Feb, and twelve
monthly cycles land on the same calendar day each year rather than drifting five days.

`python-dateutil` is installed in the venv but only **transitively** (pulled in by celery) —
it is not in `pyproject.toml`. Since this imports it directly, add it as an explicit
dependency; a transitive dep can vanish on any upstream release.

**`end_date` becomes `null=False`.** The blocker was whether any plan needs a perpetual,
never-expiring subscription. Q11 settles that none does: free plans **expire and block** just
like paid ones, and Pay As You Go **auto-renews at ₹0** rather than never expiring (wallet
document §3.7). Every subscription therefore has a cycle, and with no live rows the change
is a one-line migration.

That closes G1 permanently — no future code path can produce a subscription without a cycle,
because the database will not store one.

#### R2 — one `activate()`, three call sites

`onboard/services/subscriptions.py::activate()` replaces the inline
`Subscription.objects.create(...)` at
[create_order.py:119](../../onboard/views/onboarding/create_order.py#L119),
[verify_payment.py:181](../../onboard/views/onboarding/verify_payment.py#L181) and
[webhook.py:104](../../onboard/views/onboarding/webhook.py#L104).

Scope is subscription creation only. The ~60 lines of user promotion, tenant creation,
billing-address relinking and temp-user cleanup that `verify_payment.py` and `webhook.py`
also duplicate stay as they are — worth unifying later, but not while Phase 0 is touching
the money path. Regression net: `onboard/tests/test_onboarding.py` and
`test_subscription.py`, run under `--settings=config.settings.test_postgres`.

#### R3 — webhook: unique event, one transaction, always 200

Three changes to [webhook.py](../../onboard/views/onboarding/webhook.py):

1. `unique=True` on `RazorpayWebhookLog.event_id`, and the log row created with
   `get_or_create`. `created=False` means Razorpay is retrying an event already seen —
   return 200 immediately without re-processing. In Postgres a unique column still permits
   multiple NULLs, which is what we want: Razorpay events without an `id` fall through to
   normal processing, where the ledger's `(entry_type, reference_id)` constraint (D6) is the
   real guard.
2. Wrap processing in `atomic()`.
3. Replace `raise e` at [webhook.py:135](../../onboard/views/onboarding/webhook.py#L135)
   with: record the error on `log_entry`, log at `exception` level, return 200.

**Migration caveat:** existing `RazorpayWebhookLog` rows may already hold duplicate
`event_id`s — that is precisely what today's missing constraint permits, and finding some is
evidence a retry was double-processed in the past. The migration de-duplicates (keep
earliest, soft-delete the rest, log every one) before `AlterField`.

#### R4 — no defaults on any Razorpay credential

**Every Razorpay default is removed.** `config/settings/base.py:488-489` becomes:

```python
RAZORPAY_KEY_ID         = env.str("RAZORPAY_KEY_ID")          # no default
RAZORPAY_KEY_SECRET     = env.str("RAZORPAY_KEY_SECRET")      # no default
RAZORPAY_WEBHOOK_SECRET = env.str("RAZORPAY_WEBHOOK_SECRET")  # no default
```

This project uses `django-environ` (`environ.Env()`, base.py:11), where `env.str()` with no
default raises `ImproperlyConfigured` at import. An unset credential now stops the process
from booting instead of quietly running with a fake one.

And the fallback in the view goes too — [webhook.py:44](../../onboard/views/onboarding/webhook.py#L44)
becomes a plain `settings.RAZORPAY_WEBHOOK_SECRET`. A `getattr` default in the code path is
the same bug wearing a different hat.

##### The dummy key is a bigger hole than the missing secret

`RAZORPAY_KEY_ID` defaulted to `"rzp_test_dummykey"`, and both
[webhook.py:41-42](../../onboard/views/onboarding/webhook.py#L41-L42) and
[verify_payment.py](../../onboard/views/onboarding/verify_payment.py) treat that exact value
as *"skip signature verification, mark valid"*. A production deploy that forgot the env var
did not fail — it silently accepted **any** unsigned webhook as genuine. Anyone able to POST
to the endpoint could mint a paid subscription.

Removing the default fixes the accident, but the mechanism is still wrong: whether
signatures are verified should not be inferred from what a credential happens to equal.
Replace it with an explicit switch:

```python
# base.py — defaults to the SAFE value. A default that fails closed is fine;
# a default that fails open is what caused this.
RAZORPAY_VERIFY_SIGNATURES = env.bool("RAZORPAY_VERIFY_SIGNATURES", default=True)
```

Test settings opt out deliberately (`config/settings/test_postgres.py`, `local.py`), and
both views check the flag instead of comparing key strings. The existing
`"test" in sys.argv` branch stays as a second guard.

##### The check that remains

A new `onboard/checks.py`, modelled on [core/checks.py](../../core/checks.py) and registered
from `OnboardConfig.ready()` ([onboard/apps.py](../../onboard/apps.py)) the way
`CoreConfig.ready()` imports `core.checks`:

- `onboard.E001` — `RAZORPAY_VERIFY_SIGNATURES` is False while `DEBUG` is False. With the
  defaults gone this is the only remaining way to reach production with verification off,
  and it now requires someone to have set it explicitly.

`manage.py check` runs in CI and before every `migrate`, so it fails at deploy rather than at
the first payment.

##### Operational consequence — read this before merging

With no defaults, **Django will not start anywhere the three variables are absent** —
including local dev, CI, and any developer's first clone. That is the point, but it has to be
rolled out deliberately:

- Add all four variables to `.env` / `.env.local`, the `.env` sample, `docker-config/`, and
  the CI environment **before** the change merges.
- `config/settings/test_postgres.py` does `from .base import *`, so base raises before test
  settings can patch anything. The test environment needs the variables present, with
  `RAZORPAY_VERIFY_SIGNATURES=False`.
- A dummy value in a local `.env` is fine. The thing that was dangerous was never "a dummy
  value exists" — it was "production silently inherits one from source code". Config files
  are not inherited by a production deploy; a hardcoded default is.

Also document all four in the deployment checklist.

#### R5 — partial unique constraint, with the duplicates resolved first

```python
UniqueConstraint(fields=["tenant"], name="uniq_active_subscription_per_tenant",
                 condition=Q(status="active", is_deleted=False))
```

The `is_deleted=False` clause matters: a soft-deleted active row must not block a legitimate
new subscription.

Preceding `RunPython`: for each tenant holding more than one ACTIVE subscription, keep the
latest `start_date` (tie-break on `created_at`), set the others to `EXPIRED` with
`canceled_at = now`, and log every row it touches. Processes zero rows in production today
(§1.2), but it must stay safe to re-run against a dev or staging database that has
accumulated duplicates.

The constraint also makes an ordering requirement out of what would otherwise be style:
`apply_plan_change()` (§4.2) must close the old subscription *before* inserting the new one.
The constraint is checked per statement, not deferred, so insert-then-close fails.

##### What the constraint forces the product to do

"One active subscription per tenant" is not only a database rule — it decides *when* a plan
change is allowed to take effect, because two subscriptions can never coexist:

| | Timing | Why |
| :--- | :--- | :--- |
| **Upgrade** | **Immediately**, the moment the prorated order is paid | They have paid for the new plan; the old one closes in the same transaction and the new one opens. One active row throughout. |
| **Downgrade** | **At the end of the current cycle** | They already paid for the cycle they are in. Ending it early would mean either refunding cash or taking something they paid for. Neither is acceptable, so the change waits. |

This is D3 stated as a rule rather than as arithmetic. The two agree: the prorated amount is
positive exactly when the tenant is moving up and owes money, and zero or negative exactly
when they are moving down and are owed nothing. Branching on the sign (D3) rather than on a
tier ladder is what makes monthly↔yearly and same-price sidegrades fall out correctly, but
the user-visible promise is the simple one in this table, and it is what the UI should say.

Two consequences worth being explicit about:

- A scheduled downgrade is **stored, not applied** — `scheduled_plan` and
  `scheduled_change_at` on the existing row (§3.2). No second subscription is created until
  `process_billing_cycle` (§7) executes it, so the constraint is never even approached.
- A tenant who schedules a downgrade and then upgrades before it fires must have the
  scheduled change **cleared** as part of the upgrade. Otherwise the downgrade fires later
  and silently undoes a change they just paid for.

#### R6 — `/api/v1/billing/`

New `onboard/urls/billing.py`, wired into `api_v1_patterns` in
[config/urls.py](../../config/urls.py):

```python
path("billing/", include("onboard.urls.billing")),
```

Note that [onboard/urls/__init__.py](../../onboard/urls/__init__.py) splats every url module
into one flat list under `/api/v1/onboard/`. The billing module must **not** be added there
— that is what would put an authenticated billing surface behind the signup prefix. Swagger
tag: `Billing & Credits`.

#### R7 — snapshot the rules, read snapshot-first

`generate_plan_snapshot()` gains `allocated_credits`, `cost_per_additional_credit`,
`max_rollover_credits`, the four flags and `low_balance_threshold`. Services read through a
`Subscription.rules` property that prefers the snapshot and falls back to the live Plan per
missing key — which is exactly what pre-migration rows look like.

**One trap:** `save()` only generates a snapshot when `plan_snapshot` is falsy
([subscription.py:74-77](../../onboard/models/subscription.py#L74-L77)). Existing rows
already have one, so re-saving them changes nothing. Migration `0025` must call
`generate_plan_snapshot()` explicitly.

---

## 2. Design decisions

Ten decisions that everything downstream depends on. Where this differs from the original
scratch note, the difference is called out.

### D1 — The ledger is the truth; `credit_balance` is a cache

Every credit movement is a `CreditLedger` row. `Subscription.credit_balance` exists only so
a permission check is one indexed read instead of a `SUM()`. The invariant is:

```
SUM(CreditLedger.amount WHERE subscription = S) == S.credit_balance
```

Enforced by: a single writer service (§4.1), `select_for_update()` on the subscription row
around every mutation, and a reconciliation command that reports drift (§7).

### D2 — The ledger lives in `onboard`, not `tenant` — *differs from the scratch note*

The scratch note put it at `tenant/models/credit_ledger.py`. Recommend
`onboard/models/credit_ledger.py` instead:

- Plan, Subscription and Transaction all live in `onboard`. A ledger row is meaningless
  without a subscription; splitting the aggregate across apps buys nothing.
- `tenant` currently holds identity only — Tenant, mapping, settings, email config
  (`tenant/models/__init__.py`). It is imported *by* `onboard`, not the reverse. Putting a
  billing model there inverts the dependency.
- The model is still tenant-scoped: it extends `TenantBaseModelCore`, exactly as AGENTS.md
  requires. App location and tenant scoping are independent choices.

URL prefix is a separate question — see §5.

### D3 — Upgrade vs downgrade is decided by money owed, not by tier order

Do not rank plans by `sort_order` and call the higher one an upgrade. Compute the prorated
amount due (§4.3) and branch on its sign:

- `amount_due > 0` → **paid change**, takes effect when the order is paid.
- `amount_due <= 0` → **scheduled change**, takes effect at `end_date`. No refund, no cash out.

This handles monthly→yearly, yearly→monthly, and same-price sidegrades without a tier
ladder, and it makes "we never refund money" structural rather than remembered.

### D4 — Two credit buckets: allocated and purchased

The scratch note has one balance and one set of rollover flags. That makes topped-up credits
— which the tenant paid cash for, mid-cycle — evaporate at renewal whenever
`allow_rollover_on_renewal` is false. That is a chargeback conversation.

Recommend tracking `allocated_balance` and `purchased_balance` on the subscription, where
rollover flags govern the allocated bucket only and purchased credits always carry forward.
`credit_balance` stays as the sum for fast reads and API compatibility. Consumption drains
**allocated first**, so the cash-bought bucket is what survives.

If §12/Q2 comes back as "one bucket", drop the two fields and keep `credit_balance`; nothing
else in this plan changes.

### D5 — Lifecycle rules are read from the frozen snapshot

Extend `generate_plan_snapshot()` with `allocated_credits`, `cost_per_additional_credit`,
and the four flags. Services read rules via `subscription.rules` (a helper that reads the
snapshot and falls back to the live plan when a key is absent, which is what pre-migration
rows will look like). An admin editing a plan then changes terms for *future* cycles only.

### D6 — Ledger writes are idempotent by construction

Every row carries `(entry_type, reference_id)` under a unique constraint. The reference is
whatever real-world event caused it:

| Entry | `reference_id` |
| :--- | :--- |
| `USAGE_DEDUCTION` | `AgentCallLog.provider_call_id` (already `unique=True`) |
| `MANUAL_TOP_UP` | `Transaction.razorpay_order_id` |
| `INITIAL_PURCHASE`, `RENEWAL_ALLOCATION` | `Transaction.razorpay_order_id` |
| `*_ROLLOVER`, `EXPIRY_FORFEIT` | source `Subscription.id` |
| `ADMIN_ADJUSTMENT` | staff-supplied ticket ref |

A replayed webhook, a retried runner POST, or a double-clicked button hits the constraint
and is swallowed as a no-op. This is the single most important defence in the design.

### D7 — Three entry types the scratch note is missing

- **`RENEWAL_ROLLOVER`** — `allow_rollover_on_renewal` exists as a flag but the note's entry
  list has rollover types for upgrade and downgrade only. A same-plan renewal needs its own
  type or the audit trail lies about what happened.
- **`ADMIN_ADJUSTMENT`** — support *will* need to grant or claw back credits. Without a
  sanctioned path, someone edits `credit_balance` in Django admin and the invariant in D1 is
  gone within a month.
- **`OPENING_BALANCE`** — the backfill migration (§3.6) needs a row type, otherwise existing
  subscriptions start life already in violation of D1.

### D8 — Consumption is authorised before the call and deducted after it

Two distinct operations, and conflating them is why credit systems leak:

- **Authorise** (`wallet.assert_can_consume`) — cheap pre-flight, called before a campaign
  starts and when the runner pulls campaign context. Answers "does this tenant have budget?"
- **Deduct** (`wallet.consume`) — called when the call log lands at
  [agent_call_log.py:189](../../core/views/agent_call_log.py#L189), keyed on
  `provider_call_id`. Idempotent per D6.

The runner is an external service; it can fail, retry, and post the same call twice. Only
the post-facto keyed deduction is safe to be the source of truth.

### D9 — Reuse the existing webhook endpoint, dispatch on transaction type

Razorpay is configured with one webhook URL. Rather than register a second, keep
`/api/v1/onboard/webhook/` and dispatch inside it on `Transaction.transaction_type`
(§6). This also forces the G3 fixes to be made once, for every flow.

### D10 — Metering ships before blocking

Deducting credits and *refusing service* are separated by a flag, and the flag starts off.

Add `credits_enforced = BooleanField(default=False)` to
[TenantSettings](../../tenant/models/TenantSettings.py) — a model that exists precisely so
"the next setting is a field rather than a table", and whose `for_tenant()` accessor treats
a missing row as all-defaults rather than an error, which is what a check on the call path
needs.

- `wallet.consume` always runs. Ledger rows are written, balances move, history accrues.
- `wallet.assert_can_consume` raises only when `credits_enforced` is true for that tenant.
  Otherwise it logs what *would* have been refused, at warning level, and returns.

This buys a real answer to the question nobody can answer from a spreadsheet: **is one
credit per call the right price?** Run a full cycle of genuine traffic, compare ledger
burn against what tenants actually paid, then flip the flag. The alternative is discovering
the cost is wrong by way of a paying customer's campaign refusing to start.

The flag is per-tenant, so it doubles as the rollout control and as a support escape hatch —
a tenant caught by a billing bug can be unblocked in one field edit while the bug is fixed,
without disabling metering for anyone else. Removing the flag once enforcement is universal
is a one-line migration; leaving it in is also defensible.

---

## 3. Data model

### 3.1 `Plan` additions — [onboard/models/plan.py](../../onboard/models/plan.py)

```python
allocated_credits = models.PositiveIntegerField(
    default=0,
    help_text="Credits granted at the start of every billing cycle on this plan.",
)
cost_per_additional_credit = models.DecimalField(
    max_digits=10, decimal_places=2, default=Decimal("0.00"),
    help_text="Pre-tax price of one credit when topping up mid-cycle. 0 disables top-ups.",
)
max_rollover_credits = models.PositiveIntegerField(
    null=True, blank=True,
    help_text="Cap on credits carried into the next cycle. Null means uncapped.",
)
allow_rollover_on_renewal = models.BooleanField(default=False)
allow_rollover_on_upgrade = models.BooleanField(default=True)
allow_rollover_on_downgrade = models.BooleanField(default=False)
allow_use_after_expiry = models.BooleanField(default=False)
low_balance_threshold = models.PositiveIntegerField(
    default=0,
    help_text="Notify the tenant when the balance falls to or below this. 0 disables.",
)
```

`max_rollover_credits` is not in the scratch note and is worth having on day one:
uncapped rollover on an annual plan lets a light user bank a year of credits and then run
them down while paying nothing. Adding the column later is easy; adding it after tenants
have banked balances is a policy fight.

Add a `credit_unit` marker only if §12/Q1 resolves to per-minute billing.

### 3.2 `Subscription` additions — [onboard/models/subscription.py](../../onboard/models/subscription.py)

```python
credit_balance      = PositiveIntegerField(default=0)   # cache of the ledger sum (D1)
allocated_balance   = PositiveIntegerField(default=0)   # bucket A (D4)
purchased_balance   = PositiveIntegerField(default=0)   # bucket B (D4)
canceled_at         = DateTimeField(null=True, blank=True)
scheduled_plan      = FK("onboard.Plan", null=True, blank=True, on_delete=PROTECT,
                         related_name="scheduled_subscriptions")
scheduled_change_at = DateTimeField(null=True, blank=True)
superseded_by       = FK("self", null=True, blank=True, on_delete=SET_NULL,
                         related_name="supersedes")
```

`superseded_by` makes the chain old→new navigable, which is what support will actually ask
for when a tenant disputes a rollover.

Behaviour changes on the model:

- `save()` sets `end_date` from `start_date + billing_cycle` when it is null (fixes G1).
- `generate_plan_snapshot()` gains the credit keys (D5).
- New `rules` property → dict, snapshot-first with live-plan fallback.
- New `is_spendable` property → `status == ACTIVE`, or `status == EXPIRED and
  rules["allow_use_after_expiry"]`.

Constraints:

```python
UniqueConstraint(fields=["tenant"], name="uniq_active_subscription_per_tenant",
                 condition=Q(status="active", is_deleted=False))          # fixes G5
CheckConstraint(condition=Q(credit_balance__gte=0), name="ck_subscription_balance_non_negative")
```

### 3.3 `CreditLedger` — new, `onboard/models/credit_ledger.py`

Extends `TenantBaseModelCore` (AGENTS.md Tier 1 — these rows are system-written; the
optional `performed_by` below covers the one case a human causes one).

```python
class CreditLedger(TenantBaseModelCore):
    class EntryType(models.TextChoices):
        OPENING_BALANCE      = "opening_balance",      "Opening Balance"
        INITIAL_PURCHASE     = "initial_purchase",     "Initial Purchase"
        RENEWAL_ALLOCATION   = "renewal_allocation",   "Renewal Allocation"
        RENEWAL_ROLLOVER     = "renewal_rollover",     "Renewal Rollover"
        UPGRADE_ROLLOVER     = "upgrade_rollover",     "Upgrade Rollover"
        DOWNGRADE_ROLLOVER   = "downgrade_rollover",   "Downgrade Rollover"
        MANUAL_TOP_UP        = "manual_top_up",        "Manual Top-Up"
        USAGE_DEDUCTION      = "usage_deduction",      "Usage Deduction"
        USAGE_REFUND         = "usage_refund",         "Usage Refund"
        EXPIRY_FORFEIT       = "expiry_forfeit",       "Expiry Forfeit"
        ROLLOVER_CAP_FORFEIT = "rollover_cap_forfeit", "Rollover Cap Forfeit"
        ADMIN_ADJUSTMENT     = "admin_adjustment",     "Admin Adjustment"

    class Bucket(models.TextChoices):
        ALLOCATED = "allocated", "Allocated"
        PURCHASED = "purchased", "Purchased"

    subscription  = FK("onboard.Subscription", on_delete=PROTECT, related_name="ledger_entries")
    entry_type    = CharField(max_length=32, choices=EntryType.choices, db_index=True)
    bucket        = CharField(max_length=16, choices=Bucket.choices, default=ALLOCATED)
    amount        = IntegerField()          # signed: + credit, - debit, never 0
    balance_after = PositiveIntegerField()  # subscription total after this row
    reference_id  = CharField(max_length=255, null=True, blank=True, db_index=True)
    transaction   = FK("onboard.Transaction", null=True, blank=True, on_delete=SET_NULL,
                       related_name="ledger_entries")
    performed_by  = FK(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=SET_NULL)
    notes         = TextField(blank=True, null=True)
    metadata      = JSONField(null=True, blank=True)
```

```python
class Meta:
    db_table = "onboard_credit_ledger"
    ordering = ["-created_at"]
    constraints = [
        UniqueConstraint(fields=["entry_type", "reference_id"],
                         condition=Q(reference_id__isnull=False),
                         name="uniq_ledger_entry_reference"),        # D6
        CheckConstraint(condition=~Q(amount=0), name="ck_ledger_amount_non_zero"),
    ]
    indexes = [
        Index(fields=["subscription", "-created_at"]),
        Index(fields=["tenant", "entry_type", "-created_at"]),
    ]
```

**Append-only enforcement.** `BaseModelCore` ships `soft_delete()`, `restore()`, and an
`ExceptDeletedManager` that hides `is_deleted=True` rows — a soft-deleted ledger row would
silently break D1. So:

- `save()` raises if the row already exists in the DB.
- `soft_delete()` and `delete()` raise `NotImplementedError`.
- The admin (§8) is list + read-only detail; no add, no change, no delete action.
- Corrections are made by posting a compensating `ADMIN_ADJUSTMENT` row, never by editing.

### 3.4 `Transaction` additions — [onboard/models/transaction.py](../../onboard/models/transaction.py)

```python
class TransactionTypeChoices(models.TextChoices):
    ONBOARDING = "onboarding", "Onboarding"
    RENEWAL    = "renewal",    "Renewal"
    UPGRADE    = "upgrade",    "Upgrade"
    TOP_UP     = "top_up",     "Top-Up"

transaction_type  = CharField(max_length=20, choices=..., default=ONBOARDING, db_index=True)
tenant            = FK("tenant.Tenant", null=True, blank=True, on_delete=CASCADE,
                       related_name="transactions")
subscription      = FK("onboard.Subscription", null=True, blank=True, on_delete=SET_NULL,
                       related_name="transactions")
target_plan       = FK("onboard.Plan", null=True, blank=True, on_delete=SET_NULL,
                       related_name="target_transactions")   # plan being moved TO
credits_purchased = PositiveIntegerField(default=0)          # TOP_UP only
amount_breakdown  = JSONField(null=True, blank=True)         # net / tax / proration inputs
expires_at        = DateTimeField(null=True, blank=True)     # PENDING order TTL
```

`tenant` is nullable because onboarding transactions are created before a Tenant exists.
But it must never be null for the other three types, and with no live rows that is free to
enforce at the database rather than in review:

```python
CheckConstraint(
    condition=Q(transaction_type="onboarding") | Q(tenant__isnull=False),
    name="ck_transaction_tenant_required_post_onboarding",
)
```

Without it, a renewal or top-up order that loses its tenant is a payment that can be
captured and credited to nobody — and the webhook would have no way to tell.

`amount_breakdown` stores the exact proration inputs (old plan snapshot price, unused ratio,
days remaining) — when a tenant disputes an upgrade charge six weeks later, this is the only
record of how the number was produced.

`target_plan` is deliberately separate from the existing `plan` field rather than reusing
it: `plan` means "what they had" on a renewal and "what they bought" on onboarding, and
overloading it a third time makes every query ambiguous.

### 3.5 Register everything

- `onboard/models/__init__.py` — export `CreditLedger`.
- `onboard/admin/credit_ledger.py` + `onboard/admin/__init__.py`.
- `onboard/serializers/credit_ledger.py` (4-tier per AGENTS.md §3) + `__init__.py`.
- `config/settings/base.py` `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]`:
  `CreditLedgerEntryTypeEnum`, `CreditBucketEnum`, `TransactionTypeEnum` — the file already
  maps `TransactionStatusEnum` and `SubscriptionStatusEnum` (lines 311-312), and unmapped
  duplicate enums make `drf-spectacular` emit warnings.

### 3.6 Migrations

| # | Contents |
| :--- | :--- |
| `0021` | Plan credit fields (all defaulted, no data step). |
| `0022` | Subscription balance/schedule/cancel fields + both constraints. **Pre-check:** the active-subscription constraint fails to apply if any tenant already has two ACTIVE rows. The migration runs a `RunPython` that resolves duplicates (keep newest, mark the rest `EXPIRED`) *before* `AddConstraint`. |
| `0023` | `CreditLedger` table + constraints + indexes. |
| `0024` | Transaction type/tenant/subscription/breakdown fields; backfill `transaction_type = ONBOARDING` for all existing rows and populate `tenant` from `user.tenant_mapping.tenant` where resolvable. |
| `0025` | Data migration: for every live Subscription — set `end_date` where null (`start_date + cycle`), re-generate `plan_snapshot` so the credit keys exist, and write one `OPENING_BALANCE` ledger row per subscription so D1 holds from the first commit. |

Run `makemigrations onboard` then `migrate` locally.

Because production holds no tenants yet, `0022`'s duplicate resolution and all of `0025`
will process zero rows there. They still earn their place: dev and staging databases
accumulate exactly the junk these steps clean up, and the day the first customer onboards
these migrations are already behind them. Write them idempotent and no-op-safe rather than
assuming an empty table — `0025` in particular must skip subscriptions that already carry an
`OPENING_BALANCE` row, or a re-run doubles every balance.

The `OPENING_BALANCE` amount is **0 for every subscription**. There is no live tenant to
compensate and no usage history to honour; the first real credits any tenant sees come from
`INITIAL_PURCHASE` at signup. This removes the migration-day balance question entirely — but
it is only true while the tenant table is empty, so if that changes before Phase 1 ships,
this decision has to be revisited before the migration runs.

---

## 4. Services

New package `onboard/services/` (does not exist yet; mirrors `core/services/`).

### 4.1 `wallet.py` — the only thing that writes credits

Nothing outside this module mutates `credit_balance` or inserts a `CreditLedger` row. That
rule is what makes D1 auditable.

```python
def grant(subscription, *, entry_type, amount, bucket, reference_id=None,
          transaction=None, performed_by=None, notes="", metadata=None) -> CreditLedger | None
def consume(tenant, *, cost, reference_id, metadata=None) -> CreditLedger | None
def forfeit(subscription, *, entry_type, reference_id, notes="") -> CreditLedger | None
def assert_can_consume(tenant, *, cost) -> None            # raises InsufficientCredits
def balance_for(tenant) -> dict                            # {total, allocated, purchased, spendable}
def current_subscription(tenant) -> Subscription | None
```

Every mutating call obeys the same shape:

```python
with db_transaction.atomic():
    sub = Subscription.objects.select_for_update().get(pk=subscription.pk)
    if reference_id and CreditLedger.all_objects.filter(
            entry_type=entry_type, reference_id=reference_id).exists():
        return None                     # idempotent replay — D6
    # ... bucket arithmetic, floor at 0 ...
    sub.credit_balance = sub.allocated_balance + sub.purchased_balance
    sub.save(update_fields=[...])
    return CreditLedger.objects.create(..., balance_after=sub.credit_balance)
```

Notes that matter:

- `select_for_update()` is what stops two concurrent calls from both passing the balance
  check and driving the balance negative. Use `all_objects` for the idempotency probe so a
  soft-deleted row (which should not exist, but) still blocks a duplicate.
- `consume` drains `allocated_balance` first, then `purchased_balance` (D4).
- `consume` raises `InsufficientCredits` when the subscription is not `is_spendable` or the
  balance is short. That exception maps to HTTP 402 in the view layer.
- Returning `None` on replay rather than raising keeps every caller's happy path simple.

### 4.2 `subscriptions.py` — lifecycle

```python
def activate(tenant, plan, *, transaction=None, previous=None, reason) -> Subscription
def renew(subscription, *, transaction) -> Subscription
def apply_plan_change(subscription, new_plan, *, transaction=None, reason) -> Subscription
def schedule_plan_change(subscription, new_plan) -> Subscription
def cancel_scheduled_change(subscription) -> Subscription
def expire(subscription) -> Subscription
def rollover_amount(old_subscription, *, reason) -> int
```

`activate()` is the single function that fixes G2 — it sets dates, freezes the snapshot,
grants `allocated_credits` as `INITIAL_PURCHASE`, and is called from all three existing
signup sites ([create_order.py:119](../../onboard/views/onboarding/create_order.py#L119),
[verify_payment.py:181](../../onboard/views/onboarding/verify_payment.py#L181),
[webhook.py:104](../../onboard/views/onboarding/webhook.py#L104)) in place of the inline
`Subscription.objects.create(...)`.

`apply_plan_change()` is the execution engine, run inside one `atomic()` block:

1. Lock the old subscription row.
2. `carried = rollover_amount(old, reason)` — reads the **old** subscription's frozen rules
   (D5), returns `allocated_balance` if the matching flag is true else `0`, then clamps to
   `max_rollover_credits`. Purchased credits carry unconditionally (D4).
3. Forfeit what is not carried: `EXPIRY_FORFEIT` (flag off) or `ROLLOVER_CAP_FORFEIT`
   (clamped) for the difference. Two types, because "your plan doesn't roll over" and "you
   hit the cap" are different support conversations.
4. Close the old row: `status = CANCELLED`, `canceled_at = now`, balances to 0, and a final
   ledger row so the old subscription's own sum lands at exactly 0.
5. Create the new subscription via `activate(..., previous=old)`, set `old.superseded_by`.
6. Grant the carried credits (`UPGRADE_ROLLOVER` / `DOWNGRADE_ROLLOVER` / `RENEWAL_ROLLOVER`)
   and the new plan's `allocated_credits`.

Step 4 is the piece the scratch note leaves implicit, and skipping it means every closed
subscription keeps a non-zero balance forever — the reconciliation job would then flag every
historical row.

### 4.3 `proration.py` — money math

```python
def quote_plan_change(subscription, new_plan, *, now=None) -> ProrationQuote
def quote_top_up(subscription, credits) -> TopUpQuote
```

Plan change, computed on **pre-tax** values because tax must be charged on the actual
taxable value, then applied at the *new* plan's rate:

```
cycle_seconds   = (sub.end_date - sub.start_date).total_seconds()
unused_ratio    = max(0, (sub.end_date - now).total_seconds()) / cycle_seconds
credit_from_old = snapshot.net_base_price * unused_ratio
taxable_diff    = new_plan.net_base_price - credit_from_old
tax             = taxable_diff * new_plan.tax_percentage / 100      if taxable_diff > 0
amount_due      = (taxable_diff + tax).quantize("0.01", ROUND_HALF_UP)
```

- `amount_due > 0` → paid change (D3). **Razorpay rejects orders under ₹1.00** — if
  `0 < amount_due < 1`, skip the gateway and apply the change immediately, exactly as
  [create_order.py:85](../../onboard/views/onboarding/create_order.py#L85) already does for
  free plans.
- `amount_due <= 0` → scheduled change at `end_date`. No refund, no negative order.
- Prices come from `sub.plan_snapshot`, never the live Plan — the tenant is owed credit for
  what they actually paid.
- Quantize with `ROUND_HALF_UP` to match the `Plan` properties
  ([plan.py](../../onboard/models/plan.py)); mixing rounding modes produces off-by-a-paisa
  mismatches against Razorpay.

Top-up: `net = credits × cost_per_additional_credit`, tax at the current plan's
`tax_percentage` (§12/Q4), total quantized the same way. Reject when
`cost_per_additional_credit == 0` (top-ups disabled), when `credits <= 0`, or when
`credits` exceeds a sane per-order cap.

`ProrationQuote` / `TopUpQuote` are frozen dataclasses carrying every intermediate value, so
the preview endpoint, the order creation path, and `Transaction.amount_breakdown` all render
from one computation rather than three that can disagree.

### 4.4 `credit_policy.py` — what an action costs

```python
CREDIT_COSTS = {"outbound_call": 1, "inbound_call": 1}

def cost_for_call(call_log) -> int
```

One module, so that changing the cost of a call is one edit and one test. If §12/Q1 comes
back as per-minute, `cost_for_call` becomes `ceil(duration_seconds / 60) * rate` — the call
sites do not change.

---

## 5. API surface

**Prefix:** new top-level `path("billing/", include("onboard.urls.billing"))` in
[config/urls.py](../../config/urls.py) `api_v1_patterns`, giving `/api/v1/billing/...`.

Rationale: `/api/v1/onboard/` reads as signup and is TempUser-authenticated throughout;
`/api/v1/tenant/` (the scratch note's choice) is tenant identity and settings. Billing is
neither, and a dedicated prefix keeps the Swagger tag grouping clean. If a new prefix is
unwelcome, `/api/v1/tenant/billing/` is the fallback — views and services are unaffected.

All endpoints: `IsAuthenticated`, tenant resolved through `TenantBaseAPIView.get_tenant()`
([tenant_base_api_view.py](../../tenant/common/views/tenant_base_api_view.py)) or
`TenantBaseViewSet` for the list surfaces. Responses use `ResponseMixin.success/error`,
matching every other view in the repo.

| Method | Path | Purpose |
| :--- | :--- | :--- |
| GET | `/billing/subscription/current/` | Active subscription + balances + scheduled change + days remaining |
| GET | `/billing/subscription/eligible-plans/` | Active plans, each tagged `current` / `paid_change` / `scheduled_change` with an indicative amount |
| POST | `/billing/subscription/preview-change/` | Money **and** credit impact for one plan. Zero side effects |
| POST | `/billing/subscription/change-plan/` | Razorpay order (paid) or scheduled change (free) |
| DELETE | `/billing/subscription/scheduled-change/` | Cancel a pending downgrade |
| POST | `/billing/subscription/renew/` | Razorpay order for the next cycle of the same plan |
| GET | `/billing/wallet/` | Balances, buckets, expiry behaviour, low-balance threshold |
| POST | `/billing/wallet/top-up/quote/` | Cost breakdown for N credits. Zero side effects |
| POST | `/billing/wallet/top-up/` | Razorpay order for N credits |
| GET | `/billing/wallet/ledger/` | Paginated ledger, filter by `entry_type`, `bucket`, date range |
| GET | `/billing/transactions/` | Payment history for the tenant |

### 5.1 The two that carry the design

**`POST /billing/subscription/preview-change/`** — `{"plan_id": "<uuid>"}`

```jsonc
{
  "change_type": "paid_change",            // or "scheduled_change" | "immediate_free"
  "effective_at": "2026-08-21T10:04:00Z",  // end_date when scheduled
  "money": {
    "current_plan_net": "150.00", "unused_ratio": "0.6129", "credit_from_current": "91.94",
    "new_plan_net": "450.00", "taxable_difference": "358.06",
    "tax_percentage": "18.00", "tax_amount": "64.45",
    "amount_due": "422.51", "currency": "INR"
  },
  "credits": {
    "current_balance": 240, "allocated": 190, "purchased": 50,
    "rollover_allowed": true, "rollover_cap": 500,
    "carried_forward": 240, "forfeited": 0,
    "new_plan_allocation": 1000, "balance_after_change": 1240
  },
  "warnings": ["Your 190 plan credits do not roll over on a downgrade."]
}
```

The `warnings` array is what the UI shows in the confirmation dialog. A tenant who loses
credits must be told before they click, not after.

**`POST /billing/subscription/change-plan/`** — `{"plan_id": "<uuid>"}`

Recomputes the quote server-side (never trusts a client-supplied amount), then:

- `paid_change` → creates a `Transaction(type=UPGRADE, target_plan=..., subscription=...,
  amount_breakdown=quote, expires_at=now+30min)` and a Razorpay order; returns
  `{razorpay_order_id, amount, currency, razorpay_key_id}` — the same envelope the frontend
  already handles from
  [create_order.py](../../onboard/views/onboarding/create_order.py). The plan change happens
  on webhook, not here.
- `scheduled_change` → `schedule_plan_change()`, returns effective date. No payment.
- `immediate_free` → `apply_plan_change()` inline, returns the new subscription.

Guards: reject when a PENDING billing transaction already exists for this tenant (one open
order at a time, or the tenant can pay two upgrades and only one applies); reject a change
to the plan they are already on; reject when the subscription is not ACTIVE.

### 5.2 Error codes

Follow the existing `errors: ["CODE"]` convention. New codes: `NO_ACTIVE_SUBSCRIPTION`,
`INSUFFICIENT_CREDITS` (HTTP 402), `TOP_UPS_DISABLED`, `PLAN_UNCHANGED`,
`PENDING_ORDER_EXISTS`, `RENEWAL_WINDOW_NOT_OPEN`, `NO_SCHEDULED_CHANGE`,
`SUBSCRIPTION_NOT_ACTIVE`.

---

## 6. Webhook redesign — fixes G3 and G4

Rewrite [webhook.py](../../onboard/views/onboarding/webhook.py) around a dispatch table.
Order of operations, all of it inside one `atomic()` block:

1. **Credentials, per R4** (G4): all three Razorpay settings lose their defaults, the
   `getattr` fallback at [webhook.py:44](../../onboard/views/onboarding/webhook.py#L44)
   becomes a plain `settings.RAZORPAY_WEBHOOK_SECRET`, and the
   `RAZORPAY_KEY_ID == "rzp_test_dummykey"` bypass is replaced by the explicit
   `RAZORPAY_VERIFY_SIGNATURES` flag. `onboard/tests/` currently depends on that bypass, so
   the test settings must set the flag to `False` in the same change.
2. **Event-level idempotency** (G3): add `unique=True` to `RazorpayWebhookLog.event_id` and
   `get_or_create` on it. A duplicate `event_id` returns HTTP 200 immediately without
   re-processing. Razorpay retries aggressively; this is the outer guard, D6 is the inner one.
3. **Verify signature**, log the outcome, 400 on failure — unchanged behaviour.
4. **Dispatch** `payment.captured` on `transaction.transaction_type`:
   - `ONBOARDING` → existing promotion path, with `Subscription.objects.create` swapped for
     `subscriptions.activate()`.
   - `TOP_UP` → `wallet.grant(entry_type=MANUAL_TOP_UP, bucket=PURCHASED,
     amount=txn.credits_purchased, reference_id=txn.razorpay_order_id, transaction=txn)`.
   - `UPGRADE` → `subscriptions.apply_plan_change(txn.subscription, txn.target_plan,
     transaction=txn, reason=UPGRADE)`.
   - `RENEWAL` → `subscriptions.renew(txn.subscription, transaction=txn)`.
5. **Never re-raise** (G3): replace `raise e` at
   [webhook.py:135](../../onboard/views/onboarding/webhook.py#L135) with — record the error on
   `log_entry`, log at `exception` level, return **200**. A 500 makes Razorpay retry a
   payload that will fail identically, forever. The webhook log plus the reconciliation job
   (§7) are how a genuinely stuck payment gets noticed.
6. Keep `payment.failed` as-is, extended to mark the matching order FAILED for every type.

`verify_payment.py` gets the same dispatch for the client-confirmed path, so a tenant who
completes checkout in the browser is not left waiting on the webhook. Both paths converge on
the same idempotent services, so whichever lands second is a no-op.

---

## 7. Scheduled tasks — new `onboard/tasks.py`

Registered in `CELERY_BEAT_SCHEDULE`
([config/settings/base.py:663-714](../../config/settings/base.py#L663-L714)), following the
`ReportedTask` base used in [core/tasks.py](../../core/tasks.py).

| Task | Cadence | Does |
| :--- | :--- | :--- |
| `process_billing_cycle` | hourly | Per due subscription, **in this order**: apply a scheduled change if `scheduled_change_at <= now`, else expire if `end_date < now`. |
| `send_renewal_reminders` | daily | T-7 / T-3 / T-1 before `end_date`, deduped by day. |
| `send_low_balance_alerts` | daily | Balance at or below `low_balance_threshold`. |
| `expire_stale_billing_orders` | hourly | PENDING transactions past `expires_at` → CANCELED, so `PENDING_ORDER_EXISTS` cannot wedge a tenant out of upgrading. |
| `reconcile_credit_ledger` | weekly | Assert D1 for every subscription; log a structured error per drift. **Reports, never repairs** — silent auto-correction hides the bug that caused the drift. |

**The ordering in `process_billing_cycle` is the point.** Two independent tasks — one
expiring subscriptions, one applying scheduled downgrades — race at exactly the moment both
fire, and the tenant who scheduled a downgrade gets expired instead. One task, one pass,
explicit order.

Expiry also runs the forfeit rule: when `allow_use_after_expiry` is false, write
`EXPIRY_FORFEIT` for the full balance; when true, leave the balance and let `is_spendable`
keep authorising until the tenant renews.

Also add a management command `onboard/management/commands/reconcile_credit_ledger.py`
wrapping the same logic with a `--fix` flag for deliberate operator repair.

---

## 8. Admin

- `onboard/admin/credit_ledger.py` — `BaseAdminCore` subclass, `has_add_permission` /
  `has_change_permission` / `has_delete_permission` all `False`. List: tenant, subscription,
  entry type, bucket, amount, balance_after, reference, created_at. Filters: entry_type,
  bucket, tenant. Search: reference_id, subscription id, tenant name.
- `onboard/admin/subscription.py` — add balance/schedule fields to `list_display` and a
  "Credits" fieldset; make `credit_balance`, `allocated_balance`, `purchased_balance`
  **read-only** (D7 — the only sanctioned way to move credits is an `ADMIN_ADJUSTMENT`).
- Two admin actions on Subscription: **Grant credits** and **Deduct credits**, each an
  intermediate form taking amount + reason, writing through `wallet.grant()` with
  `performed_by=request.user`. This is what keeps staff out of the raw balance field.
- `onboard/admin/plan.py` — a "Credits & Rollover" fieldset for the new Plan fields.

---

## 9. Notifications

`InAppNotification` ([core/models/in_app_notification.py](../../core/models/in_app_notification.py))
is generic — tenant, user, title, body, type — so billing notifications create rows directly.
Its `notification_type` reuses `NotificationLog.NotificationType`, whose choices are
booking-only today (`CONFIRMATION`, `REMINDER`, `RESCHEDULE_REQUEST`, `REASSIGNED`,
`CANCELED`, `RESCHEDULED`). Add `BILLING` and `CREDITS`, then a small
`onboard/services/billing_notifications.py` for: renewal due, subscription expired, plan
change scheduled, plan change applied, top-up credited, low balance, credits forfeited.

Push delivery reuses `core/services/push.py` if the tenant has device tokens; email is
optional in v1. `core/services/notifications.py` is booking-shaped throughout and should not
be extended for this.

---

## 10. Tests

Run with the Postgres settings — the default SQLite test DB cannot handle this codebase's
`ArrayField` usage:

```
python manage.py test onboard --settings=config.settings.test_postgres
```

| File | Covers |
| :--- | :--- |
| `onboard/tests/test_wallet_service.py` | grant/consume/forfeit arithmetic; bucket drain order; `balance_after` correctness; D1 invariant after a random sequence of ops; idempotent replay returns None and writes nothing; `InsufficientCredits`; the `select_for_update` path under two concurrent `consume` calls. |
| `onboard/tests/test_proration.py` | Upgrade mid-cycle; downgrade → negative → scheduled; monthly↔yearly; day-one and last-day boundaries; sub-₹1 immediate path; rounding against known fixtures. |
| `onboard/tests/test_subscription_lifecycle.py` | Rollover on/off per flag; cap clamping and `ROLLOVER_CAP_FORFEIT`; old subscription closes at exactly 0; `superseded_by` chain; scheduled change applied by the task; cancelling a scheduled change; expiry with and without `allow_use_after_expiry`. |
| `onboard/tests/test_billing_api.py` | Every endpoint: auth, tenant isolation (tenant B cannot see tenant A's ledger), `PENDING_ORDER_EXISTS`, `PLAN_UNCHANGED`, 402 on insufficient credits, preview has no side effects. |
| `onboard/tests/test_billing_webhook.py` | Dispatch per transaction type; **replayed `event_id` credits once**; replayed `payment.captured` with a fresh event id still credits once (D6); processing failure returns 200 and logs. |
| `onboard/tests/test_credit_consumption.py` | Call log create deducts once; same `provider_call_id` posted twice deducts once; expired-but-spendable tenant still deducts; zero-balance tenant is refused. |

Two existing suites need updating rather than replacing:
[test_subscription.py](../../onboard/tests/test_subscription.py) (subscriptions now carry an
`end_date` and credit fields) and `test_onboarding.py` (activation moves behind the service).

Model tests that call `full_clean()` must exclude `created_by` / `updated_by` /
`deleted_by`; on the audited base classes those are populated at save time, so a bare
`full_clean()` always fails. `CreditLedger` uses the Tier-1 core base and is unaffected.

---

## 11. Build order

Seven phases plus one decision point. Each ends in a state that can be merged and deployed
on its own.

### Phase 0 — Foundation repairs *(blocking; no user-visible change)*
The seven blocker resolutions in §1.2, R1-R7. Ship them together: R2 is what R1 hangs off,
and R3 and R5 both add constraints that want their data steps in one pass.

No maintenance window and no production rehearsal — there is no tenant data to endanger
(§1.2). This is the cheapest Phase 0 will ever be, which is the argument for doing it in
full now rather than deferring any of it past the first customer.

**Done when:**
- Django refuses to boot with any Razorpay credential unset, and `manage.py check` fails when `RAZORPAY_VERIFY_SIGNATURES` is off outside DEBUG (R4).
- Existing onboarding tests pass unchanged, run under `test_postgres` (R2).
- Every Subscription — new and backfilled — has an `end_date` (R1).
- The same webhook `event_id` delivered twice produces one processed log and two 200s (R3).
- A second ACTIVE subscription for a tenant raises `IntegrityError` (R5).
- `subscription.rules` returns plan values for a row whose snapshot predates the change (R7).

### Phase 1 — Ledger and balances
Plan fields, Subscription balance fields, `CreditLedger`, `wallet.py`, admin, serializers,
migrations `0021`-`0025` including the `OPENING_BALANCE` backfill, reconciliation command.
**Done when:** `reconcile_credit_ledger` reports zero drift across all existing tenants.

### Phase 2 — Consumption engine *(metered, not enforced — D10)*
`credit_policy.py`; `TenantSettings.credits_enforced` defaulting to false;
`assert_can_consume` in the campaign start path ([core/tasks.py](../../core/tasks.py)
`start_due_campaigns`) and in the runner context endpoint; `wallet.consume` on call-log
create at [agent_call_log.py:189](../../core/views/agent_call_log.py#L189); balance surfaced
in the runner campaign context payload.

**Coordinate with the runner team before this ships.** Adding balance to the campaign
context changes a contract an external service consumes, and the `INSUFFICIENT_CREDITS`
refusal is a new failure mode it has to handle rather than retry into. That conversation is
a dependency, not a follow-up.

**Done when:** every completed call writes exactly one `USAGE_DEDUCTION` (a call posted
twice deducts once), a zero-balance tenant is logged as *would-be-refused* but still runs,
and flipping `credits_enforced` on a test tenant blocks their campaign.

### Phase 2b — Flip enforcement *(a decision point, not a code change)*
After a full billing cycle of real traffic: compare ledger burn against what tenants paid,
confirm the credit cost in `credit_policy.py` is right, then enable `credits_enforced`.
Nothing ships here except a settings change and, if the cost was wrong, one edit to
`credit_policy.py` before anyone is blocked by it.

### Phase 3 — Manual top-ups
`quote_top_up`, the two top-up endpoints, `TOP_UP` webhook branch, `MANUAL_TOP_UP` grants
into the purchased bucket.
**Done when:** an end-to-end test buys credits and the balance rises exactly once.

### Phase 4 — Renewals and expiry
Renewal endpoint and window rules, `RENEWAL` webhook branch, `process_billing_cycle`,
renewal reminders, `expire_stale_billing_orders`.
**Done when:** an unpaid subscription expires on schedule with the correct forfeit
behaviour, and a paid renewal extends the cycle and allocates credits once.

### Phase 5 — Upgrades and downgrades
`proration.py`, preview / change-plan / cancel-scheduled endpoints, `UPGRADE` webhook
branch, `apply_plan_change` and the scheduled path in `process_billing_cycle`.
**Done when:** a mid-cycle upgrade charges the prorated difference and applies on payment,
and a downgrade applies at `end_date` with the rollover the preview promised.

### Phase 6 — Read surface, notifications, docs
Wallet, ledger history and transaction endpoints; billing notifications; low-balance alerts;
`docs/apiDocs/onboard/billing.md`, `wallet.md`, `credits.md`; `docs/_sidebar.md` entry under
a new **💳 Billing & Credits** group; update
[docs/apiDocs/onboard/webhooks.md](../../docs/apiDocs/onboard/webhooks.md) for the new event
types.

**Phases 3, 4 and 5 are independent of each other** once 0-2 land, so they can be split
across people. Nothing before Phase 2 is visible to a tenant, which makes Phases 0-1 safe to
ship early and quietly.

---

## 12. Open questions

Nine of the thirteen are settled — struck through, with what was decided and where it is
written. Numbers are kept stable because other sections reference them.

**Four remain open, none blocking Phase 0–2:**

| # | Still open | Needed by |
| :--- | :--- | :--- |
| Q4 | GST treatment on top-ups | Phase 3 — needs whoever owns tax compliance |
| Q5 | Expired-with-balance, then a new plan a month later — does the balance return? | Phase 4 |
| Q6 | Refund the credit for a failed or unanswered call? | Phase 4 — needs the runner's outcome taxonomy |
| Q7 | Renewal window and grace period | Phase 4 |

Each still carries a recommended default below, so no phase stalls waiting on an answer.

| # | Question | Recommendation |
| :--- | :--- | :--- |
| Q1 | ~~Is a credit one call or one minute?~~ | **RESOLVED.** Per **minute**, rounded up to whole billable units. A 90-second call on a 5-credits/minute plan costs 10. See the wallet document's D2 and §3.7. |
| Q2 | ~~Are purchased credits a separate bucket from allocated?~~ | **RESOLVED — yes.** `allocated_balance` and `purchased_balance`, consumption drains allocated first, rollover rules govern the allocated bucket only. Now lives on the tenant wallet, not the subscription. |
| Q3 | ~~Should rollover be capped?~~ | **RESOLVED — yes.** `max_rollover_credits`, seeded at 1× the cycle allocation for Starter and Professional, uncapped for Pay As You Go (wallet §3.7). |
| Q4 | Is GST charged on top-ups, and at what rate? | Charge at the tenant's **current plan** `tax_percentage`. Confirm with whoever owns tax compliance before Phase 3 ships. |
| Q5 | A tenant expires with a balance, forfeits nothing (`allow_use_after_expiry=true`), then buys a new plan a month later. Does the old balance return? | **No.** Forfeit at the moment the new subscription activates, logged as `EXPIRY_FORFEIT`. Resurrecting balances across a gap makes reconciliation ambiguous. |
| Q6 | Failed / unanswered / voicemail calls — refund the credit? | Refund on provider-side failure only (no connection established), via `USAGE_REFUND` keyed on the same `provider_call_id`. No refund for a connected call the human hung up on. Needs the runner's outcome taxonomy confirmed. |
| Q7 | How early can a tenant renew, and is there a grace period after `end_date`? | Renew from T-14; no grace period in v1 (`allow_use_after_expiry` already covers the soft landing). |
| Q8 | ~~Can a scheduled downgrade be cancelled?~~ | **RESOLVED — yes.** `DELETE /billing/subscription/scheduled-change/`, and cleared automatically when an upgrade is applied first. Written into R5. |
| Q9 | ~~Mid-cycle downgrade — refund in cash or credits?~~ | **RESOLVED — neither.** It is scheduled for `end_date`; the tenant keeps what they paid for until the cycle ends. Written into R5 as a product rule. |
| Q10 | ~~Should tenants see the raw ledger?~~ | **RESOLVED — yes.** Read-only and paginated, with the rating snapshot inline so “why was I charged 10 credits?” is answered on the row. |
| Q11 | ~~Do FREE plans expire and cycle?~~ | **RESOLVED — they expire and block**, exactly like paid plans. This is what allows `end_date` to be `null=False` (R1). Note the product consequence: the Free Tier is now a 30-day trial, and the pricing copy should say so. |
| Q12 | ~~What is `PAY_AS_YOU_GO` for?~~ | **RESOLVED.** A plan with `allocated_credits=0`, top-ups enabled and `allow_use_after_expiry=true`. It **auto-renews at ₹0** rather than expiring — blocking a tenant whose purchased credits sit unspent would be indefensible (wallet §3.7). |
| Q13 | ~~Nobody has set the actual numbers.~~ | **RESOLVED — wallet §3.7.** Full grid seeded: allocation, per-plan rates, top-up prices, entitlement caps and rollover rules for all six plans. Corrected at W5 against real traffic. |

---

## 13. Risks

| Risk | Mitigation |
| :--- | :--- |
| **Double credit on webhook retry.** Highest-severity failure mode: real money, silent. | Two independent guards — unique `event_id` at the log layer (§6.2) and unique `(entry_type, reference_id)` at the ledger layer (D6). Explicit tests for both. |
| **Balance drifts from the ledger.** | Single-writer service, `select_for_update` on every mutation, `CheckConstraint` against negative balances, weekly reconciliation that reports rather than repairs. |
| **Concurrent consumption oversells.** Two calls start at once with one credit left. | Row lock in `wallet.consume`; the second call raises `InsufficientCredits`. |
| **The credit cost is wrong.** One-per-call may be far off actual economics, and the first symptom would be a paying customer's campaign refusing to start. | D10 — meter for a full cycle with enforcement off, compare ledger burn to revenue, fix `credit_policy.py`, *then* flip. |
| **Existing subscriptions have no `end_date`.** Migration `0025` guesses a cycle. | Moot while no tenant is live (§1.2); the migration processes zero rows. Keep it idempotent so it stays safe on dev and staging, and re-read this row if anyone onboards before Phase 1 ships. |
| **The runner is an external service with its own release cycle.** Phase 2 changes its campaign-context contract and adds a refusal it must not retry into. | Treat the runner-team conversation as a Phase 2 dependency, not a follow-up. Ship the balance field additively so an un-updated runner keeps working. |
| **A plan edited mid-cycle changes live terms.** | Rules read from the frozen snapshot (D5). Snapshot is written once, at activation. |
| **Removing the credential defaults stops Django booting** anywhere the env vars are missing — CI, a fresh clone, a forgotten deploy variable. | Deliberate: it is the whole point of R4. Land the variables in `.env`, the sample, `docker-config/` and CI **before** the settings change merges, and verify the test settings carry `RAZORPAY_VERIFY_SIGNATURES=False`. A boot failure is loud and instant; the alternative was a production webhook endpoint accepting unsigned requests. |
| **Razorpay minimum order amount (₹1.00).** A tiny prorated upgrade produces an order Razorpay refuses. | `amount_due < 1` takes the immediate-free path (§4.3). |
| **No auto-pay means renewals are a human action.** Expect churn purely from forgetfulness. | T-7/T-3/T-1 reminders plus in-app and push. Revisit the moment mandates are approved — by design, only order creation and the webhook trigger change. |
| **The tenant-facing story is new surface area.** Six endpoints, all money-adjacent. | Preview endpoints are side-effect-free and share one computation with the commit path, so what the tenant is shown is what they are charged. |

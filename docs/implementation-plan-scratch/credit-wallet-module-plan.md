# Credit Wallet Module — Implementation Plan

**Status: built, migrated and tested.** Every phase in §12 shipped except W5, which is
an operator decision rather than work. 175 test cases cover this app; the numbers that
turned out differently from this plan are marked *(as built)* where they occur.
**Companion document:** [subscription-credits-wallet-plan.md](subscription-credits-wallet-plan.md)
— the subscription lifecycle, proration and top-up engine. That document *funds* this one.
Where the two overlap, this document is authoritative on anything inside the wallet.

Scope: **a centralised, tenant-level credit wallet** — the store of value, the append-only
ledger behind it, the rate card that turns real-world usage into credits, and the
consumption path that spends them.

Out of scope here (and owned by the companion document): subscription lifecycle, plan
changes and proration, Razorpay orders, and manual top-up purchase flows.
Metered overage was considered and rejected — see §10.3.

> **Quota enforcement was out of scope for v1 and has since been built** — in `onboard`,
> not here, exactly as §3.4 and §9 required. `onboard/services/entitlements.py` plus
> `EntitlementGuardMixin`; over-limit creates return **402 `QUOTA_EXCEEDED`** once
> `entitlements_enforced` is on for the tenant.

---

## 1. The mental model

Four concepts. Most credit systems go wrong by merging two of them.

| | What it is | Where it lives |
| :--- | :--- | :--- |
| **Wallet** | The store of value. One per tenant, one number. | `Wallet`, 1:1 with `Tenant` |
| **Ledger** | The history — why the balance is what it is. Append-only. | `CreditLedger` |
| **Rate card** | The price list, **per plan**. "On Pro, one minute of outbound call = 3 credits." | `CreditRate`, one set per `Plan` |
| **Entitlements** | The caps, **per plan**, and what more of them costs in ₹. Never touches the wallet. | `PlanEntitlement` rows |

**The plan is the source of both.** Rates and entitlements are not global platform
constants — they are part of what a tenant buys. Choosing Pro over Starter buys a bigger
allocation, cheaper per-minute calls, and higher caps, all at once. §2/D9 covers how those
plan terms get frozen onto the tenant at subscription time.

### 1.1 Quota is a gauge; credit is a flow

These behave differently and only one of them belongs in a wallet.

**Quota** — storage, agent count, contacts, seats. Measured as *current state against a
cap*. Nobody spends 5 GB permanently: upload 5 GB, delete a file, and you are back to 3.
Enforcement is `current_usage + requested <= limit`. The resource is its own record — you
count agents by counting agents.

**Credit** — call minutes. Spent and gone. Enforcement is
`balance >= cost`. This, and only this, is the wallet.

The test for which one you are holding: **does deleting it give the resource back?**
Delete a file, 5 GB returns → quota. Hang up a call, the minutes do not return → credit.

Modelling a gauge as a flow produces questions with no good answer — *they deleted the file,
do we refund the credits? Storage pricing changed, do we re-rate history?* The questions are
unanswerable because the model was wrong, not because the policy is undecided.

### 1.2 The wallet knows nothing about subscriptions

This is the load-bearing decision of the whole module, and it is what "centralised"
actually buys.

```
  onboard (subscriptions)              core (calls, campaigns)
         │                                      │
         │ grant() / forfeit() / freeze()       │ consume() / assert_can_consume()
         │ set_plan_context()                   │
         ▼                                      ▼
  ┌──────────────────────────────────────────────────┐
  │                  wallet                          │
  │   Wallet · CreditLedger · CreditRate             │
  │   knows: tenants, credits, rates, and which      │
  │          plan's rates it was stamped with        │
  │   does not know: subscriptions, cycles, money,   │
  │          Razorpay, rollover policy               │
  └──────────────────────────────────────────────────┘
                         │
                         ▼
                      tenant
```

Dependencies point **inward only**. The subscription engine deposits credits, stamps the
wallet with the plan's rate card, and freezes the wallet when a cycle lapses; the wallet
never inspects a subscription to decide anything.

**Rates being plan-scoped does not break this**, because the plan's rate card is *pushed to*
the wallet at subscription time and read back off the wallet's own row at call time (D9).
The alternative — having the rating path look up "which subscription is active for this
tenant right now" on every call — would invert the dependency and put a cross-app join on
the hot path. Pushing instead of pulling avoids both.

The one soft link is `CreditLedger.subscription`, a nullable FK kept for audit context —
"which cycle was live when this was spent" — and documented as read-never in decision paths.

That inversion is why the wallet can be built, tested and shipped **before** any of the
subscription work lands.

---

## 2. Decisions

### D1 — One wallet per tenant, outliving every subscription

`Wallet` is 1:1 with `Tenant`. The companion plan originally put `credit_balance` on
`Subscription`; anchoring to the tenant is strictly better here:

- Plan changes stop being balance *transfers* and become forfeit *decisions*. The
  close-old-at-exactly-zero dance in the companion document's §4.2 disappears.
- "What is my balance" is one row that never moves, whatever happens to plans.
- Consumption paths in `core` never have to resolve which subscription is current.

### D2 — Integer credits; usage rounds up to whole billable units

Credits are `PositiveIntegerField` everywhere — balance, ledger, API. No decimals, no
floats, no scaling factor to forget in a display path.

Fractional *usage* is resolved before it becomes credits. A 90-second call on a
5-credits-per-minute rate is `ceil(90/60) = 2` units → **10 credits**. Rounding is a
property of the rate (§3.3), so a future per-second rate is a config change, not a schema
change.

This is standard telecom billing and defensible to a customer, which matters more than the
few seconds of rounding in your favour on each short call.

### D3 — Every deduction carries its own rating snapshot

A ledger row records not just `-10` but *how 10 was reached*: `raw_quantity=90`,
`unit_type=per_minute`, `units=2`, `rate_applied=5`.

Without it, raising call pricing next year silently re-explains every historical row with
the new rate, and "why was I charged 10 credits?" becomes unanswerable. With it, the answer
is on the row.

### D4 — Rates are versioned in the catalogue, never edited in place

`CreditRate` carries `effective_from` / `effective_to`. Re-pricing a plan means closing the
current row and opening a new one, never updating in place.

Note what this is *for*, given D9: the catalogue history answers "when did we change Pro to
3 credits per minute, and what was it before" — a question that comes up in pricing reviews
and customer disputes. It is **not** what the runtime reads. Runtime rating never queries
this table (D9), and historical charges are already reproducible from the ledger's own
rating snapshot (D3). Three layers, each answering a different question:

| Layer | Answers |
| :--- | :--- |
| `CreditRate` history | What did we *advertise* on this plan, on this date? |
| `Wallet.rate_snapshot` | What is this tenant *entitled to* right now? |
| `CreditLedger` rating fields | What was this *specific charge* computed from? |

### D5 — The ledger is the truth; `Wallet.balance` is a cache

```
SUM(CreditLedger.amount WHERE wallet = W) == W.balance
```

Enforced by a single writer service (§4.1), `select_for_update()` on the wallet row around
every mutation, and a reconciliation job that reports drift rather than repairing it (§7).

### D6 — Idempotency by construction

Every ledger row carries `(entry_type, reference_id)` under a unique constraint. A retried
runner POST, a replayed webhook, or a double-clicked button hits the constraint and is
swallowed as a no-op. `AgentCallLog.provider_call_id` is already `unique=True`
([agent_call_log.py:11-16](../../core/models/agent_call_log.py#L11-L16)), which makes it a
free, reliable reference for call deductions.

### D7 — Metering ships before blocking

`consume()` always runs and always writes a ledger row. `assert_can_consume()` only
*refuses* when `TenantSettings.credits_enforced` is true — otherwise it logs what would have
been refused and returns. Carried over from the companion plan's D10; the flag lives on
[TenantSettings](../../tenant/models/TenantSettings.py), whose `for_tenant()` accessor
already treats a missing row as all-defaults rather than an error.

### D8 — A new `wallet` Django app

`INSTALLED_APPS` currently holds four local apps — `user`, `tenant`, `onboard`, `core`
([base.py:117-121](../../config/settings/base.py#L117-L121)). This adds a fifth.

The alternative is putting it in `onboard`, where `Plan`, `Subscription` and `Transaction`
already live. That fails on one point: `core` is the product's main app and it is what spends
credits. Housing the wallet in `onboard` would make
[core/views/agent_call_log.py](../../core/views/agent_call_log.py) import from the *signup*
app on every call. A dedicated app makes that `from wallet.services import consume`, and
makes the boundary in §1.2 real rather than aspirational.

Cost: an app config, a migrations directory, an `INSTALLED_APPS` line, and admin/URL wiring.
If a fifth app is unwelcome, `onboard` works — the models and services are unchanged, only
the import paths move.

### D9 — The plan's terms are stamped onto the tenant at subscription time

**Rates and entitlements belong to the plan, and the plan is chosen at subscription.** So
the flow is:

```
  Plan (catalogue)                    "Pro: 1000 credits/cycle,
    │  defines: allocation             3 credits/min outbound,
    │           rate card              10 agents, 50 GB"
    │           entitlement caps
    ▼
  Subscription created  ──────┬──────▶  plan_snapshot   (money · credit rules · entitlements)
    (payment succeeds)        │              on onboard.Subscription
                              └──────▶  rate_snapshot   (the plan's rate card)
                                             on wallet.Wallet
    ▼
  Every call rates against the wallet's own stamped snapshot — one row read, no joins.
```

Two snapshots rather than one, because they answer to different owners: money and caps stay
with the subscription (the contract), the rate card goes to the wallet (the meter). Neither
is read from the live `Plan` at runtime.

**Why freeze rather than follow the live plan.** The companion document already freezes
pricing at subscription time (its D5/R7) so that editing a plan changes terms for *future*
cycles only. Rates must follow the same rule or the guarantee is incoherent: a tenant's
monthly fee would be locked while the rate at which they burn credits silently doubled
mid-cycle. Locking one and not the other is worse than locking neither.

**When the stamp is refreshed:** on activation, on renewal, and on any applied plan change.
All three already call into the wallet (§10.2), so it is one more call on paths that exist.

**Fallback:** a wallet with no `rate_snapshot` — a tenant that predates this, or one created
outside the normal flow — falls back to reading the live plan's current `CreditRate` rows
and logs a warning. If that also finds nothing, `RateNotConfigured` is raised rather than
charging zero.

---

## 3. Data model

All models follow AGENTS.md: tenant-scoped models extend `TenantBaseModelCore`
(Tier 1 — these rows are system-written), global models extend `BaseModelCore`.

### 3.1 `Wallet` — `wallet/models/wallet.py`

```python
class Wallet(TenantBaseModelCore):
    """The tenant's credit balance. One row per tenant, for the life of the tenant."""

    balance           = PositiveIntegerField(default=0)   # allocated + purchased
    allocated_balance = PositiveIntegerField(default=0)   # granted by a plan cycle
    purchased_balance = PositiveIntegerField(default=0)   # bought with cash

    lifetime_granted  = PositiveIntegerField(default=0)   # monotonic, analytics only
    lifetime_consumed = PositiveIntegerField(default=0)   # monotonic, analytics only

    is_frozen         = BooleanField(default=False)
    frozen_reason     = CharField(max_length=255, blank=True, null=True)
    frozen_at         = DateTimeField(null=True, blank=True)

    low_balance_threshold   = PositiveIntegerField(default=0)   # 0 disables
    low_balance_notified_at = DateTimeField(null=True, blank=True)

    # --- the plan's rate card, stamped at subscription time (D9) -----------------
    current_plan     = FK("onboard.Plan", null=True, blank=True, on_delete=SET_NULL,
                          related_name="wallets")     # display and debug only
    rate_snapshot    = JSONField(null=True, blank=True)   # the authority for rating
    rate_snapshot_at = DateTimeField(null=True, blank=True)
```

`rate_snapshot` is what the rating engine actually reads (D9). `current_plan` is a
convenience for the admin and the rates endpoint — a string FK, so no import and no
dependency on `onboard` at module load. Shape:

```jsonc
{
  "plan_id": "…", "plan_code": "pro", "stamped_at": "2026-08-23T09:00:00Z",
  "operations": {
    "outbound_call": { "unit": "per_minute", "credits_per_unit": 3,
                       "rounding": "ceil", "minimum_units": 1, "rate_id": "…" },
    "inbound_call":  { "unit": "per_minute", "credits_per_unit": 0,
                       "rounding": "ceil", "minimum_units": 1, "rate_id": "…" }
  }
}
```

`rate_id` is carried so a ledger row can point back at the catalogue row it came from, even
though rating never queries that table at runtime.

```python
class Meta:
    db_table = "wallet_wallet"
    constraints = [
        UniqueConstraint(fields=["tenant"], condition=Q(is_deleted=False),
                         name="uniq_wallet_per_tenant"),
        CheckConstraint(condition=Q(balance=F("allocated_balance") + F("purchased_balance")),
                        name="ck_wallet_balance_is_sum_of_buckets"),
    ]
```

The unique constraint mirrors
[TenantSettings](../../tenant/models/TenantSettings.py), which uses exactly this pattern.

**Two buckets, one balance.** `allocated_balance` is what a plan cycle granted;
`purchased_balance` is what the tenant paid cash for mid-cycle. Rollover rules govern the
allocated bucket only — cash-bought credits always survive a plan change. Consumption drains
**allocated first**, so the bucket the tenant paid for is the one that lasts.

**`is_frozen` is set from outside.** The wallet has no opinion on why. The subscription
engine freezes on lapse and unfreezes on renewal; support freezes for fraud. Frozen means
"cannot spend"; it does not block grants, because a renewal payment must be able to land in
a frozen wallet and unfreeze it.

Access mirrors the `TenantSettings.for_tenant()` precedent — "no row yet" must behave like
"empty wallet", never like an error, because this is read on a call path:

```python
@classmethod
def for_tenant(cls, tenant_id):
    wallet, _ = cls.objects.get_or_create(tenant_id=tenant_id)
    return wallet
```

### 3.2 `CreditLedger` — `wallet/models/credit_ledger.py`

```python
class CreditLedger(TenantBaseModelCore):
    class EntryType(models.TextChoices):
        OPENING_BALANCE      = "opening_balance",      "Opening Balance"
        CYCLE_ALLOCATION     = "cycle_allocation",     "Cycle Allocation"
        MANUAL_TOP_UP        = "manual_top_up",        "Manual Top-Up"
        USAGE_DEDUCTION      = "usage_deduction",      "Usage Deduction"
        USAGE_ADJUSTMENT     = "usage_adjustment",     "Usage Adjustment"
        USAGE_REFUND         = "usage_refund",         "Usage Refund"
        ROLLOVER_FORFEIT     = "rollover_forfeit",     "Rollover Forfeit"
        ROLLOVER_CAP_FORFEIT = "rollover_cap_forfeit", "Rollover Cap Forfeit"
        EXPIRY_FORFEIT       = "expiry_forfeit",       "Expiry Forfeit"
        ADMIN_ADJUSTMENT     = "admin_adjustment",     "Admin Adjustment"

    class Bucket(models.TextChoices):
        ALLOCATED = "allocated", "Allocated"
        PURCHASED = "purchased", "Purchased"

    wallet        = FK(Wallet, on_delete=PROTECT, related_name="entries")
    entry_type    = CharField(max_length=32, choices=EntryType.choices, db_index=True)
    bucket        = CharField(max_length=16, choices=Bucket.choices, default=ALLOCATED)
    amount        = IntegerField()          # signed: + credit, - debit, never 0
    balance_after = PositiveIntegerField()

    # --- rating snapshot (D3): only on usage rows -------------------------------
    operation     = CharField(max_length=32, null=True, blank=True)   # outbound_call
    raw_quantity  = PositiveIntegerField(null=True, blank=True)       # 90 (seconds)
    units         = PositiveIntegerField(null=True, blank=True)       # 2 (minutes)
    unit_type     = CharField(max_length=32, null=True, blank=True)   # per_minute
    rate_applied  = PositiveIntegerField(null=True, blank=True)       # 5 credits/unit
    rate          = FK("wallet.CreditRate", null=True, blank=True, on_delete=SET_NULL)

    # --- provenance --------------------------------------------------------------
    reference_id  = CharField(max_length=255, null=True, blank=True, db_index=True)
    subscription  = FK("onboard.Subscription", null=True, blank=True, on_delete=SET_NULL,
                       related_name="ledger_entries")   # audit context only (§1.2)
    performed_by  = FK(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=SET_NULL)
    notes         = TextField(blank=True, null=True)
    metadata      = JSONField(null=True, blank=True)
```

```python
class Meta:
    db_table = "wallet_credit_ledger"
    ordering = ["-created_at"]
    constraints = [
        UniqueConstraint(fields=["entry_type", "reference_id"],
                         condition=Q(reference_id__isnull=False),
                         name="uniq_ledger_entry_reference"),          # D6
        CheckConstraint(condition=~Q(amount=0), name="ck_ledger_amount_non_zero"),
    ]
    indexes = [
        Index(fields=["wallet", "-created_at"]),
        Index(fields=["tenant", "entry_type", "-created_at"]),
        Index(fields=["operation", "-created_at"]),
    ]
```

**Append-only enforcement.** `BaseModelCore` ships `soft_delete()` and an
`ExceptDeletedManager` that hides deleted rows — a soft-deleted ledger row would silently
break D5. So `save()` raises when the row already exists, `soft_delete()` and `delete()`
raise `NotImplementedError`, the admin is read-only (§8), and corrections happen by posting
a compensating `ADMIN_ADJUSTMENT`.

Reference IDs by entry type:

| Entry | `reference_id` |
| :--- | :--- |
| `USAGE_DEDUCTION` | `AgentCallLog.provider_call_id` |
| `USAGE_ADJUSTMENT` | `{provider_call_id}:adj:{n}` |
| `MANUAL_TOP_UP`, `CYCLE_ALLOCATION` | `Transaction.razorpay_order_id` |
| `ROLLOVER_FORFEIT`, `ROLLOVER_CAP_FORFEIT`, `EXPIRY_FORFEIT` | source `Subscription.id` |
| `ADMIN_ADJUSTMENT` | staff-supplied ticket reference |
| `OPENING_BALANCE` | `wallet:{wallet_id}` — only when opening at a **non-zero** balance |

### 3.3 `CreditRate` — `wallet/models/credit_rate.py`

Plan-scoped, not tenant-scoped — extends `BaseModelCore`. This is the **catalogue**: what
each plan charges for each operation. It is read when a plan's rate card is stamped onto a
wallet (D9), and by the admin — never on the call path.

```python
class CreditRate(BaseModelCore):
    class Operation(models.TextChoices):
        # Call tiers only, and the set is closed. Direction is deliberately not
        # priced: an inbound and an outbound minute on the same tier cost the same.
        STATIC_REEL               = "static_reel",               "Static Reel"
        CONVERSATION_HIGH_LATENCY = "conversation_high_latency", "Standard"
        CONVERSATION_LOW_LATENCY  = "conversation_low_latency",  "Realtime"
        # No storage: it is a PlanEntitlement allowance with a rupee add-on, not
        # credits per GB-month (§10.3). No SMS: it is not sold.

    class Unit(models.TextChoices):
        PER_EVENT     = "per_event",     "Per Event"       # 1 call = N credits
        PER_SECOND    = "per_second",    "Per Second"
        PER_MINUTE    = "per_minute",    "Per Minute"

    class Rounding(models.TextChoices):
        CEIL    = "ceil",    "Round Up"
        FLOOR   = "floor",   "Round Down"
        NEAREST = "nearest", "Round To Nearest"

    operation        = CharField(max_length=32, choices=Operation.choices, db_index=True)
    unit             = CharField(max_length=32, choices=Unit.choices)
    credits_per_unit = PositiveIntegerField()
    rounding         = CharField(max_length=16, choices=Rounding.choices, default=CEIL)
    minimum_units    = PositiveIntegerField(default=1)   # minimum billable quantity
    plan             = FK("onboard.Plan", on_delete=CASCADE,
                          related_name="credit_rates")   # required — rates are plan terms
    effective_from   = DateTimeField()
    effective_to     = DateTimeField(null=True, blank=True)   # null = still current
    notes            = TextField(blank=True, null=True)
```

```python
class Meta:
    db_table = "wallet_credit_rate"
    ordering = ["operation", "-effective_from"]
    constraints = [
        CheckConstraint(condition=Q(effective_to__isnull=True) | Q(effective_to__gt=F("effective_from")),
                        name="ck_rate_period_valid"),
        UniqueConstraint(fields=["operation", "plan"],
                         condition=Q(effective_to__isnull=True, is_deleted=False),
                         name="uniq_current_rate_per_operation_plan"),
    ]
```

That last constraint is the one that makes D4 stick: **at most one open-ended rate per
(operation, plan)**. Publishing a new price without closing the old one fails at the
database, rather than producing two "current" rates and a coin-flip at call time.

`minimum_units` is the fine print that telephony billing always has — a 4-second call on a
per-minute rate still bills one minute. Default 1, so it does nothing unless configured.

**`plan` is required.** A rate with no plan would be a platform-wide default, and the moment
one exists someone relies on it and per-plan pricing stops being trustworthy — "did this
tenant get the Pro rate or the fallback?" becomes a question you have to investigate rather
than read. Every plan carries a full set of rates, including free plans (price the
operations at 0 rather than omitting the rows).

**Every plan needs a complete rate card.** A plan missing a rate for an operation its tenants
can perform means `RateNotConfigured` at call time.

> **This originally called for a `wallet/checks.py` system check that failed the deploy,
> and that was the wrong shape.** `manage.py check` runs before every `migrate`, so a
> gate on catalogue completeness meant an admin adding a plan through the admin could
> stop the next deploy — and it blocked its own seeding migration, because Django runs
> checks first. Rate cards are data an admin owns, not a code invariant.
>
> **As built:** rates were seeded from `json_files/credit_rate.json` (since removed — rate
> cards are now created with their plan in the super admin panel), and
> `manage.py audit_billing_readiness` *reports* every active plan with a missing rate
> without blocking anything. `report_unrated_calls` catches whatever still reaches a
> call, and `resolve_rate` raises loudly rather than charging zero.

### 3.4 Entitlements live in `onboard`, not here — declared here, enforced there ✅

**Superseded.** This section originally put seven `max_*` columns on `Plan`. Entitlements
then gained a per-plan add-on price and an availability flag, which is four attributes ×
seven resources — the point at which columns stop being the answer. They are now two models
in `onboard`, and the columns have been removed:

| Model | Holds |
| :--- | :--- |
| `PlanEntitlement` | One row per (plan, resource): included quantity, add-on available, ₹ per block, block size, ceiling |
| `TenantEntitlement` | One row per (tenant, resource): extras bought, at a price frozen on purchase |

**They are priced in rupees, recurring per cycle** — not in credits. That is what puts them
in `onboard` beside `Plan` and `Transaction` rather than here: buying an add-on changes the
tenant's subscription price, and no row in either model ever produces a ledger entry.

These are **gauges** (§1.1). They exist now so plans are fully describable and the pricing
page has something to render; enforcement is §9. Declaring them early stops someone
"temporarily" charging credits for an agent seat.

**They are frozen with the subscription, not with the wallet** (D9). Caps go into
`Subscription.plan_snapshot` alongside the money and the credit rules, because that is the
contract; only the rate card goes to the wallet, because that is the meter. So the later
entitlement checker (§9) reads the active subscription's snapshot, and the wallet stays
purely about credits — no cap ever ends up being enforced by a balance.

One consequence worth stating: raising a cap on a plan does **not** immediately raise it for
existing subscribers — they get it at their next cycle, exactly as they get a new price or a
new rate. If a cap increase should apply immediately, that is a deliberate re-stamp, not a
side effect of an admin edit.

### 3.5 Registration

- `wallet/apps.py` → `WalletConfig`; add `"wallet"` to `INSTALLED_APPS` after `"core"`.
- `wallet/models/__init__.py`, `admin/`, `serializers/`, `services/`, `urls/`, `tests/` —
  package-per-concern, one class per file, per AGENTS.md §2.
- `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]`: `CreditEntryTypeEnum`, `CreditBucketEnum`,
  `CreditOperationEnum`, `CreditUnitEnum` — the settings file already maps several enums
  ([base.py:310-325](../../config/settings/base.py#L310-L325)) and unmapped duplicates make
  `drf-spectacular` warn.
- `TenantSettings.credits_enforced` (D7) — a field on the existing model, plus one migration
  in the `tenant` app.

### 3.6 Migrations

| App | # | Contents |
| :--- | :--- | :--- |
| `wallet` | `0001` | `Wallet`, `CreditLedger`, `CreditRate` with all constraints and indexes. |
| `wallet` | `0004` | Data: create a `Wallet` for every live Tenant. **No ledger row** — see the note below. Idempotent; skips tenants that already have one. |
| `wallet` | `0003` | Data: seed **a complete rate card for every active Plan** from §13/Q1's agreed numbers — including free plans, priced at 0 rather than omitted. Separate migration so re-pricing never means editing a schema migration. |
| `wallet` | `0004` | Data: stamp `rate_snapshot` onto every existing wallet from its tenant's current plan (D9), so no wallet starts life on the §4.2 fallback path. |
| `onboard` | next | Plan entitlement fields (§3.4). |
| `tenant` | next | `TenantSettings.credits_enforced`, default `False`. |

> **Correction, found in build.** This table originally called for a zero-value
> `OPENING_BALANCE` row per wallet. That row cannot exist: `ck_ledger_amount_non_zero`
> rejects an amount of 0, and rightly so — a zero-credit movement is noise in an audit
> trail. It is also unnecessary. The invariant is `SUM(ledger) == balance`, and an empty
> ledger sums to zero, which is exactly the balance being opened. `OPENING_BALANCE` earns
> its place in the enum for the case it is actually for: opening a wallet at a *non-zero*
> balance — a migration from another system, or a goodwill grant at creation.

Production had no tenants when this was planned; the dev database has 17, and `0004`
created a wallet for each. It must stay safe to re-run.

---

### 3.7 The seed grid — decided

The concrete numbers migrations `0003` and the `onboard` plan migration write. Anchored at
**1 credit ≈ ₹1 of usage value**, against the six plans already in
`json_files/plan.json` (since removed — plans are now created only from the super admin panel).

These are a *starting position*, not final pricing. W5 (§12) exists precisely to correct
them against real traffic before enforcement blocks anyone.

#### Allocation and rates

Higher plans get **both** a bigger allocation and a cheaper per-minute rate.

| Plan (`code`) | Net price | Credits/cycle | Rate | Included minutes | Top-up ₹/credit |
| :--- | ---: | ---: | ---: | ---: | ---: |
| `free_plan` | ₹0 | 100 | 5/min | 20 | — disabled |
| `starter_inr` | ₹999 | 1,000 | 5/min | 200 | ₹1.20 |
| `professional_inr` | ₹2,699 | 3,500 | 3/min | 1,166 | ₹1.00 |
| `starter_inr_yearly` | ₹9,999 | 12,000 | 5/min | 2,400 | ₹1.20 |
| `professional_inr_yearly` | ₹26,999 | 42,000 | 3/min | 14,000 | ₹1.00 |
| `pay_as_you_go_inr` | ₹0 | 0 | 5/min | top-up only | ₹1.20 |

- **Inbound is rated the same as outbound** at launch. Both rows are seeded per plan, so
  making inbound cheaper or free later is a rate edit, not a code change (§13/Q2).
- `minimum_units = 1` everywhere — a one-minute minimum per call.
- **Free plans cannot top up** (`cost_per_additional_credit = 0` disables it, per the
  companion §4.3). A free tenant who wants more should upgrade, not buy credits.
- Yearly allocations are 12× the monthly, while yearly prices are ~10× — the discount is in
  the price, and the credits scale with the time covered.
- Professional's effective value: 5.8× Starter's minutes for 2.7× the price. That gap is the
  upsell, and it is deliberate.

#### Entitlement caps

| Plan | agents | users | contacts | KBs | storage GB |
| :--- | ---: | ---: | ---: | ---: | ---: |
| `free_plan` | 1 | 1 | 100 | 1 | 1 |
| `starter_inr` (+ yearly) | 3 | 3 | 5,000 | 5 | 10 |
| `professional_inr` (+ yearly) | 10 | 10 | 50,000 | 25 | 50 |
| `pay_as_you_go_inr` | 2 | 2 | 1,000 | 3 | 5 |

Declared now, enforced later (§3.4, §9).

#### Rollover and expiry rules

| Plan | renewal | upgrade | downgrade | cap | use after expiry | low-balance alert |
| :--- | :--- | :--- | :--- | :--- | :--- | ---: |
| `free_plan` | — | yes | no | — | **no** | 20 |
| `starter_inr` | no | yes | no | 1× allocation | no | 100 |
| `professional_inr` | **yes** | yes | no | 1× allocation | no | 350 |
| `starter_inr_yearly` | no | yes | no | 1× allocation | no | 1,200 |
| `professional_inr_yearly` | **yes** | yes | no | 1× allocation | no | 4,200 |
| `pay_as_you_go_inr` | **yes** | yes | yes | none | **yes** | 50 |

Rollover on renewal is itself a Professional-tier perk. Rollover on upgrade is universal —
a tenant moving up should never be penalised for the timing.

#### Two consequences of "free plans expire and block"

Free plans now behave exactly like paid ones: the cycle ends, the subscription expires,
access stops. Two things follow that are worth being explicit about rather than discovering.

**1. The Free Tier is a 30-day trial, not a free tier.** After one month a free tenant is
blocked until they pay. That may be exactly the intent — it is a strong upgrade prompt — but
the plan is named "Free Tier" and the pricing page will imply something else. Either rename
it, or accept that free means "free for one month". No code depends on which; the product
copy does.

**2. Pay As You Go cannot use the same rule.** A PAYG tenant pays ₹0 up front and buys
credits by topping up. Under expire-and-block, their ₹0 subscription lapses after a month
and blocks them **while their purchased credits sit unspent in the wallet** — taking money
for credits and then refusing to let them be spent.

So PAYG is treated separately, keyed on `plan_type`:

| `plan_type` | At cycle end |
| :--- | :--- |
| `FREE` | Expire and block. |
| `PAID` | Expire and block; renewal requires payment. |
| `PAY_AS_YOU_GO` | **Auto-renew at ₹0** with a 0-credit allocation. Nothing to charge, nothing to allocate — the cycle simply rolls. |

The distinction is defensible: a free trial is meant to end, whereas a PAYG tenant is a
paying customer holding a balance. It also settles the companion document's Q12 — PAYG needs
no new code beyond this branch, because `allocated_credits=0` plus top-ups plus
`allow_use_after_expiry=true` already *is* pay-as-you-go.

---

## 4. Services — `wallet/services/`

### 4.1 `wallet.py` — the only thing that writes credits

Nothing outside this module mutates `Wallet.balance` or inserts a `CreditLedger` row.

```python
def for_tenant(tenant) -> Wallet
def balance_for(tenant) -> dict            # {balance, allocated, purchased, frozen, spendable}

def grant(tenant, *, entry_type, amount, bucket, reference_id=None,
          subscription=None, performed_by=None, notes="", metadata=None) -> CreditLedger | None
def consume(tenant, *, operation, quantity, reference_id,
            subscription=None, metadata=None) -> CreditLedger | None
def refund(tenant, *, reference_id, notes="") -> CreditLedger | None
def forfeit(tenant, *, entry_type, amount, bucket, reference_id, notes="") -> CreditLedger | None

def quote(tenant, *, operation, quantity) -> RatingResult     # no side effects
def assert_can_consume(tenant, *, operation, quantity) -> None  # raises InsufficientCredits

def freeze(tenant, *, reason) -> Wallet
def unfreeze(tenant) -> Wallet
def set_plan_context(tenant, plan) -> Wallet   # stamp the plan's rate card (D9)
```

Every mutating call has the same shape:

```python
with db_transaction.atomic():
    wallet = Wallet.objects.select_for_update().get(tenant_id=tenant.id)

    if reference_id and CreditLedger.all_objects.filter(
            entry_type=entry_type, reference_id=reference_id).exists():
        return None                            # idempotent replay — D6

    # ... bucket arithmetic, floored at 0 ...
    wallet.balance = wallet.allocated_balance + wallet.purchased_balance
    wallet.save(update_fields=[...])
    return CreditLedger.objects.create(..., balance_after=wallet.balance)
```

Points that matter:

- `select_for_update()` is what stops two concurrent calls both passing the balance check
  and driving the wallet negative.
- The idempotency probe uses `all_objects`, so even a (theoretically impossible)
  soft-deleted row still blocks a duplicate.
- `consume()` drains `allocated_balance` first, then `purchased_balance` (§3.1).
- Returning `None` on replay rather than raising keeps every caller's happy path clean.
- `InsufficientCredits` is raised only when enforcement is on (D7); it maps to HTTP 402.
- `grant()` deliberately works on a frozen wallet — a renewal payment must be able to land.

### 4.2 `rating.py` — turning usage into credits

```python
def resolve_rate(wallet, operation) -> ResolvedRate      # from the wallet's stamp (D9)
def rate_quantity(resolved, raw_quantity) -> RatingResult
def build_snapshot(plan, *, at=None) -> dict             # plan's rate card → snapshot JSON
def stamp(wallet, plan) -> Wallet                        # called by the subscription engine
```

`resolve_rate` reads `wallet.rate_snapshot["operations"][operation]` — no query, no join,
and the wallet row is already locked and in memory on the consumption path. Order of
resolution:

1. The wallet's `rate_snapshot`. This is the answer in every normal case.
2. **Fallback:** the live `CreditRate` rows for `wallet.current_plan`, effective now — for a
   wallet stamped before this feature or created outside the normal flow. Logs a warning,
   because a wallet without a stamp means a subscription path skipped `set_plan_context`.
3. `RateNotConfigured`, raised loudly. Silently charging zero is how a platform gives away
   its product; `report_unrated_calls` (§7) catches anything that still slips through.

`build_snapshot` reads the catalogue (`CreditRate` rows for the plan, effective now) and
produces the JSON in §3.1. It is the **only** place that reads `CreditRate` outside admin.

`rate_quantity` is the whole of D2, and it is worth reading as arithmetic:

```python
UNIT_SECONDS = {Unit.PER_SECOND: 1, Unit.PER_MINUTE: 60}

# a 90-second outbound call, on a wallet stamped from a plan rated 5 credits/minute
raw_quantity = 90                                  # seconds
divisor      = UNIT_SECONDS[resolved.unit]         # 60
units        = ceil(90 / 60)                       # 2      (resolved.rounding)
units        = max(units, resolved.minimum_units)  # 2
credits      = units * resolved.credits_per_unit   # 10

# the same call on a Pro wallet stamped at 3 credits/minute costs 6, not 10 —
# the only thing that differs is the stamp the wallet is carrying
```

`PER_EVENT` skips the divisor entirely: `units = 1`, `credits = credits_per_unit`.

`RatingResult` is a frozen dataclass carrying `credits`, `units`, `unit_type`,
`rate_applied`, `raw_quantity` and the `CreditRate` itself — exactly the fields
`CreditLedger` snapshots (D3), so the quote endpoint, the consumption path and the ledger row
all render from one computation instead of three that can disagree.

### 4.3 `policy.py` — what an operation means

Maps a domain object to `(operation, quantity)`, and nothing else:

```python
def operation_for_call(call_log) -> tuple[str, int]:
    """('outbound_call', 90) — the operation name and the raw quantity to rate."""
```

Isolated so that switching from per-minute to per-call, or starting to bill inbound
differently from outbound, is one edit in one file with one test.

---

## 5. Consumption path

### 5.1 Where the deduction happens

`AgentCallLogViewSet.perform_create`
([agent_call_log.py:189](../../core/views/agent_call_log.py#L189)) is the only place a
completed call lands, and `provider_call_id` is already unique — a free idempotency key
(D6). After the row saves:

```python
wallet.consume(
    tenant=tenant,
    operation=operation,          # from policy.operation_for_call
    quantity=raw_quantity,
    reference_id=call_log.provider_call_id,
)
```

**Deduct after, authorise before.** Two distinct operations, and conflating them is how
credit systems leak. `assert_can_consume()` runs *before* work starts — campaign start in
[core/tasks.py](../../core/tasks.py) `start_due_campaigns`, and when the runner pulls
campaign context. The runner is an external service that can fail and retry, so only the
post-facto keyed deduction is safe as the source of truth.

### 5.2 The duration problem

`AgentCallLog.duration_seconds` may be absent or zero when the row is first written and
corrected later — the runner does not always know the final duration at post time.

- Deduct on the first write that carries a **non-null, non-zero** duration. A row created
  with no duration is not rated yet.
- The `provider_call_id` reference means later updates cannot double-charge — the second
  attempt is a no-op.
- If a duration is *corrected upward* later, post the difference as `USAGE_ADJUSTMENT` with
  reference `{provider_call_id}:adj:{n}` rather than editing the original row (append-only).
- A call that never gets a duration is never charged, and the reconciliation job (§7)
  reports it as unrated so it can be chased rather than silently lost.

Confirm the runner's actual posting behaviour before building this — §13/Q3.

### 5.3 Enforcement is off at first

Per D7, Phase W4 ships metering only. `consume()` writes real ledger rows from day one;
`assert_can_consume()` logs `would-refuse` at warning level and returns. After a cycle of
real traffic, compare ledger burn against revenue, correct the rates if needed, and only
then flip `credits_enforced`.

---

## 6. API surface

Under the companion plan's `/api/v1/billing/` prefix, routed from `wallet/urls/`, so the
frontend sees one billing surface. All endpoints `IsAuthenticated`, tenant resolved through
`TenantBaseAPIView.get_tenant()`
([tenant_base_api_view.py](../../tenant/common/views/tenant_base_api_view.py)), responses via
`ResponseMixin.success/error` like every other view in the repo.

| Method | Path | Purpose |
| :--- | :--- | :--- |
| GET | `/billing/wallet/` | Balance, both buckets, frozen state and reason, low-balance threshold |
| GET | `/billing/wallet/ledger/` | Paginated history; filter by `entry_type`, `bucket`, `operation`, date range |
| GET | `/billing/wallet/rates/` | The rate card stamped on this wallet — what each operation costs *on their plan*, straight from `rate_snapshot` |
| POST | `/billing/wallet/quote/` | `{operation, quantity}` → credits it would cost. No side effects |

`GET /billing/wallet/ledger/` returns the rating snapshot inline, which is the point of D3:

```jsonc
{
  "id": "…", "entry_type": "usage_deduction", "bucket": "allocated",
  "amount": -10, "balance_after": 1230,
  "operation": "outbound_call",
  "rating": { "raw_quantity": 90, "unit_type": "per_minute", "units": 2, "rate_applied": 5 },
  "reference_id": "call_9fA…", "created_at": "2026-08-23T09:14:22Z"
}
```

A tenant asking "why 10 credits?" reads the answer off the row instead of opening a ticket.

Serializers follow the AGENTS.md 4-tier pattern; `CreditLedger` needs no write path at all,
so its "main" serializer is read-only by construction.

---

## 7. Background jobs — `wallet/tasks/` *(a package, as built)*

Registered in `CELERY_BEAT_SCHEDULE`
([base.py:663-714](../../config/settings/base.py#L663-L714)), using the `ReportedTask` base
from [core/tasks.py](../../core/tasks.py).

| Task | Cadence | Does |
| :--- | :--- | :--- |
| `reconcile_wallets` | weekly | Assert D5 for every wallet; log one structured error per drift. **Reports, never repairs** — auto-correction hides the bug that caused it. |
| `report_unrated_calls` | daily | `AgentCallLog` rows with a duration but no `USAGE_DEDUCTION` (§5.2). Revenue quietly not charged. |
| `send_low_balance_alerts` | daily | Balance at or below `low_balance_threshold`, deduped via `low_balance_notified_at`. |

Plus a management command `wallet/management/commands/reconcile_wallets.py` wrapping the same
logic with a `--fix` flag for deliberate operator repair.

---

## 8. Admin

- **`CreditLedger`** — `BaseAdminCore`, with `has_add_permission`, `has_change_permission`
  and `has_delete_permission` all `False`. List: tenant, entry type, bucket, amount,
  balance_after, operation, reference, created_at.
- **`Wallet`** — balances **read-only** (the only sanctioned way to move credits is through
  the service). Two intermediate-form actions, **Grant credits** and **Deduct credits**,
  each taking an amount and a reason and writing via `wallet.grant()` with
  `performed_by=request.user`. This is what keeps staff out of the raw balance field. Plus
  **Freeze** / **Unfreeze** actions.
- **`CreditRate`** — editable, but the admin must steer toward D4: a "Supersede this rate"
  action that closes the current row (`effective_to = now`) and opens a pre-filled new one,
  so re-pricing is one click and never an in-place edit. The uniqueness constraint in §3.3
  blocks the wrong path anyway.

  Two behaviours verified against the live schema and worth building to:

  - **Supersede cannot close a rate at its own `effective_from`.**
    `ck_credit_rate_period_valid` requires `effective_to > effective_from`, so a
    zero-length validity window is rejected — correctly, since such a row was never in
    force. The action must therefore guard `effective_to = max(now, effective_from + ε)`
    rather than assuming `now` is always later.
  - **The escape hatch for a rate published by mistake is soft-delete, not supersede.**
    `uniq_current_rate_per_plan_operation` is predicated on `NOT is_deleted`, so soft-
    deleting a bad row frees the slot immediately and a corrected rate can take its place
    at the same instant. That is the only path that works for a same-second correction.

---

## 9. What quota enforcement looks like — built, in `onboard` ✅

Deferred out of v1 and built afterwards, to this shape and in this place. The reason for
recording it here was to stop anyone improvising it into the wallet later, and that
held: **nothing in `wallet/` knows what an entitlement is.**

```python
# onboard/services/entitlements.py  — a later phase, NOT in this app
def check(tenant, resource, *, requested=1) -> None   # raises QuotaExceeded
def usage(tenant) -> dict                             # {resource: {used, included, extra, limit}}
```

**It belongs in `onboard`, not `wallet`.** An earlier draft of this section put it at
`wallet/services/entitlements.py`; that was wrong. The checker reads
`Subscription.plan_snapshot` for the included amount and `TenantEntitlement` for purchased
extras — both `onboard` models, both priced in rupees. Nothing it touches is a credit, so
putting it here would give the wallet a dependency on the subscription engine for no reason.

The effective limit is `included + additional_quantity`, and a **gauge** throughout —
counted live, never a ledger, never a balance. It hooks into `perform_create` on the
relevant viewsets (agents, contacts, knowledge bases, KB websites), which is why it is its
own phase: it touches many existing views rather than living in one service.

---

## 10. Seam with the subscription plan

### 10.1 Who owns what

| Concern | Owner |
| :--- | :--- |
| Wallet, balance, buckets, freeze state | **this document** |
| Ledger, entry types, idempotency | **this document** |
| `CreditRate` model, rating engine, consumption | **this document** |
| Stamping a plan's rate card onto a wallet (D9) | **this document** |
| Plan credit *rules* (allocation, rollover flags, caps) | companion |
| Entitlement caps and their enforcement | companion / §9 |
| Subscription lifecycle, expiry, scheduled changes | companion |
| Proration, Razorpay orders, top-up purchase | companion |
| Rollover *policy* — how much survives a plan change | companion |
| **The actual rate and cap numbers per plan** | neither — a pricing decision (§13/Q1) |

### 10.2 The five calls the subscription engine makes

That is the entire interface. Nothing else reaches into the wallet.

```python
# 1. On activation, renewal, and any applied plan change — stamp the plan's terms (D9).
#    Do this FIRST: a grant that lands before the stamp is credited against the old
#    plan's rate card, and any call in between rates at the wrong price.
wallet.set_plan_context(tenant, plan)

# 2. Deposit the cycle's allocation.
wallet.grant(tenant, entry_type=CYCLE_ALLOCATION, amount=plan.allocated_credits,
             bucket=ALLOCATED, reference_id=txn.razorpay_order_id, subscription=sub)

# 3. Deposit a mid-cycle top-up.
wallet.grant(tenant, entry_type=MANUAL_TOP_UP, amount=txn.credits_purchased,
             bucket=PURCHASED, reference_id=txn.razorpay_order_id)

# 4. Burn what the old plan's rollover rules do not let them keep.
wallet.forfeit(tenant, entry_type=ROLLOVER_FORFEIT, amount=n, bucket=ALLOCATED,
               reference_id=str(old_sub.id))

# 5. Stop spending when a cycle lapses; resume on renewal.
wallet.freeze(tenant, reason="subscription_expired")
wallet.unfreeze(tenant)
```

Note what is *absent*: no transfer, no close-out, no balance move on a plan change. The
wallet persists; only the forfeit amount is a policy question, and that policy lives in the
companion document.

**A plan change is therefore three calls in one transaction** — stamp the new rate card,
forfeit what does not roll over, grant the new allocation. The tenant's balance never moves
between rows, and from the next call onward they are rated at their new plan's price.

### 10.3 Metered overage — considered and rejected

An earlier draft planned to bill storage-beyond-quota as a `CreditRate` with
`unit = PER_GB_MONTH` plus a monthly job converting usage into a `consume()` call: a gauge
turned into a flow by a clock.

**That is no longer the plan, and the rate card no longer contains a `storage` operation.**
Storage is sold the way every other capacity is — a `PlanEntitlement` allowance with a
rupee-priced add-on (§3.4). It stays a gauge from end to end.

The reason is §1.1's delete-test. Charging credits per GB-month makes deleting a file a
refund question with no good answer: re-credit the unused days, and a tenant can churn
uploads to arbitrage the meter; do not, and they paid for storage they released. As a
recurring entitlement, deleting a file simply frees the allowance and the next cycle costs
less. Nothing to reconcile, nothing to refund.

`CreditRate.Operation` is therefore closed to the three call tiers. SMS is absent for a
simpler reason — it is not sold. Should genuinely consumable metering ever be needed, it is
a new enum member and a seeded row, not a redesign.

---

## 11. Tests

Run with the Postgres settings — the default SQLite test DB cannot handle this codebase's
`ArrayField` usage:

```
python manage.py test wallet --settings=config.settings.test_postgres
```

| File *(as built)* | Covers |
| :--- | :--- |
| `wallet/tests/test_models.py` | The two catalogue-and-state models at the database: the bucket-sum constraint, one live wallet per tenant, one open-ended rate per plan and operation, and the period-validity check. Asserted as `IntegrityError`, not as Python that happens to check first |
| `wallet/tests/test_rating.py` | The stamp is what prices a call: 90s on a 5/min stamp → 10 credits, the same call on a 3/min stamp → 6. `ceil` / `floor` / `nearest`; `minimum_units` on a 4-second call; `PER_EVENT` ignores duration. Resolution order — stamp, then live-plan fallback with a warning, then `RateNotConfigured`. Re-stamping changes the price of the *next* call and none of the previous ones |
| `wallet/tests/test_policy.py` | Which tier a call is billed at: the call log beats the agent, the agent is the fallback, and `DEFAULT_TIER` is the standard model rather than the expensive one. Plus the drift guard — every `ServiceTier` value must be a priceable `CreditRate.Operation` |
| `wallet/tests/test_wallet_service.py` | grant / consume / refund / forfeit / adjust arithmetic; allocated drains before purchased; `balance_after`; the D5 invariant after a seeded 50-operation sequence; a replayed `reference_id` returns `None` and writes nothing; `InsufficientCredits` only when `credits_enforced`; `grant()` lands on a frozen wallet; two concurrent consumes on a one-credit wallet produce exactly one deduction |
| `wallet/tests/test_ledger_immutability.py` | `save()` on an existing row raises; `delete()`, `soft_delete()`, `restore()`, `deactivate()` and every bulk queryset path raise; the admin exposes no add, change or delete. **The queryset case is the one that matters** — `BaseQuerySet.soft_delete()` would leave the row in the table and out of every sum |
| `wallet/tests/test_wallet_api.py` | Auth on all four endpoints; tenant isolation; `quote` has no side effects; `/rates/` returns the wallet's stamp rather than the live plan; 405 on every ledger write verb |
| `wallet/tests/test_wallet_tasks.py` | Unrated-call reporting, reconciliation (reports, never repairs), low-balance alerts and their quiet window. Plus that every task name matches its beat-schedule entry |
| `wallet/tests/test_admin_actions.py` | Grant and deduct go **through the service**; a reason is required; a double-submitted form pays out once; balances are read-only; superseding a rate created moments ago does not raise |
| `wallet/tests/test_query_budget.py` | `resolve_rate` costs **0 queries**; `consume` never touches `CreditRate`; the balance endpoint does not grow with ledger size. Exact counts, not ceilings |
| `wallet/tests/test_concurrency.py` | Two real connections with a deterministic hand-off. A `TestCase` rolls back its transaction, so the race being tested *cannot happen* there |
| `core/tests/billing/test_credit_consumption.py` | Call-log create deducts exactly once; the same `provider_call_id` twice deducts once; a row with no duration is not rated; **billing never fails the request** |

> **`test_checks.py` was dropped along with `wallet/checks.py`.** The completeness check
> refused to boot when a plan had no rate card — a deploy gate on data an admin owns.
> `audit_billing_readiness` reports the same condition without blocking anything.

Two properties are worth asserting directly rather than only through examples, because they
are what the whole module rests on:

- **Ledger sum equals balance**, after any sequence of operations (D5).
- **Replaying any operation changes nothing**, for every entry type that carries a
  `reference_id` (D6).

`CreditLedger` and `Wallet` extend the Tier-1 core base classes, so they have no
`created_by` / `updated_by` fields and `full_clean()` is safe to call on them directly —
unlike the audited Tier-2 models elsewhere in this codebase, where those fields are
populated at save time and a bare `full_clean()` always fails.

---

## 12. Build order

Six phases. W0-W4 are **done**; W5 is a decision, not work.

Phases W0-W2 had no dependency on the subscription work and were built and merged first,
which is what let the wallet ship before any of the subscription engine existed.

### W0 — App scaffold and models ✅
`wallet` app, three models, migrations `0001`-`0002`, `Plan` entitlement fields,
`TenantSettings.credits_enforced`, admin registration.
**Done when:** every existing tenant has a wallet, and
`reconcile_wallets` reports zero drift.

### W1 — Rating engine and the plan stamp ✅
`rating.py` (`resolve_rate`, `rate_quantity`, `build_snapshot`, `stamp`), `policy.py`,
`set_plan_context`, `CreditRate` admin with the supersede action.

*(As built: the seed migration and `wallet/checks.py` were both removed. Whatever an
admin configures is final, so a hard-coded catalogue and a deploy gate on it were the
wrong shape. Rates were then seeded from `json_files/credit_rate.json`, and
`audit_billing_readiness` reports plans with holes. That seed file has since been removed
too: plans and their rate cards are created only from the super admin panel.)*
**Done when:** a 90-second call on a 5-credits/minute stamp rates to exactly 10 credits and
the same call on a 3-credits/minute stamp rates to 6; a 4-second call bills
`minimum_units`; re-stamping a wallet changes the price of the *next* call and none of the
previous ones; and `manage.py check` fails when any active plan is missing a rate.

### W2 — Wallet service ✅
`grant` / `consume` / `refund` / `forfeit` / `quote` / `freeze`, reconciliation command.
**Done when:** the D5 invariant survives a randomised sequence of operations, a replayed
`reference_id` writes nothing, and two concurrent `consume` calls on a one-credit wallet
produce exactly one deduction.

### W3 — Read surface ✅
Four endpoints, serializers, docs.
**Done when:** a tenant can read their balance and see why any single deduction was the size
it was.

### W4 — Consumption, metered only ✅ *(enforcement off — D7)*
`consume` on call-log create, `assert_can_consume` at campaign start and in the runner
context, `report_unrated_calls`.
**Coordinate with the runner team first** — §5.2 depends on when it posts durations, and the
eventual refusal is a new failure mode it must not retry into.
**Done when:** every completed call writes exactly one deduction, a call posted twice
deducts once, and a zero-balance tenant is logged as would-be-refused but still runs.

### W5 — Flip enforcement ⏳ *(a decision, not a deploy — still open)*
After a full cycle of real traffic: compare ledger burn against revenue, correct
`CreditRate` if needed, then enable `credits_enforced`.

---

## 13. Open questions

| # | Question | Recommendation |
| :--- | :--- | :--- |
| Q1 | ~~What are the actual numbers, per plan?~~ | **RESOLVED — §3.7.** Anchored at 1 credit ≈ ₹1; higher plans get both a bigger allocation and a cheaper rate (Starter 5/min, Professional 3/min). Seeded now, corrected at W5. |
| Q2 | ~~Are inbound calls charged at all?~~ | **RESOLVED — §3.7.** Rated the same as outbound at launch, with both rows seeded per plan. Making inbound cheaper or free is a rate edit, not a code change — revisit once there is real inbound volume to look at. |
| Q3 | **When does the runner post `duration_seconds`?** | **Built for post-on-completion.** The call-log endpoint is POST-only, so a row arriving with no duration is recorded, not charged, and `report_unrated_calls` surfaces it daily. If the runner ever moves to create-then-update, the deduction has to move to the update path and `USAGE_ADJUSTMENT` becomes load-bearing rather than an edge case. **Still worth confirming with the runner team.** |
| Q4 | **Refund the credit for a failed or unanswered call?** | **Still open, and the mechanism is built but unwired.** `wallet_service.refund()` exists, restores each bucket exactly as it was drawn, and is idempotent — but **nothing calls it outside the tests.** Deciding this is choosing which `outcome` values earn a refund, and needs the runner's taxonomy. Until then a failed call is charged like any other. |
| Q5 | ~~Should a frozen wallet block *all* spending, or allow a small negative buffer?~~ | **RESOLVED — built as recommended.** `assert_can_consume` refuses on a frozen wallet; `consume` deliberately does **not** check `is_frozen`, so a call already in flight completes and is charged. That asymmetry is the design, not an oversight: the block is at authorisation, and a call that happened gets paid for. No overdraft concept exists. |
| Q6 | ~~Which rate applies when there is no live plan?~~ | **RESOLVED — built as recommended.** `freeze()` touches three columns and none of them is `rate_snapshot`, so a lapsed tenant keeps the stamp of the plan that granted their credits and spends at that plan's price. |

---

## 14. Risks

| Risk | Mitigation |
| :--- | :--- |
| **Double-charging on a retry.** Real money, silent. | `(entry_type, reference_id)` unique constraint (D6), with `provider_call_id` as a naturally unique key. Tested explicitly. |
| **Balance drifts from the ledger.** | Single-writer service, `select_for_update` on every mutation, bucket-sum check constraint, weekly reconciliation that reports rather than repairs. |
| **Concurrent consumption oversells.** Two calls, one credit left. | Row lock in `consume`; the second re-reads the spent balance and returns `None` without writing. *(As built: `consume` returns `None` rather than raising — `assert_can_consume` is what raises, at authorisation. Proven on two real connections in `test_concurrency.py`.)* |
| **No rate configured for an operation.** | `RateNotConfigured` raised loudly rather than charging zero. *(As built: the deploy-gating check was removed — it blocked boot on data an admin owns. `audit_billing_readiness` reports plans with missing rates, and `report_unrated_calls` catches anything that still slips through.)* |
| **A subscription path forgets to stamp the wallet**, so a tenant is rated at their previous plan's price after an upgrade. | `set_plan_context` is call #1 in §10.2 and lives on the same three paths as the grant. The §4.2 fallback logs a warning whenever a wallet is missing a stamp, which makes the omission visible rather than silently mispriced. Test that a plan change re-prices the next call. |
| **The rate is simply wrong.** | D7 — meter for a full cycle with enforcement off, compare burn to revenue, correct, *then* flip. |
| **Calls arrive without a duration and are never charged.** | `report_unrated_calls` surfaces them daily; §13/Q3 settles the posting contract before W4. |
| **Someone models a quota as a credit.** | §1.1's delete-test, entitlement fields declared up front (§3.4), and §9's sketch so the later implementation had somewhere to go that was not the wallet. **It held** — enforcement was built in `onboard`, and `EN-20` asserts that buying capacity writes no ledger row at all. |

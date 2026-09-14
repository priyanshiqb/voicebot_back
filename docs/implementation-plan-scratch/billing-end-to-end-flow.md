# Billing, Credits & Entitlements — End-to-End Reference

Every model involved in charging a tenant, and exactly what each one holds at each step
of the lifecycle: plan setup → trial → upgrade → add-ons → usage → renewal → expiry.

**Companion documents:** [credit-wallet-module-plan.md](credit-wallet-module-plan.md) (the
wallet's internals) and [subscription-credits-wallet-plan.md](subscription-credits-wallet-plan.md)
(the subscription engine). This document is the map; those two are the territory.

## Status

**All of it is built, migrated and tested.** Every model, every lifecycle step and every
rule below is live; the ✅ marks are kept because they are the record of what was
verified, not a to-do list.

Coverage: **688 test cases** across `wallet/tests/`, `onboard/tests/` and
`core/tests/billing/` — see
[billing-test-plan.md](billing-test-plan.md). Two suites are opt-in because they leave
the machine: real Razorpay test-mode orders, and real Firebase pushes.

What genuinely remains is in [Part 4](#part-4--what-is-left), and none of it is code.

---

# Part 1 — The models

Twelve models take part. Grouped by what they answer.

| Group | Models | Answers |
| :--- | :--- | :--- |
| **Catalogue** | `Plan`, `PlanEntitlement`, `CreditRate` | What is on sale, and at what price |
| **The contract** | `Subscription`, `TenantEntitlement` | What this tenant bought, and when it ends |
| **The money** | `Transaction`, `RazorpayWebhookLog`, `BillingAddress` | What was charged and whether it cleared |
| **The meter** | `Wallet`, `CreditLedger` | What they have left, and where it went |
| **The switch** | `TenantSettings` | Whether a shortfall actually blocks work |
| **The usage** | `AgentCallLog` | What was consumed, and at which tier |

---

## 1.1 `Plan` ✅ — `onboard_plan`

The product on the pricing page. Prices, credit rules, and cycle behaviour.

| Field | Type | Description |
| :--- | :--- | :--- |
| `code` | char, unique | Machine identifier, e.g. `starter_inr`. What seeds and migrations key on |
| `name` | char, unique | Display name |
| `description` | text | Marketing copy for the pricing page |
| `base_price` | decimal | Gross price before discount and tax |
| `discount_percentage` | decimal | e.g. `10.00` |
| `tax_percentage` | decimal | e.g. `18.00` (GST) |
| `currency` | char | `INR` only today |
| `billing_cycle` | char | `monthly` / `yearly` — how long one cycle lasts |
| `plan_type` | char | `free` / `paid` / `pay_as_you_go`. **Nothing branches on this** |
| `sort_order`, `is_most_popular` | int, bool | Pricing page ordering and badge |
| `allocated_credits` | int | Credits granted at the start of every cycle |
| `cost_per_additional_credit` | decimal | Pre-tax ₹ per credit on a top-up. **`0.00` disables top-ups** |
| `max_rollover_credits` | int, null | Ceiling on credits carried into the next cycle. Null = uncapped |
| `allow_rollover_on_renewal` | bool | Do unused plan credits survive a same-plan renewal? |
| `allow_rollover_on_upgrade` | bool | …survive a move to a higher plan? |
| `allow_rollover_on_downgrade` | bool | …survive a move to a lower plan? |
| `allow_use_after_expiry` | bool | May they spend a leftover balance after the cycle lapses? |
| `low_balance_threshold` | int | Alert at or below this balance. 0 disables |
| `renews_without_payment` | bool | Cycle rolls over with no order and no payment. Constrained to zero-cost plans |

Derived, read-only properties: `discount_amount`, `net_base_price`, `tax_amount`,
`total_price`. **`total_price` is what gets charged** — it is multiplied by 100 and handed
to Razorpay.

> **Why prices-per-minute are not here.** They vary by service tier and need version
> history, so they are `CreditRate` rows. Why caps are not here: they need an included
> amount, an availability flag, a price and a ceiling each — four columns × seven
> resources — so they are `PlanEntitlement` rows.

## 1.2 `PlanEntitlement` ✅ — `onboard_plan_entitlement`

One row per (plan, resource). What the plan includes, and what more of it costs.

| Field | Type | Description |
| :--- | :--- | :--- |
| `plan` | FK → Plan | Cascade — entitlements die with the plan |
| `resource` | char | `agent` / `user` / `contact` / `knowledge_base` / `crawl_page` / `campaign_monthly` / `storage_gb` |
| `included_quantity` | int, null | Included at no extra cost. **Null = unlimited** |
| `reset_period` | char | **What the number means.** `none` = a standing total; `daily` / `weekly` / `monthly` / `quarterly` / `half_yearly` / `yearly` = an allowance that refills |
| `is_addon_available` | bool | May they buy beyond the included amount? |
| `price_per_additional` | decimal | Pre-tax ₹ per **block**, charged **every cycle** |
| `addon_step` | int | Block size. 1 for seats; 10 for storage sold in 10 GB blocks |
| `max_additional` | int, null | Ceiling on extras, in units. Null = uncapped |

Constraints: unique `(plan, resource)`; **add-ons cannot be enabled on an unlimited
allowance**; `addon_step >= 1`.

> **`reset_period` decides which of two questions "used" answers.** Left at `none` it is
> a standing total counted live from the rows that *are* the resource — delete an agent
> and the seat comes back. Any other value makes it windowed: the *act* is counted from
> when it happened, taken from `all_objects` so a deletion cannot hand the allowance
> back, and the window runs from the tenant's own subscription rather than the calendar.
>
> Campaigns are the only resource shipping windowed. They were previously hard-coded to
> the calendar month, which had two defects: a cycle running 5 Aug — 4 Sep contained two
> allowances (twelve on a yearly plan), and the count came from the default manager, so
> a tenant could run their campaigns, delete them, and run the same number again.
> Campaigns now count from creation, stop counting only if deleted *before ever running*,
> and a campaign that ran cannot be deleted at all.

Helper: `price_for(units)` → ceiling-divides into blocks, so 12 GB on a 10 GB step bills 2
blocks.

## 1.3 `CreditRate` ✅ — `wallet_credit_rate`

One row per (plan, operation, validity window). The per-minute price list.

| Field | Type | Description |
| :--- | :--- | :--- |
| `plan` | FK → Plan | **Required.** No platform-wide fallback rate exists |
| `operation` | char | `static_reel` / `conversation_high_latency` / `conversation_low_latency` — **call tiers only** |
| `unit` | char | `per_event` / `per_second` / `per_minute` |
| `credits_per_unit` | int | Credits per billable unit. Integer — no fractional credits |
| `rounding` | char | `ceil` / `floor` / `nearest`. Applied to partial units |
| `minimum_units` | int | Smallest billable quantity. A 4-second call still bills 1 minute |
| `effective_from` / `effective_to` | datetime | Validity window. `effective_to` null = current |
| `notes` | text | Why this rate exists or was superseded |

Constraints: **at most one open-ended rate per (plan, operation)**; `effective_to >
effective_from`.

> **Not read on the call path.** Only when stamping a wallet, and by the admin. Rating a
> call reads `Wallet.rate_snapshot`.

> **The operation set is closed to call tiers.** Storage is a `PlanEntitlement` allowance
> priced in rupees, not credits per GB-month, and SMS is not sold. See
> [Storage is the case that proves the rule](#storage-is-the-case-that-proves-the-rule).
> Direction is deliberately not priced either — an inbound and an outbound minute on the
> same tier cost the same, because pricing direction too would mean six operations rather
> than three.

## 1.4 `Subscription` ✅ — `onboard_subscription`

The contract: which plan, for which window.

| Field | Type | Status | Description |
| :--- | :--- | :--- | :--- |
| `tenant` | FK → Tenant | ✅ | Who bought it |
| `plan` | FK → Plan | ✅ | PROTECT — a plan with subscriptions cannot be deleted |
| `status` | char | ✅ | `active` / `cancelled` / `expired` / `trialing` |
| `start_date` | datetime | ✅ | Cycle start |
| `end_date` | datetime | ✅ | Cycle end. Derived in `save()` when unset; backfilled on all 35 rows |
| `plan_snapshot` | json | ✅ | Frozen pricing **+ credit rules + entitlement allowances, each with its refill period**. Read via `rules` and `entitlement_reset_period()` |
| `canceled_at` | datetime | ✅ | When it was voided |
| `scheduled_plan` | FK → Plan | ✅ | A downgrade waiting for cycle end |
| `scheduled_change_at` | datetime | ✅ | When that downgrade fires |
| `superseded_by` | FK → self | ✅ | The subscription that replaced this one |

**Model layer complete** (`onboard.0025`). `save()` fills `end_date` only when unset, so a
renewal that has already pushed it forward is never recomputed. Constraint:
`uniq_active_subscription_per_tenant`, partial on `status='active' AND NOT is_deleted`.

Helpers: `rules` (snapshot-first with live-plan fallback), `included_entitlement(resource)`,
`cycle_delta()`, `derive_end_date()`, `is_current`, `has_scheduled_change`, `is_expired`.

`end_date` is still `null=True` at the database level — the column is populated everywhere
and `save()` guarantees new rows get one, but a hard `NOT NULL` is deferred until the three
signup paths go through `activate()` (R2), so a half-migrated deploy cannot fail inserts.

> **Resolved.** All 35 subscriptions in the dev database had `end_date = NULL` — G1 was
> real, and migration `0025` backfilled every one from `start_date + billing_cycle`.
>
> No tenant actually held two ACTIVE subscriptions. An earlier note here claimed two did;
> that came from grouping by tenant *name* rather than id, and "Test" and "Akky" are
> simply names shared by several distinct tenant rows. The constraint applied cleanly,
> which is the proof. G5 remains worth having — an upgrade creates a second row, so the
> risk is structural — but it was not already violated.

## 1.5 `TenantEntitlement` ✅ — `onboard_tenant_entitlement`

Extra capacity bought beyond the plan. One row per (tenant, resource).

| Field | Type | Description |
| :--- | :--- | :--- |
| `tenant` | FK → Tenant | Owner |
| `resource` | char | Same enum as `PlanEntitlement.resource` |
| `additional_quantity` | int | Units bought beyond included. 0 is a valid resting state |
| `price_per_unit` | decimal | **Frozen at purchase.** Read-only in admin |
| `addon_step` | int | Block size, frozen alongside the price |
| `subscription` | FK → Subscription | Which cycle bought it. Context only — extras outlive it |
| `purchased_at` | datetime | When the quantity last changed |
| `notes` | text | Support grants, negotiated deals |

Helper: `recurring_charge` → pre-tax ₹ this row adds to every cycle.

**Effective limit** = `plan_snapshot.included` + `additional_quantity`.

## 1.6 `Transaction` ✅ — `onboard_transaction`

One payment attempt. One Razorpay order.

| Field | Type | Status | Description |
| :--- | :--- | :--- | :--- |
| `razorpay_order_id` | char, unique | ✅ | `order_xxx`, or `free_xxx` for zero-cost |
| `razorpay_payment_id` | char | ✅ | Set on capture |
| `amount` | decimal | ✅ | ₹ charged, tax inclusive |
| `status` | char | ✅ | `pending` / `successful` / `failed` / `canceled` |
| `temp_user` | FK → TempUser | ✅ | Signup only, before a User exists |
| `user` | FK → CustomUser | ✅ | Set once onboarding completes |
| `plan` | FK → Plan | ✅ | The plan involved |
| `raw_webhook_payload` | json | ✅ | Debugging |
| `transaction_type` | char | ✅ | `onboarding` / `renewal` / `upgrade` / `top_up` / `addon` |
| `tenant` | FK → Tenant | ✅ | Null only for onboarding, enforced by check constraint |
| `subscription` | FK → Subscription | ✅ | Which cycle this pays for |
| `target_plan` | FK → Plan | ✅ | The plan being moved **to** |
| `credits_purchased` | int | ✅ | Top-ups only |
| `amount_breakdown` | json | ✅ | Proration inputs, for disputes |
| `expires_at` | datetime | ✅ | Pending-order TTL |

## 1.7 `Wallet` ✅ — `wallet_wallet`

One per tenant. The balance, and the stamped rate card.

| Field | Type | Description |
| :--- | :--- | :--- |
| `tenant` | FK → Tenant | Unique per live tenant |
| `balance` | int | Total spendable. **Always** `allocated + purchased` (check constraint) |
| `allocated_balance` | int | Granted by a plan cycle. Rollover rules govern this bucket |
| `purchased_balance` | int | Bought with cash. Always survives a plan change |
| `lifetime_granted` / `lifetime_consumed` | int | Monotonic, analytics only |
| `is_frozen` | bool | Blocks **spending**, not granting |
| `frozen_reason` / `frozen_at` | char / datetime | Why and when |
| `low_balance_threshold` | int | Copied from the plan |
| `low_balance_notified_at` | datetime | Dedupes the daily alert |
| `current_plan` | FK → Plan | Whose rate card is stamped. Display only |
| `rate_snapshot` | json | **The authority for rating every call** |
| `rate_snapshot_at` | datetime | When it was stamped |

`rate_snapshot` shape:

```jsonc
{ "plan_id": "…", "plan_code": "starter_inr", "stamped_at": "…",
  "operations": {
    "static_reel":               { "unit": "per_minute", "credits_per_unit": 2,  "rounding": "ceil", "minimum_units": 1, "rate_id": "…" },
    "conversation_high_latency": { "unit": "per_minute", "credits_per_unit": 5,  … },
    "conversation_low_latency":  { "unit": "per_minute", "credits_per_unit": 10, … } } }
```

## 1.8 `CreditLedger` ✅ — `wallet_credit_ledger`

Append-only. `SUM(amount)` per wallet **is** the balance.

| Field | Type | Description |
| :--- | :--- | :--- |
| `wallet` | FK → Wallet | PROTECT |
| `entry_type` | char | 10 types — see below |
| `bucket` | char | `allocated` / `purchased` |
| `amount` | int, signed | + adds, − removes. **Never zero** |
| `balance_after` | int | Wallet total immediately after this row |
| `operation` | char, null | Usage rows: which tier |
| `raw_quantity` | int, null | e.g. 90 (seconds) — before rounding |
| `units` | int, null | e.g. 2 (minutes) — after rounding |
| `unit_type` | char, null | e.g. `per_minute` |
| `rate_applied` | int, null | Credits per unit at the time |
| `rate` | FK → CreditRate | Convenience link to the catalogue row |
| `reference_id` | char, null | **The idempotency key.** Unique per entry type |
| `subscription` | FK → Subscription | Audit context. Never read to decide anything |
| `performed_by` | FK → User | Staff behind an admin adjustment |
| `notes` / `metadata` | text / json | Explanation and structured context |

**Entry types:**

| In | Out |
| :--- | :--- |
| `opening_balance` | `usage_deduction` |
| `cycle_allocation` | `usage_adjustment` |
| `manual_top_up` | `rollover_forfeit` |
| `usage_refund` | `rollover_cap_forfeit` |
| `admin_adjustment` (either sign) | `expiry_forfeit` |

**Reference IDs:**

| Entry | `reference_id` |
| :--- | :--- |
| `usage_deduction` | `AgentCallLog.provider_call_id` |
| `usage_adjustment` | `{provider_call_id}:adj:{n}` |
| `cycle_allocation`, `manual_top_up` | Razorpay order id |
| forfeits | Source `Subscription.id` |
| `admin_adjustment` | Support ticket ref |

Immutability is enforced: instance `save`/`delete`/`soft_delete` raise, and the queryset
blocks bulk `delete`/`soft_delete`/`update`.

## 1.9 Supporting models

| Model | Status | Role |
| :--- | :--- | :--- |
| `Tenant` ✅ | — | The organisation. Everything is scoped to it |
| `TenantUserMapping` ✅ | — | User ↔ tenant. Counted against the `user` entitlement |
| `TenantSettings.credits_enforced` ✅ | — | **False** = meter but never block |
| `TempUser` ✅ | — | Pre-account signup state; soft-deleted on completion |
| `BillingAddress` ✅ | — | GSTIN and address, captured before the order |
| `RazorpayWebhookLog` ✅ | — | Every inbound event. `event_id` is now `unique=True` — the outer idempotency guard |
| `AgentCallLog.service_tier` ✅ | — | Which tier actually ran. **This is what gets billed** |
| `Agent.service_tier` ✅ | — | The bot's configured tier. The fallback |

---

# Part 2 — The lifecycle, record by record

One tenant, **Acme**, followed from signup to expiry. Every number is real arithmetic
against the seeded grid.

## Reference: the seeded catalogue

| Plan | Net ₹ | Total ₹ | Credits | static / high / low |
| :--- | ---: | ---: | ---: | :--- |
| `free_plan` | 0 | 0 | 100 | 2 / 5 / 10 |
| `starter_inr` | 999.00 | 1,178.82 | 1,000 | 2 / 5 / 10 |
| `professional_inr` | 2,699.10 | 3,184.94 | 3,500 | 1 / 3 / 6 |

Starter entitlements: 3 agents, 10 GB storage (+₹199 per 10 GB block, max +90).

---

## Step 0 — Setup (admin, once) ✅

| Model | Records created |
| :--- | :--- |
| `Plan` | 5 rows, via `load_data plan` *(historical — plan seeds have since been removed; plans are created only from the super admin panel)* |
| `PlanEntitlement` | 7 rows **per plan** = 35 |
| `CreditRate` | 3 rows **per plan** = 15, all `effective_to = NULL` |

Nothing tenant-facing exists yet. No wallet, no subscription.

---

## Step 1 — Acme signs up on the free trial ✅

Zero-cost, so Razorpay is skipped entirely.

| # | Model | Record |
| :--- | :--- | :--- |
| 1 | `TempUser` | email, hashed password, `selected_plan = free_plan`, OTP verified |
| 2 | `BillingAddress` | address + GSTIN, attached to the TempUser |
| 3 | `Transaction` | `razorpay_order_id = free_a1b2c3…`, `amount = 0.00`, `status = successful`, `transaction_type = onboarding` |
| 4 | `CustomUser` | Promoted from TempUser, `is_active`, `is_verified` |
| 5 | `Tenant` | `name = Acme`, `subdomain = acme` |
| 6 | `TenantUserMapping` | Links the two |
| 7 | `Subscription` | `plan = free_plan`, `status = active`, `start = 1 Mar`, **`end = 1 Apr`**, `plan_snapshot` frozen |
| 8 | `Wallet` | Created, `balance = 0` |
| 9 | `Wallet` | **Stamped**: `current_plan = free_plan`, `rate_snapshot` = 2/5/10 |
| 10 | `CreditLedger` | `cycle_allocation +100`, bucket `allocated`, `balance_after = 100`, ref = `free_a1b2c3…` |
| 11 | `TempUser` | `is_onboarded = True`, soft-deleted |

**Order matters at 9→10.** Stamping before granting means a call between the two is rated
at the right plan's prices.

**State after:**

```
Subscription  free_plan · active · 1 Mar → 1 Apr
Wallet        balance 100 (allocated 100 / purchased 0) · not frozen · rates 2/5/10
Ledger        1 row  · +100
```

---

## Step 2 — Acme runs a call ✅

A 3-minute call on the low-latency tier.

| # | Model | Record |
| :--- | :--- | :--- |
| 1 | *(pre-flight)* | `assert_can_consume` reads the **Wallet row only**. Not frozen, balance > 0 → proceed |
| 2 | `AgentCallLog` | `provider_call_id = call_9fA…`, `duration_seconds = 180`, `service_tier = conversation_low_latency` |
| 3 | *(rating)* | From `rate_snapshot`: 10 cr/min, ceil, min 1 → `ceil(180/60) = 3` units → **30 credits** |
| 4 | `CreditLedger` | `usage_deduction −30`, `raw_quantity 180`, `units 3`, `unit_type per_minute`, `rate_applied 10`, ref = `call_9fA…`, `balance_after = 70` |
| 5 | `Wallet` | `allocated 70`, `balance 70`, `lifetime_consumed 30` |

**If the runner re-POSTs the same call:** the `(usage_deduction, call_9fA…)` unique
constraint blocks it. `consume()` returns `None`, nothing is written, balance stays 70.

**While `credits_enforced = False`:** step 1 logs *would-refuse* and proceeds anyway. Step 4
still writes a real ledger row. That is the metering-before-blocking rule.

---

## Step 3 — Acme upgrades to Starter on day 10 ✅

### 3a. Preview (no side effects)

```
current plan net       ₹0.00          (free)
unused ratio           20/30 = 0.6667
unused value           ₹0.00          ← nothing to carry over from a free plan
new plan net           ₹999.00        ← charged in full, always
bonus days             0.00           ← nothing to convert
tax @ 18%              ₹179.82
amount due             ₹1,178.82
```

> **Coming from a free plan is the one case where nothing carries.** From a paid plan,
> the unused value is converted at the new plan's daily rate into **extra days** — the
> price charged is still the plan's full list price, and the cycle simply runs longer.
> Had Acme been on a ₹499 plan with 20 of 30 days left, ₹332.67 of unused value would
> have bought 10 extra days of Starter, and the amount due would still be ₹1,178.82.
> See [How Money Moves §3](../apiDocs/billing/money-and-payments.md#3-plan-changes).

Credit impact: free plan has `allow_rollover_on_upgrade = True` → **70 credits carry**.
New balance will be `70 + 1000 = 1070`.

### 3b. Order

| Model | Record |
| :--- | :--- |
| `Transaction` | `transaction_type = upgrade`, `target_plan = starter_inr`, `amount = 1178.82`, `status = pending`, `amount_breakdown` = the table above, `expires_at = now+30m` |
| Razorpay | `order_xxx` returned to the frontend |

### 3c. Payment captured → webhook

| # | Model | Record |
| :--- | :--- | :--- |
| 1 | `RazorpayWebhookLog` | `event_id` unique — a redelivery returns 200 and stops here |
| 2 | `Transaction` | `status = successful`, `razorpay_payment_id` set |
| 3 | `Wallet` | **Re-stamped**: `current_plan = starter_inr`, rates now 2/5/10 (unchanged here; from Pro they would change) |
| 4 | `CreditLedger` | Rollover allowed and under cap → **no forfeit row** |
| 5 | `Subscription` (old) | `status = cancelled`, `canceled_at = now`, `superseded_by` → new |
| 6 | `Subscription` (new) | `plan = starter_inr`, `active`, `10 Mar → 10 Apr`, fresh `plan_snapshot` |
| 7 | `CreditLedger` | `cycle_allocation +1000`, ref = the upgrade order id, `balance_after = 1070` |
| 8 | `Wallet` | `allocated 1070`, `balance 1070` |

**The wallet balance never moved between rows.** Old sub closed, new sub opened, the 70
credits simply stayed where they were. That is what anchoring the wallet to the tenant buys.

**State after:**

```
Subscription  starter_inr · active · 10 Mar → 10 Apr   (old one: cancelled)
Wallet        balance 1070 (allocated 1070 / purchased 0) · rates 2/5/10
Ledger        3 rows · +100, −30, +1000
```

> **Direct paid purchase** (no trial) is Step 1 with two differences: `amount = ₹1,178.82`
> so a real Razorpay order is created, and the flow completes on webhook or on
> `verify_payment` — whichever lands first, the other becomes a no-op.

---

## Step 4 — Acme tops up 500 credits ✅

Starter's `cost_per_additional_credit = ₹1.20`.

```
500 × ₹1.20 = ₹600.00  +18% = ₹708.00
```

| # | Model | Record |
| :--- | :--- | :--- |
| 1 | `Transaction` | `transaction_type = top_up`, `credits_purchased = 500`, `amount = 708.00` |
| 2 | `CreditLedger` | `manual_top_up +500`, bucket **`purchased`**, ref = order id, `balance_after = 1570` |
| 3 | `Wallet` | `allocated 1070`, **`purchased 500`**, `balance 1570` |

From here every call **drains `allocated` first**. The 500 they paid cash for is what
survives the cycle boundary.

---

## Step 5 — Acme buys 20 GB extra storage on day 18 ✅

Starter includes 10 GB. `PlanEntitlement`: `+₹199 per 10 GB block, max_additional = 90`.

```
20 GB → ceil(20/10) = 2 blocks → ₹398.00 per cycle
prorated for 22 of 30 days remaining:  ₹398.00 × 0.7333 = ₹291.87
                                +18% =  ₹344.41  ← charged now
```

| # | Model | Record |
| :--- | :--- | :--- |
| 1 | `Transaction` | `transaction_type = addon`, `amount = 344.41`, breakdown records the proration |
| 2 | `TenantEntitlement` | `resource = storage_gb`, `additional_quantity = 20`, **`price_per_unit = 199.00` frozen**, `addon_step = 10`, `purchased_at = now` |
| 3 | *(no wallet change)* | Entitlements are gauges. **No ledger row, no balance change, ever** |

Effective storage limit is now `10 (included) + 20 (extra) = 30 GB`.
Their recurring charge from next cycle becomes `₹999 + ₹398 = ₹1,397` net.

> If the catalogue price later rises to ₹249, Acme keeps ₹199 — the frozen
> `price_per_unit` is what recurs. Buying *more* re-prices the whole block to current.

---

## Step 6 — Renewal ✅

### 6a. Reminders

`send_renewal_reminders` fires at T−7, T−3, T−1 → `InAppNotification` + push. No model
changes to billing.

### 6b. Acme clicks "Renew"

```
plan net                ₹999.00
add-ons                 ₹398.00     ← TenantEntitlement.recurring_charge
net total             ₹1,397.00
tax @ 18%               ₹251.46
amount due            ₹1,648.46
```

| Model | Record |
| :--- | :--- |
| `Transaction` | `transaction_type = renewal`, `subscription` = current, `amount = 1648.46` |

### 6c. Payment captured

Say Acme has **400 allocated + 500 purchased = 900** left. Starter has
`allow_rollover_on_renewal = False`.

| # | Model | Record |
| :--- | :--- | :--- |
| 1 | `Subscription` | `end_date` extended to `10 May`. Same row — a renewal is not a new subscription |
| 2 | `CreditLedger` | `rollover_forfeit −400`, bucket `allocated`, ref = subscription id, `balance_after = 500` |
| 3 | `CreditLedger` | `cycle_allocation +1000`, bucket `allocated`, ref = renewal order id, `balance_after = 1500` |
| 4 | `Wallet` | `allocated 1000`, `purchased 500`, `balance 1500` |
| 5 | `TenantEntitlement` | Unchanged — extras carry, and were re-charged in 6b |

**The two buckets behave differently, and this is the step where it shows.** The 400 plan
credits burned because Starter does not roll over. The 500 purchased credits survived
untouched, because cash-bought credits always do.

> On Professional (`allow_rollover_on_renewal = True`, cap 3,500) step 2 would not happen;
> the balance would carry, clamped at the cap, with any excess written as
> `rollover_cap_forfeit`.

---

## Step 7 — If Acme does **not** renew ✅

`process_billing_cycle` runs hourly. Per subscription, **in this order**:

1. Is there a `scheduled_change_at <= now`? → apply the plan change
2. Otherwise, is `end_date < now`? → expire

The order matters: two independent tasks would race, and a tenant who scheduled a downgrade
would get expired instead.

| # | Model | Record |
| :--- | :--- | :--- |
| 1 | `Subscription` | `status = expired` |
| 2 | `Wallet` | `is_frozen = True`, `frozen_reason = subscription_expired`, `frozen_at = now` |
| 3 | `CreditLedger` | `expiry_forfeit` for the **allocated bucket only** — plan credits end with the plan |
| 3b | `Wallet` | **Purchased credits are kept, not confiscated** — frozen, and spendable again the moment a renewal clears |
| 4 | `TenantEntitlement` | Rows remain. Extras are re-charged if they come back |

**Access stops** — `assert_can_consume` refuses on a frozen wallet.

> **Corrected during build.** An earlier draft of this step forfeited the *whole* balance.
> That would take cash a tenant paid for a top-up and give nothing back, which is
> indefensible — so expiry now burns only the allocated bucket and freezes the rest.
> Purchased credits survive, unspendable while lapsed and spendable again on renewal.
>
> On a plan with `allow_use_after_expiry = True`, steps 2 and 3 do not happen at all. The
> subscription expires but the wallet stays live and the whole balance stays spendable.
> That single flag is what pay-as-you-go is built from.

**Paying a renewal after expiry** unfreezes the wallet and extends the cycle — but the
forfeited credits are gone. `grant()` deliberately works on a frozen wallet so the payment
can land before the unfreeze.

---

## Step 8 — Downgrade (for contrast) ✅

Acme moves Starter → Free on day 12. Proration is negative, so:

| # | Model | Record |
| :--- | :--- | :--- |
| 1 | `Subscription` | `scheduled_plan = free_plan`, `scheduled_change_at = 10 Apr`. **Nothing else happens** |
| 2 | — | No `Transaction`, no order, no refund. They keep Starter until the cycle ends |
| 3 | *(at cycle end)* | `process_billing_cycle` applies it: re-stamp, forfeit per rules, new subscription |
| 4 | `TenantEntitlement` | Free includes 1 GB and sells no add-ons → the 20 GB extra is **removed**, and storage over the limit needs a cleanup policy ⚠️ |

Only ever one ACTIVE subscription exists, so the R5 constraint is never approached — the
downgrade is *stored*, not applied.

> ⚠️ **This holds only while the time left is worth more than the cheaper plan.** Late in
> a cycle it is not, and the move becomes a paid change charged at the **cheaper plan's
> full price**, with the unused value converted to bonus days. A day-20 downgrade from a
> ₹30,000 plan to a ₹12,000 one charges ₹14,160 and runs a 55-day cycle. Value-neutral,
> but the cash outlay is six times what the old discount model asked for — on a move the
> customer thinks of as spending less. It is the most surprising case in the system;
> see [How Money Moves §3](../apiDocs/billing/money-and-payments.md#3-plan-changes).

---

## Step 9 — Acme comes back, three weeks after lapsing ✅

Step 7 left Acme expired: subscription lapsed, allocated credits forfeited, wallet
frozen, and 500 purchased credits sitting untouched. This is what happens next.

**Before this existed, coming back on a bigger plan meant renewing Starter *and then*
upgrading — two payments for one outcome — and `GET /billing/subscription/` returned a
409, so the dashboard could not even say why they were blocked.**

### 9a. They can still see everything

| Endpoint | Answers | Shows |
| :--- | :--- | :--- |
| `GET /billing/subscription/` | 200 | `status: expired`, the plan that lapsed, `days_remaining: 0` |
| `GET /billing/wallet/` | 200 | Balance 500, `is_frozen: true` — the credits are visibly still theirs |
| `GET /billing/subscription/eligible-plans/` | 200 | Every plan, at **full price** |
| `POST /billing/top-up/`, `/add-ons/` | **409** | Both belong to a cycle, and there is not one running |

`current_or_expired()` is what makes this possible: the live subscription, or the most
recent non-cancelled one. Cancelled rows are excluded — those were superseded by a plan
change, so the row that replaced them is the one that matters.

### 9b. Quoting a lapsed subscription

Once a cycle is over there is no unused time to credit and no cycle to schedule against,
so *upgrade* and *downgrade* stop being meaningful. Every option is a full-price purchase.

| | Mid-cycle (Step 3) | Lapsed (here) |
| :--- | :--- | :--- |
| `credit_from_current` | Prorated | **0.00** |
| A cheaper plan | `scheduled_change`, ₹0 | `paid_change`, full price |
| The free plan | `scheduled_change` at `end_date` | `immediate_free`, applied now |
| `effective_at` | Now, or `end_date` | **Now** — never a date in the past |
| `money.reactivation` | `"False"` | `"True"` |

A subscription past its `end_date` counts as lapsed **even if the hourly sweep has not
reached it yet**. The date decides, not the status — otherwise a tenant would be
credited for time they no longer have.

### 9c. Acme pays for Professional

| # | Model | Record |
| :--- | :--- | :--- |
| 1 | `Transaction` | `transaction_type = upgrade`, `target_plan = professional_inr`, amount = Professional's **full** `total_price` |
| 2 | `Subscription` *(old)* | `status = cancelled`, `canceled_at = now`, `superseded_by` → the new row |
| 3 | `Subscription` *(new)* | `status = active`, fresh cycle, snapshot re-frozen from the live plan |
| 4 | `Wallet` | Rate card re-stamped to Professional — the next call prices at 3 credits/min, not 5 |
| 5 | `CreditLedger` | `cycle_allocation +3500`, keyed on the order id |
| 6 | `Wallet` | Unfrozen |
| 7 | `TenantEntitlement` | Rows survived the lapse and are re-charged from the next renewal |

**The 500 purchased credits are still there**, through the expiry, the freeze, the plan
change and the fresh allocation. Balance: 4000.

### 9d. If Acme wanted Starter again

`renew/`, not `change-plan/`. It **extends the existing subscription row** rather than
opening a second one, so a customer who came and went twice has a history someone can
read. `change-plan/` to the plan you are already on returns `409 PLAN_UNCHANGED` with a
message pointing at `renew/`.

---

# Part 3 — Cross-cutting rules

## Who may write what

```
onboard (subscriptions) ──▶ set_plan_context() · grant() · forfeit() · freeze()/unfreeze()
core (calls, campaigns) ──▶ consume() · assert_can_consume()
                                       │
                                       ▼
                        wallet/services/wallet.py   ← the ONLY writer
                                       │
                             Wallet + CreditLedger
```

Nothing else touches a balance or inserts a ledger row.

## Credits vs entitlements

| | Credits | Entitlements |
| :--- | :--- | :--- |
| Nature | Flow — spent and gone | Gauge — held, then released |
| Measured | `Wallet.balance` | Live count vs limit |
| Priced in | Credits | **Rupees**, recurring |
| Bought via | Top-up | Add-on purchase |
| Model | `CreditRate` → `CreditLedger` | `PlanEntitlement` → `TenantEntitlement` |
| Touches the wallet | Always | **Never** |
| Everything in it | The 3 call tiers | agents, users, contacts, KBs, crawl pages, campaigns/month, storage |
| The test | Hang up a call — minutes do not return | Delete a file — the GB returns |

### Storage is the case that proves the rule

Storage was briefly modelled as a credit — `credits per GB-month`, deducted monthly by a
job. That was dropped, and `CreditRate.Operation` is now **closed to the three call tiers**
so it cannot drift back.

The delete-test is why. Charge 20 credits per GB-month and deleting a file becomes a refund
question with no good answer:

- **Re-credit the unused days** → a tenant can churn uploads to arbitrage the meter.
- **Do not re-credit** → they paid for storage they no longer hold.

As a recurring rupee entitlement neither question arises: deleting frees the allowance, and
next cycle simply costs less. Nothing to reconcile, nothing to refund, no ledger row ever
written for a gauge.

The same reasoning retires the old "metered overage" idea for storage-beyond-quota — see
the wallet document's §10.3.

**If a genuinely consumable resource is ever added** — outbound SMS is the obvious
candidate — it is a new `Operation` member and a seeded rate row per plan, not a redesign.
SMS is absent today only because it is not sold.

## Three layers of frozen history

| Layer | Frozen when | Answers |
| :--- | :--- | :--- |
| `Subscription.plan_snapshot` | Subscription starts | What terms did they buy? |
| `Wallet.rate_snapshot` | Plan is activated or changed | What does a minute cost them now? |
| `CreditLedger` rating fields | Every deduction | How was *this* charge computed? |
| `TenantEntitlement.price_per_unit` | Add-on purchased | What do their extras recur at? |

Editing a plan therefore changes terms for **future** cycles only.

## Idempotency, end to end

| Layer | Guard |
| :--- | :--- |
| Webhook | `RazorpayWebhookLog.event_id` unique → redelivery returns 200, no processing |
| Ledger | `(entry_type, reference_id)` unique → replay writes nothing |
| Calls | `provider_call_id` already unique → a re-POSTed call deducts once |
| Orders | One PENDING billing transaction per tenant → no double upgrade |

## Money vs credits — which endpoint charges what

| Action | Charged in | Creates a Transaction? | Ledger row? |
| :--- | :--- | :--- | :--- |
| Subscribe / renew | ₹ | Yes | `cycle_allocation` |
| Upgrade | ₹ prorated | Yes | forfeit (maybe) + `cycle_allocation` |
| Downgrade | — | No | At cycle end only |
| Buy credits | ₹ | Yes | `manual_top_up` |
| Buy an add-on | ₹ prorated, recurring | Yes | **No** |
| Make a call | Credits | No | `usage_deduction` |

---

# Part 4 — What is left

Nothing in this document is unbuilt. What remains is operational, or a product decision.

## Decisions waiting on you

| # | Decision | Why it matters |
| :--- | :--- | :--- |
| 1 | **Run `backfill_wallet_stamps`** | Every subscription created before `activate()` existed has no stamped rate card, so *every call by that tenant raises `RateNotConfigured`*. The stamping half must run before enforcement. Granting the allocations those tenants never received is separate, and is a business call — hence `--grant-allocations` |
| 2 | **Run `audit_billing_readiness --enable-safe`** | Flips `credits_enforced` and `entitlements_enforced` for tenants with no findings. Until then everything meters and nothing blocks |
| 3 | **Set `RAZORPAY_WEBHOOK_SECRET` locally** | Four of the twelve live-gateway tests are the *only* coverage of signature verification anywhere — every other webhook test runs unsigned. They skip without it |
| 4 | **`reconcile_wallets --fix` exits non-zero after a successful repair** | Defensible (drift *was* found), but it fails any automation that calls it |

## Open product questions

| # | Question | Current behaviour |
| :--- | :--- | :--- |
| 1 | **Downgrade with extras** (Step 8, row 4) — 20 GB stored, moving to a plan that sells no storage add-ons | The `TenantEntitlement` is removed and the data is over the limit. **No grace period, no cleanup policy.** Still the sharpest gap in the design |
| 2 | **`campaign_monthly` is not sellable** — `is_addon_available: false` on every plan | Deliberate, and currently correct: it is a *count in a window*, not a rate held over time, so `quote_addon`'s proration would charge a third of the price for the full count. Selling it needs flat pricing like a top-up, not proration |
| 3 | **A yearly plan grants the whole year on day one** — one `cycle_allocation` of 12,000, next one 365 days later | Consistent, but `starter_inr_yearly` has `allow_rollover_on_renewal: false`, so an unspent year burns at the anniversary — twelve times the goodwill cost of the monthly equivalent. Its `low_balance_threshold` of 10% also fires about five weeks out, a ratio carried over from the monthly plans |
| 4 | **`consume()` does not check `is_frozen`; only `assert_can_consume()` does** | A frozen tenant is blocked from *starting* a campaign, but a call log that arrives anyway is still charged. Arguably right — the call happened — but "unspendable while lapsed" holds at authorisation, not at settlement |
| 5 | **`renews_without_payment` and `Agent.service_tier` are admin-only** | Neither is on any serializer. The first is what makes a zero-cost cycle roll over instead of expiring; the second decides what a bot's minutes cost. Both are set in the admin, and a tenant cannot choose either |

## Not automatable, by nature

- **Completing a real payment.** Razorpay Checkout needs a browser. The live suite
  creates real test-mode orders and never pays them; the rest is manual QA.
- **Delivery to a device.** FCM reports that Firebase *accepted* a message, never that a
  phone showed it. A `SENT` row means the former, and no test can assert the latter.
- **A second currency.** `Plan.CurrencyChoices` has one member. Adding another means
  per-currency plans, a currency on the wallet or a conversion at the boundary, and a
  decision about what a credit is worth in each — none of which is designed.

## Resolved since this document was written

| Was | Now |
| :--- | :--- |
| "Entitlement enforcement is not built — a tenant can exceed their caps today" | Built. `onboard/services/entitlements.py` plus `EntitlementGuardMixin`; over-limit creates return **402 `QUOTA_EXCEEDED`** once `entitlements_enforced` is on |
| "Add-ons change the recurring amount, so renewal and proration must both include it — that is new scope" | Done. Renewal orders are plan price **+** `recurring_total()`, and proration credits the unused part of what was already paid for capacity |
| "Seed the catalogue" as a build phase | Removed. There is no hard-coded catalogue — **whatever an admin configures is final**, and `wallet/checks.py` was deleted with it |

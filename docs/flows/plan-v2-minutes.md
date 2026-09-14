# Minutes-Driven Plans — What Changed, End to End

The customer now says **how many minutes they need**. Which plan they land on, what they
pay and how many credits they receive are all derived from that one number.

This walks the whole path in order — admin configuration first, then signup, then
everything after it — and for each step shows **what it was**, **what it is now**, and
the exact request and response. Written for the team and for the frontend: if a screen
touches plans, pricing or credits, its change is in here.

**Reference:** [feature detail](../feature-detail-scratch/plan-v2-feature.md) ·
[implementation plan](../implementation-plan-scratch/plan-v2-implementation-plan.md) ·
[release note](../release-note/1.0.2.md)

---

## Contents

1. [The one-paragraph summary](#1-the-one-paragraph-summary)
2. [The shape of a plan, before and after](#2-the-shape-of-a-plan-before-and-after)
3. [Step 1 — Configuring a plan in the admin console](#3-step-1--configuring-a-plan-in-the-admin-console)
4. [Step 2 — The pricing page](#4-step-2--the-pricing-page)
5. [Step 3 — Signup](#5-step-3--signup)
6. [Step 4 — Paying and activation](#6-step-4--paying-and-activation)
7. [Step 5 — Living with a subscription](#7-step-5--living-with-a-subscription)
8. [Step 6 — Changing plan or quantity](#8-step-6--changing-plan-or-quantity)
9. [Step 7 — Renewal, expiry and reactivation](#9-step-7--renewal-expiry-and-reactivation)
10. [Top-ups and add-ons](#10-top-ups-and-add-ons)
11. [Every API change, in one table](#11-every-api-change-in-one-table)
12. [Things that no longer work](#12-things-that-no-longer-work)
13. [Frontend checklist](#13-frontend-checklist)

---

## 1. The one-paragraph summary

A plan used to be a **fixed bundle**: `base_price` rupees for `allocated_credits`
credits. It is now a **band of minutes** with a per-minute price. The customer enters
the minutes they need, the server picks the one plan whose band contains that number,
and both the price and the credit grant are computed from it. Nothing about how a
*call* is charged has changed.

```
BEFORE   pick a plan  ->  pay its fixed price  ->  get its fixed credits

AFTER    say how many minutes you need
             |
             v
         server picks the plan whose band contains it
             |
             +--> price   = minutes x rupees-per-minute
             +--> credits = minutes x credits-per-minute
```

---

## 2. The shape of a plan, before and after

### `onboard.Plan`

| Field | Before | After |
|---|---|---|
| `base_price` | decimal — the price | **Removed.** Price is `minutes × price_per_minute_inr` |
| `allocated_credits` | int — credits per cycle | **Removed.** Grant is `minutes × credits_per_unit` |
| `min_minutes` | — | 🆕 int, nullable. Fewest minutes sold. Null = no floor |
| `max_minutes` | — | 🆕 int, nullable. Most minutes sold. Null = no ceiling |
| `credit_basis` | — | 🆕 choice. Which call tier the plan is **sold at** |
| `low_balance_threshold` | int | Kept, but **superseded by** `low_balance_percent` |
| `low_balance_percent` | — | 🆕 int, nullable. Alert at this % of the cycle's grant |
| `max_rollover_credits` | int, nullable | Kept, but **superseded by** `max_rollover_ratio` |
| `max_rollover_ratio` | — | 🆕 decimal. Cap as a multiple of the cycle's grant |
| `discount_percentage`, `tax_percentage`, `currency`, `billing_cycle`, `plan_type`, `sort_order`, `is_most_popular`, `status`, rollover flags, `allow_use_after_expiry`, `renews_without_payment`, `cost_per_additional_credit` | | **All unchanged** |

### `wallet.CreditRate` — one row per (plan, tier)

| Field | Before | After |
|---|---|---|
| `credits_per_unit` | int — what a minute costs in credits | **Unchanged** |
| `price_per_minute_inr` | — | 🆕 decimal(10,4). What a minute of this tier costs in **rupees** |

> **Why the rupee price lives here and not on the plan.** What a minute of a tier costs
> in money and what it costs in credits are two views of one commercial decision. Split
> across two tables they can be changed independently — switching a plan's basis would
> double the credits it grants and leave the price untouched, with nothing anywhere to
> compare them against. On the same row they move together.
>
> It also means a **price change is versioned**: `CreditRate` is supersede-don't-edit,
> so re-pricing closes the old row and opens a new one. `base_price` never had that.

### `onboard.Subscription`

| Field | Before | After |
|---|---|---|
| `selected_minutes` | — | 🆕 int, nullable. The quantity this subscription bought |
| `allocated_credits` | — | 🆕 int. What this cycle granted — **moved here from `Plan`** |
| `scheduled_minutes` | — | 🆕 int, nullable. The quantity a pending downgrade moves to |
| `plan_snapshot` | JSON | Gains `selected_minutes`, `credit_basis`, `price_per_minute_inr`, `credits_per_minute`. The money keys now hold the price **for the quantity bought** |

### `onboard.TempUser`

| Field | Before | After |
|---|---|---|
| `selected_minutes` | — | 🆕 int, nullable. Chosen at signup, before a tenant exists |

---

## 3. Step 1 — Configuring a plan in the admin console

**Base path:** `/api/v1/ops/plans/` · Requires the `plan.change` console permission and
a `reason` on every write. Full reference: [ops/plans.md](../apiDocs/ops/plans.md).

### What changed

A plan is still three models — the plan row, 7 entitlement rows, 3 rate rows — created
in one payload and starting as a **draft**. Two fields moved and three appeared.

**`POST /api/v1/ops/plans/`** — the `plan` block:

```jsonc
{
  "reason": "New volume ladder for Q4",
  "plan": {
    "code": "growth",
    "name": "Growth",
    "description": "For teams running regular campaigns",

    // ── REMOVED ─────────────────────────────────────────────
    // "base_price": "2999.00",        <- gone
    // "allocated_credits": 3500,      <- gone

    // ── NEW ─────────────────────────────────────────────────
    "min_minutes": 501,                // null = no floor
    "max_minutes": 2000,               // null = no ceiling
    "credit_basis": "conversation_high_latency",
    "low_balance_percent": 10,         // optional; falls back to the absolute field
    "max_rollover_ratio": "1.00",      // optional; 1.00 = one cycle's worth

    // ── UNCHANGED ───────────────────────────────────────────
    "discount_percentage": "0.00",
    "tax_percentage": "18.00",
    "currency": "INR",
    "billing_cycle": "monthly",
    "plan_type": "paid",
    "sort_order": 11,
    "is_most_popular": true,
    "cost_per_additional_credit": "0.54",
    "low_balance_threshold": 800,
    "max_rollover_credits": null,
    "allow_rollover_on_renewal": true,
    "allow_rollover_on_upgrade": true,
    "allow_rollover_on_downgrade": false,
    "allow_use_after_expiry": false,
    "renews_without_payment": false
  },
  "entitlements": [ /* 7 rows, unchanged */ ],
  "rates": [ /* 3 rows, see below */ ]
}
```

**The `rates` block** — each row gains one field:

```jsonc
{
  "operation": "conversation_high_latency",
  "credits_per_unit": 4,
  "price_per_minute_inr": "1.8000",   // 🆕 required on the basis tier
  "unit": "per_minute",
  "rounding": "ceil",
  "minimum_units": 1,
  "notes": ""
}
```

`price_per_minute_inr` is **required on the tier named by `credit_basis`** — that row
supplies both the price and the grant. It is strongly recommended on the other two so
the pricing page can quote them; a missing one is a warning, not a refusal.

### A worked plan

| | reel | **standard** | realtime |
|---|---:|---:|---:|
| `credits_per_unit` | 2 | **4** | 8 |
| `price_per_minute_inr` | 0.90 | **1.80** | 3.60 |
| ₹ per credit | 0.45 | **0.45** | 0.45 |

Bold is the `credit_basis`. A customer buying 1,200 minutes pays `1200 × 1.80 = ₹2,160`
net and receives `1200 × 4 = 4,800` credits.

### Activation is stricter

`POST /api/v1/ops/plans/{id}/activate/` refuses more than it used to. **New blocking
codes:**

| Code | Meaning |
|---|---|
| `CREDIT_BASIS_UNSET` | No basis tier — nothing to price or grant from |
| `CREDIT_BASIS_RATE_MISSING` | The basis tier has no open rate row |
| `CREDIT_BASIS_NO_INR_PRICE` | The basis row has no rupees-per-minute price |
| `CREDIT_BASIS_PER_EVENT` | The basis row is per-call, which has no per-minute meaning |
| `FREE_MINUTES_PLAN_UNCAPPED` | Free per minute with no `max_minutes` — unbounded free credits |
| `RENEWS_FREE_BUT_PRICED` | A priced plan that renews itself would stop billing |
| `BAND_OVERLAPS` | Another active plan sells the same quantities — two prices for one request |
| `BAND_GAP` | A gap to the next **ranged** band; nobody sells that quantity |

**New warnings:** `RUPEES_PER_CREDIT_INCONSISTENT`, `RATE_MISSING_INR_PRICE`,
`LADDER_OPEN_BOTTOM_MISSING`, `LADDER_OPEN_TOP_MISSING`.

**Removed warning:** `PAID_PLAN_NO_CREDITS` — a plan that grants nothing is now refused
outright rather than noted.

### The readiness report gained a ladder

`GET /api/v1/ops/plans/{id}/readiness/` now includes a `ladder` block: every band in
this plan's billing cycle, the price either side of every boundary, and the ladder-level
warnings. A band is only correct relative to its neighbours, so the review screen should
render this rather than the plan alone.

```jsonc
"ladder": {
  "bands": [
    {"code": "starter", "min_minutes": null, "max_minutes": 500,
     "is_fixed_quantity": false, "is_this_plan": false},
    {"code": "growth",  "min_minutes": 501,  "max_minutes": 2000,
     "is_fixed_quantity": false, "is_this_plan": true}
  ],
  "boundaries": [
    {"from_plan": "starter", "to_plan": "growth",
     "at_minutes": 500,  "price_below": "1375.00",
     "next_minutes": 501, "price_above": "901.80",
     "cheaper_above": true}
  ],
  "warnings": []
}
```

> **`cheaper_above` is information, not a fault.** Every volume ladder is cheaper just
> past a boundary — that *is* the volume discount. Avoiding it would mean a ladder that
> never gets cheaper per minute. Show the four numbers and let commercial decide.

### Rules the console now enforces

- **Bands must not overlap**, and must not leave a gap between two *ranged* bands. A gap
  next to a fixed-quantity pack is expected and allowed.
- **A rate write cannot strand a live plan's basis tier.** `PUT /ops/plans/{id}/rates/`
  returns **`409 RATE_WOULD_STRAND_BASIS`** if it would blank the basis row's rupee price
  or make it per-event on an active plan. Drafts are exempt.
- **Cloning warns about the band**: `BAND_COLLIDES` (same cycle — the copy sells exactly
  what the source sells) or `BAND_NEEDS_CONVERSION` (cycle switched — prices and band
  were copied verbatim).

### Two ways to sell

| Shape | Band | Behaviour |
|---|---|---|
| **Ranged** | `min < max`, or either null | The customer's exact number is sold |
| **Pack** | `min == max` | One fixed quantity. A request below it is rounded **up** to the smallest pack that covers it |

A ladder can mix them: packs sit above a ranged ladder where no ranged band claims
territory. A pack that *overlaps* a ranged band is refused.

---

## 4. Step 2 — The pricing page

### 🆕 `GET /api/v1/onboard/plans/for-minutes/`

**The new entry point.** Public, no auth. The customer types a number; this returns the
one plan that sells it, priced, with the credit grant worked out.

**Query parameters**

| Name | Type | Required | Notes |
|---|---|---|---|
| `minutes` | int | **yes** | 1 to 1,000,000 |
| `billing_cycle` | string | no | `monthly` (default) or `yearly` |
| `currency` | string | no | `INR` by default |

**`200` response `data`**

```jsonc
{
  "plan": {
    "id": "…", "code": "growth", "name": "Growth",
    "description": "…", "billing_cycle": "monthly", "currency": "INR",
    "min_minutes": 501, "max_minutes": 2000,
    "is_fixed_quantity": false, "is_most_popular": true
  },
  "requested_minutes": 1200,
  "selected_minutes": 1200,
  "rounded_up": false,
  "money": {
    "basis": "conversation_high_latency",
    "price_per_minute_inr": "1.8000",
    "gross": "2160.00", "discount": "0.00", "net": "2160.00",
    "tax": "388.80", "total": "2548.80",
    "tax_percentage": "18.00", "currency": "INR"
  },
  "credits": {
    "per_minute": 4,
    "granted": 4800,
    "equivalent_minutes": {
      "static_reel": 2400,
      "conversation_high_latency": 1200,
      "conversation_low_latency": 600
    }
  },
  "entitlements": [ … ],
  "rates": [ … ]
}
```

**Three fields the frontend must not confuse:**

| Field | Meaning |
|---|---|
| `requested_minutes` | What the customer typed |
| `selected_minutes` | **What is actually sold.** Send *this* to signup and change-plan |
| `rounded_up` | True when a pack covers a smaller request — show "the 5,000-minute pack covers your 1,200" |

`equivalent_minutes` is what the grant is worth on **each** tier. The promise "1,200
minutes" is only true on the basis tier; showing all three is what keeps it honest.

**Error responses**

| Status | `errors` | When |
|---|---|---|
| `400` | `INVALID_MINUTES` | Missing, non-numeric, ≤ 0, or above 1,000,000 |
| `404` | `NO_PLAN_FOR_MINUTES` | Nothing covers it. `data` carries `below` / `above` — the nearest bands, so the page can say what *is* available |
| `409` | `PLAN_NOT_SELLABLE` | The ladder matched a plan the catalogue cannot price. A configuration fault, not a bad request |

### `GET /api/v1/onboard/plans/` — changed

Still public and unchanged in shape, with these additions per plan:

| Field | Type | Notes |
|---|---|---|
| `min_minutes`, `max_minutes` | int, null | 🆕 The band |
| `credit_basis` | string, null | 🆕 The tier it is sold at |
| `is_minutes_based` | boolean | 🆕 |
| `is_fixed_quantity` | boolean | 🆕 A pack rather than a range |
| `rates[].price_per_minute_inr` | string, null | 🆕 |
| `rates[].is_basis` | boolean | 🆕 Which row prices the sale |
| `allocated_credits` | | **❌ Removed** — a plan has no single grant any more |
| `base_price` | | **❌ Removed** — see below |
| `net_base_price`, `tax_amount`, `total_price`, `discount_amount` | string | **Meaning changed:** now the plan's **entry price** — what its smallest sellable quantity costs. Use them for a "from ₹X" label, never to charge |

Enough is on the list response to render a slider without a request per tick. Call
`for-minutes/` once, on commit.

---

## 5. Step 3 — Signup

Full reference: [onboard/signup_flow.md](../apiDocs/onboard/signup_flow.md).

### `POST /api/v1/onboard/register-pending/` — changed

| Field | Before | After |
|---|---|---|
| `minutes` | — | 🆕 int. **Required** when the chosen plan is sold by the minute |

Send the `selected_minutes` returned by `for-minutes/`, **not** the number the customer
typed — on a pack they differ.

```jsonc
{
  "plan_id": "…",
  "minutes": 1200,          // 🆕
  "first_name": "Jane", "last_name": "Doe",
  "business_name": "Doe Enterprises",
  "email": "jane@example.com",
  "password": "…", "confirm_password": "…"
}
```

**New `400` cases**

| Message | When |
|---|---|
| `"<Plan> is sold by the minute; tell us how many."` | `minutes` omitted on a minutes plan |
| `"<Plan> does not sell N minutes."` | Outside the plan's band |
| `"<Plan> is not sold by the minute."` | `minutes` sent for a plan that has no band |

The quantity is **refused, never silently corrected** — the client already got a valid
quantity from `for-minutes/`, so a mismatch means the two are out of step, and quietly
moving it would charge somebody for something they never saw.

### `POST /api/v1/onboard/verify-otp/` — changed response

`billing_breakdown` is now computed for the chosen quantity and gains three fields:

```jsonc
"billing_breakdown": {
  "base_price": "2160.00",       // gross for the quantity, not a catalogue price
  "discount_amount": "0.00",
  "net_base_price": "2160.00",
  "tax_percentage": "18.00",
  "tax_amount": "388.80",
  "total_price": "2548.80",
  "currency": "INR",
  "selected_minutes": 1200,          // 🆕 null on a plan with no band
  "price_per_minute_inr": "1.8000",  // 🆕 null likewise
  "allocated_credits": 4800          // 🆕
}
```

The same block, with the same three new fields, is returned by **`resume-session/`** and
**`resume-session-token/`**, which additionally return `selected_minutes` at the top
level of `data`.

### `PATCH /api/v1/onboard/update-plan/` — changed

| Field | Before | After |
|---|---|---|
| `minutes` | — | 🆕 int, optional. Omitted, the current quantity is kept and re-validated |

**The quantity is re-validated against the new plan.** A registrant on 1,200 minutes who
switches to a plan selling up to 500 is refused **here**, not at checkout:

```jsonc
// 400
{
  "message": "Starter does not sell 1,200 minutes.",
  "errors": ["MINUTES_OUT_OF_BAND"],
  "data": {"min_minutes": null, "max_minutes": 500}
}
```

Switching to a plan with no band clears the quantity to `null`.

### `POST /api/v1/onboard/create-order-just-in-time/` — changed

**Request unchanged** (phone number and billing address). What changed is inside:

- The amount is `quote_plan(plan, selected_minutes)`, not `plan.total_price`.
- The order's `amount_breakdown` **freezes** `selected_minutes`, `price_per_minute_inr`
  and `credits_per_minute`, so a rate change between the order and the payment cannot
  alter what the customer receives.
- New **`400 MINUTES_NOT_SELLABLE`** if the stored quantity no longer fits the plan.
- The zero-amount branch now runs the same promotion the webhook does, rather than its
  own copy. Side effect: a free signup's first transaction is finally linked to its
  tenant, and the registrant's timezone and language now survive.

---

## 6. Step 4 — Paying and activation

**No request or response change on either payment endpoint.** What changed is what
activation does with the order.

| | Before | After |
|---|---|---|
| Subscription | plan + dates | plus `selected_minutes` and `allocated_credits` |
| Grant | `plan.allocated_credits` | `minutes × credits_per_minute`, read back from the order's frozen breakdown |
| Wallet alert level | `plan.low_balance_threshold` | `low_balance_percent` of *this* cycle's grant, when set |

**The quantity is applied verbatim.** If the plan's band moves between the order and the
payment clearing, the customer still gets what they paid for. This is worth stating
because the opposite is the expensive bug: a tenant orders 2,500 minutes, commercial
narrows the band to 5,000–9,000, and a re-resolving implementation would hand them 5,000
minutes for a 2,500-minute payment.

Both `POST /onboard/verify/` (signup) and `POST /billing/verify-payment/` (everything
after) behave identically, and both still race the webhook safely.

---

## 7. Step 5 — Living with a subscription

### `GET /api/v1/billing/subscription/` — changed response

```jsonc
{
  "id": "…", "status": "active",
  "plan": {"id": "…", "code": "growth", "name": "Growth", "billing_cycle": "monthly"},
  "selected_minutes": 1200,       // 🆕 null on a subscription with no quantity
  "allocated_credits": 4800,      // 🆕 what THIS cycle granted
  "start_date": "…", "end_date": "…",
  "days_remaining": 18,
  "renewable_now": false,
  "scheduled_change": {
    "plan_id": "…", "plan_code": "growth",
    "selected_minutes": 600,      // 🆕 the quantity the change moves to
    "effective_at": "…"
  },
  "credits": { … },               // unchanged
  "entitlements": [ … ],          // unchanged
  "addons_recurring_net": "0.00"  // unchanged
}
```

Everything else on this endpoint is unchanged.

---

## 8. Step 6 — Changing plan or quantity

### `POST /api/v1/billing/subscription/change-plan/` — changed

| Field | Before | After |
|---|---|---|
| `plan_id` | uuid, required | **Unchanged** |
| `minutes` | — | 🆕 int, optional. Omitted, the current quantity is carried across and resolved into the new plan's band |

**Buying more minutes of the plan you are already on is now a real change.** It used to
be refused outright; it is an upgrade, and it is priced by the same sign test that
already decided upgrade from downgrade.

| Request | Outcome |
|---|---|
| Different plan, no `minutes` | Current quantity, resolved into the new band. A pack always resolves to its one value |
| Different plan, with `minutes` | Validated against the new plan's band |
| **Same plan, different `minutes`** | 🆕 A real change — upgrade if it costs more, scheduled if less |
| Same plan, same `minutes` | `409 PLAN_UNCHANGED` — the only case still refused |

**Responses.** An upgrade returns an order, exactly as before. A downgrade and a
sub-rupee change now also report the quantity:

```jsonc
// scheduled downgrade
{
  "change_type": "scheduled_change",
  "plan_id": "…", "plan_code": "growth",
  "selected_minutes": 600,        // 🆕
  "effective_at": "…",
  "charged": false
}
```

> **A downgrade in quantity moves no plan**, so `scheduled_plan` alone could not express
> it — it would hold the plan the tenant is already on and the change would fire having
> done nothing. `Subscription.scheduled_minutes` is what carries it, and
> `DELETE /billing/subscription/scheduled-change/` clears both.

### `POST /api/v1/billing/subscription/preview-change/` — changed

Same new optional `minutes`. Zero side effects, as before.

- `money` — the quote breakdown, now including `selected_minutes` and `allocated_credits`
- `credits` — gains `selected_minutes`, and `new_plan_allocation` is now the grant **for
  the quantity being previewed** rather than a fixed plan number

### `GET /api/v1/billing/subscription/eligible-plans/` — changed

**New optional query parameter `?minutes=N`** — price every candidate at a quantity the
tenant is considering, rather than at the one they currently hold.

Each entry gains:

| Field | Type | Notes |
|---|---|---|
| `min_minutes`, `max_minutes` | int, null | 🆕 |
| `is_fixed_quantity` | boolean | 🆕 |
| `selected_minutes` | int, null | 🆕 The quantity this plan would be bought at |
| `total_price` | string | **Meaning changed** — the plan's entry price. `amount_due` is what they would actually pay |
| `allocated_credits` | | ❌ Removed |

---

## 9. Step 7 — Renewal, expiry and reactivation

**No request or response change.** Behaviour:

| | Before | After |
|---|---|---|
| Renewal price | `plan.net_base_price` + add-ons | `minutes × today's ₹/min` + add-ons |
| Renewal grant | `plan.allocated_credits` | `minutes × today's credits/min` |
| Quantity | — | Carried forward on the same row |

A renewal is a **future cycle** — it is the one place a price or rate change legitimately
reaches an existing customer, and that rule is unchanged. Superseding a plan's basis rate
today changes what its subscribers pay and receive at their *next* renewal, never
mid-cycle.

Expiry and reactivation are unchanged: allocated credits are forfeited, purchased credits
are frozen rather than confiscated, and a lapsed subscription is quoted at full price
because it has no unused time to credit.

---

## 10. Top-ups and add-ons

**Completely unchanged.** Both are priced in their own units and neither has anything to
do with minutes:

- **Top-ups** are bought in credits at `cost_per_additional_credit`. Still the right
  answer for a one-off overrun.
- **Add-ons** are capacity — agents, seats, storage — priced in rupees per cycle and
  prorated for the remainder of the cycle.

A renewal order is still the plan price plus whatever capacity is still held.

---

## 11. Every API change, in one table

| Endpoint | Change |
|---|---|
| `GET /onboard/plans/for-minutes/` | 🆕 **New.** Match a quantity to a plan, priced |
| `GET /onboard/plans/` | Band, basis and per-tier rupee prices added; `base_price` and `allocated_credits` removed; the four price properties now mean *entry price* |
| `GET /onboard/plans/{id}/` | Same as the list |
| `GET /onboard/plans/dropdown/` | Label now shows the entry price. Otherwise unchanged |
| `POST /onboard/register-pending/` | 🆕 `minutes` — required on a minutes plan. Three new `400`s |
| `POST /onboard/verify-otp/` | `billing_breakdown` gains `selected_minutes`, `price_per_minute_inr`, `allocated_credits` |
| `PATCH /onboard/update-plan/` | 🆕 optional `minutes`; re-validated against the new plan; new `400 MINUTES_OUT_OF_BAND` |
| `POST /onboard/create-order-just-in-time/` | Amount from the quantity; breakdown frozen; new `400 MINUTES_NOT_SELLABLE` |
| `POST /onboard/resume-session/` | 🆕 `selected_minutes`; breakdown gains three fields |
| `POST /onboard/resume-session-token/` | Same |
| `GET /onboard/billing-breakdown/` | 🆕 **New.** Read-only for a temp token: plan with band, `selected_minutes`, breakdown, and `sellable` — whether checkout would accept it |
| `POST /onboard/verify/` | Unchanged contract; subscription now records the quantity |
| `POST /onboard/webhook/` | Unchanged contract; same |
| `GET /billing/subscription/` | 🆕 `selected_minutes`, `allocated_credits`; `scheduled_change.selected_minutes` |
| `GET /billing/subscription/eligible-plans/` | 🆕 `?minutes=`; band fields and `selected_minutes` per entry |
| `POST /billing/subscription/preview-change/` | 🆕 optional `minutes`; quantity in both `money` and `credits` |
| `POST /billing/subscription/change-plan/` | 🆕 optional `minutes`; **same plan + new quantity is now a valid change**; `selected_minutes` in the response |
| `DELETE /billing/subscription/scheduled-change/` | Also clears the scheduled quantity |
| `POST /billing/subscription/renew/` | Unchanged contract; priced from the stored quantity |
| `POST /billing/verify-payment/` | Unchanged contract |
| `POST /billing/top-up/`, `/top-up/quote/` | **Unchanged** |
| `POST /billing/add-ons/`, `/add-ons/quote/` | **Unchanged** |
| `POST /ops/plans/` | `base_price` and `allocated_credits` removed; band, basis and calibration fields added; rates take `price_per_minute_inr` |
| `PATCH /ops/plans/{id}/` | Same fields |
| `PUT /ops/plans/{id}/rates/` | 🆕 `price_per_minute_inr`; new `409 RATE_WOULD_STRAND_BASIS` |
| `POST /ops/plans/{id}/activate/` | 8 new blocking codes |
| `GET /ops/plans/{id}/readiness/` | 🆕 `ladder` block |
| `POST /ops/plans/{id}/clone/` | 🆕 `BAND_COLLIDES` / `BAND_NEEDS_CONVERSION` review notes |

---

## 12. Things that no longer work

Read this section before anything else if you maintain existing integrations.

### A plan cannot grant nothing

The grant is `minutes × credits-per-minute` and both are positive, so **the smallest a
plan can give is one minute's worth**. Pay-as-you-go — "included: nothing, buy what you
need" — is no longer expressible as a plan.

It is still expressible as an **activation decision**: `credit_grant=0`, the same
override a negotiated contract uses. The `onboard_custom_deal` command does this.

### A plan without a basis cannot be sold

There is no `base_price` and no `allocated_credits` left to fall back on, so a plan with
no `credit_basis` has no price and no grant. Activation refuses it, and migration `0035`
retires any that were already active. Existing subscriptions on such a plan keep working
— their terms are in their own snapshot — and can move to a real plan at any time.

### `Plan.total_price` is no longer what gets charged

It is the plan's **entry price** now — what the smallest sellable quantity costs. Use it
for a "from ₹X" label. To charge, quote the actual quantity through `for-minutes/`.

### The `renews_without_payment` check moved

It was a database constraint on `base_price`. The price is cross-table now, so the check
lives in the activation gate as `RENEWS_FREE_BUT_PRICED`. Creating such a plan succeeds
(it is a draft); activating it does not.

---

## 13. Frontend checklist

**Pricing page**

- [ ] Render a minutes input or slider from `GET /onboard/plans/` (bands and rates are
      on the list response — no request per tick)
- [ ] On commit, call `for-minutes/` and show `money.total` and `credits.granted`
- [ ] Show `credits.equivalent_minutes` — the promise is only exact on the basis tier
- [ ] Handle `rounded_up: true` — say which pack covers the request
- [ ] Handle `404 NO_PLAN_FOR_MINUTES` using `below` / `above`
- [ ] Stop reading `base_price` and `allocated_credits`; they are gone

**Signup**

- [ ] Send `minutes: selected_minutes` (not `requested_minutes`) to `register-pending/`
- [ ] Show the three new `billing_breakdown` fields at OTP and on resume
- [ ] Send `minutes` with `update-plan/` when the customer changes their mind; handle
      `MINUTES_OUT_OF_BAND` by offering the band in the error `data`
- [ ] On page refresh, restore the pricing summary from `GET /onboard/billing-breakdown/`
      (read-only, safe on every render); use `resume-session-token/` only when you also
      need `step`. Treat `sellable: false` as "ask for a new quantity" before checkout

**Subscription screen**

- [ ] Show `selected_minutes` and `allocated_credits`
- [ ] Show `scheduled_change.selected_minutes` when a change is pending
- [ ] Offer a "change my minutes" control — same plan, new quantity, via `change-plan/`
- [ ] Pass `?minutes=` to `eligible-plans/` when comparing at a hypothetical usage

**Admin console**

- [ ] Band, basis and the two calibration fields on the plan form
- [ ] `price_per_minute_inr` on every rate row; mark the basis row required
- [ ] Render the `ladder` block on the review screen, including boundary prices
- [ ] Surface the new blocking codes and warnings verbatim — each carries a message
      written to be shown

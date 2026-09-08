# Plans API Documentation

The subscription plans a prospect chooses from during [signup](./signup_flow.md), and the pricing
that flows through to the payment gateway.

**Base path:** `/api/v1/onboard/plans/`

Four things govern this API:

- **It is read-only, for everybody.** `list`, `retrieve`, `dropdown` and `for-minutes`
  require no authentication at all — the pricing page needs them before anyone has an
  account. There is **no write path here for anyone, superuser included**; catalogue
  writes live at [`/api/v1/ops/plans/`](../ops/plans.md), where they are permissioned,
  gated on completeness and audited.
- **A plan sells a band of minutes, not a fixed bundle.** `min_minutes` to `max_minutes`
  is what it sells; `credit_basis` names the call tier it is sold at. The customer's
  quantity decides both the price and the credit grant.
- **Price and grant come from one row.** The `CreditRate` for the plan's `credit_basis`
  carries `price_per_minute_inr` **and** `credits_per_unit`. Money and credits are two
  views of one decision, so they live together and cannot drift.
- **A plan is more than a price.** It also carries what it grants — per-minute rates and
  an allowance of every countable resource. All of it is on the list response, because a
  plan you cannot compare on those is just a number with a name.

> ⚠️ **Changed in 1.0.2.** `base_price` and `allocated_credits` are **gone**, and the four
> derived price fields now mean the plan's *entry price*. See
> [the migration guide](../../flows/plan-v2-minutes.md).

## Endpoints Summary

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/onboard/plans/for-minutes/` | 🆕 Match a quantity of minutes to a plan, priced | **None** |
| `GET` | `/onboard/plans/` | List plans (paginated) | **None** |
| `GET` | `/onboard/plans/{id}/` | Retrieve one plan | **None** |
| `GET` | `/onboard/plans/dropdown/` | Label/value picker options | **None** |

**There are no write endpoints.** `POST`, `PUT`, `PATCH` and `DELETE` return `405`.
Creating and editing plans is [an ops-console operation](../ops/plans.md).

---

## How pricing is calculated

The gross comes from the **quantity of minutes** and the rupee price on the plan's basis
tier. Everything after that is unchanged — each step rounded to 2 decimal places, half-up:

```
gross            = selected_minutes × price_per_minute_inr    (of the credit_basis tier)
discount_amount  = gross × discount_percentage / 100
net_base_price   = gross − discount_amount
tax_amount       = net_base_price × tax_percentage / 100
total_price      = net_base_price + tax_amount
```

Worked example — 1,200 minutes at `1.8000`/min, `discount 0%`, `tax 18%`:

| Field | Value |
|---|---|
| gross | `2160.00` |
| `discount_amount` | `0.00` |
| `net_base_price` | `2160.00` |
| `tax_amount` | `388.80` |
| `total_price` | `2548.80` |

And the credit grant, from the same row:

```
allocated_credits = selected_minutes × credits_per_unit       (of the credit_basis tier)
                  = 1200 × 4  =  4,800 credits
```

> **The four price fields on a plan are its *entry price*.** With the price depending on a
> quantity there is no single figure for a plan, so `net_base_price`, `discount_amount`,
> `tax_amount` and `total_price` answer for the plan's **smallest sellable quantity** —
> the "from ₹X" a pricing page shows. **Never charge from them.** To charge, quote the
> real quantity through [`for-minutes/`](#get-onboardplansfor-minutes).

> **Tax is charged on the discounted price, not the list price.** Discount is applied first, then
> tax on what remains.

> **All money is returned as a string**, e.g. `"4720.00"`, so no float rounding can occur in
> transit. Parse as a decimal, never as a float.

> **`plan_type` does not determine price.** A plan typed `free` whose basis tier has a
> non-zero `price_per_minute_inr` will still be charged, and a `paid` plan priced at zero
> per minute will skip the gateway. What matters is the rupee price on the basis row.

---

## What a plan grants

Beyond price, every plan carries three groups of fields. **All of them are on the list
response**, so a pricing page needs one call.

### Minutes and credits — read-only

| Field | Type | What it means |
|---|---|---|
| `min_minutes` | integer \| null | 🆕 Fewest minutes this plan sells, inclusive. `null` = no floor |
| `max_minutes` | integer \| null | 🆕 Most minutes it sells, inclusive. `null` = no ceiling. Equal to `min_minutes` makes it a **pack** — one fixed quantity |
| `credit_basis` | string \| null | 🆕 The call tier the plan is *sold* at. Both the price and the grant come from that tier's rate row |
| `is_minutes_based` | boolean | 🆕 Derived |
| `is_fixed_quantity` | boolean | 🆕 Derived — `min_minutes == max_minutes` |
| `cost_per_additional_credit` | decimal string | Pre-tax price of one credit on a mid-cycle top-up. **`0.00` disables top-ups** on this plan — the answer to "I need more" becomes "upgrade" |
| `max_rollover_credits` | integer \| null | Ceiling on credits carried into the next cycle. `null` is uncapped. **Superseded by `max_rollover_ratio`** when that is set |
| `max_rollover_ratio` | decimal string \| null | 🆕 The cap as a multiple of the cycle's own grant. `"1.00"` = one cycle's worth. Read in preference to the absolute figure, because one fixed number cannot serve a band whose grant varies several-fold |
| `low_balance_percent` | integer \| null | 🆕 Warn at this percentage of the cycle's grant. Read in preference to `low_balance_threshold` |
| `allow_rollover_on_renewal` | boolean | Whether unspent plan credits survive a renewal |
| `allow_rollover_on_upgrade` | boolean | …an upgrade |
| `allow_rollover_on_downgrade` | boolean | …a downgrade |
| `allow_use_after_expiry` | boolean | Whether credits stay spendable once the cycle lapses. This is what separates pay-as-you-go from a trial that is meant to end |
| `low_balance_threshold` | integer | Warn at or below this absolute balance. `0` disables it. **Superseded by `low_balance_percent`** *(detail response only)* |

These govern only the **allocated** bucket. Credits bought with cash always survive —
see [Wallet → two buckets](../billing/wallet.md#two-buckets-one-balance).

### `entitlements` — read-only

What the plan includes of each countable resource, and what more of it costs. One entry
per resource, sorted by name.

```json
"entitlements": [
  { "resource": "agent", "included": 2, "reset_period": "none",
    "addon_available": true, "price_per_block": "500.00",
    "block_size": 1, "max_additional": 10 },
  { "resource": "campaign_monthly", "included": 10, "reset_period": "monthly",
    "addon_available": false, "price_per_block": "0.00",
    "block_size": 1, "max_additional": null }
]
```

| Field | What it means |
|---|---|
| `resource` | `agent`, `user`, `contact`, `knowledge_base`, `crawl_page`, `campaign_monthly`, `storage_gb` |
| `included` | How many the plan grants. **`null` means unlimited** — a distinct value from a very large number, because otherwise somebody eventually hits it |
| `reset_period` | What the number *means*. `none` is a standing total — three agents is three agents, and deleting one gives the seat back. `daily`, `weekly`, `monthly`, `quarterly`, `half_yearly` or `yearly` make it a windowed allowance: the *act* is counted from when it happened, and deleting the row does not give it back |
| `addon_available` | Whether more can be bought. `false` means upgrading is the only route |
| `price_per_block` | Pre-tax rupees per block, **recurring every cycle** |
| `block_size` | Units per block, rounded up — a 10 GB block bills two blocks for 12 GB |
| `max_additional` | Ceiling on extras, or `null` for none. Caps a plan below the next tier's included amount, so a Starter tenant cannot assemble a Professional footprint at add-on prices |

Edited through the plan's admin page, not this endpoint — "10 agents, +1 at ₹299" is a
sentence about a plan, and a separate API would have you cross-referencing seven rows
against the plan you are actually pricing.

> **Read `reset_period` before rendering the number.** "10" is a different offer
> depending on whether it refills monthly or yearly, so a pricing page that prints the
> figure alone is printing half a fact. Windows are anchored to each tenant's own
> subscription rather than the calendar, so an allowance never straddles a billing
> cycle — and the period is frozen onto the subscription alongside the number, so
> changing a plan's window never re-interprets an allowance somebody is already
> living under.

### `rates` — read-only

What each call tier costs on this plan — **in credits and in rupees**. Only the
**currently effective** rate appears; superseded ones stay in the catalogue and out of
here.

`is_basis` marks the one row that prices a sale. The other two are what let a page say
"and realtime runs ₹3.60/min" rather than leaving the customer to work it out from a
credit rate. Every tier on a well-formed plan works out to the same rupees per credit —
`price_per_minute_inr ÷ credits_per_unit` — and the ops console warns when they differ.

```json
"rates": [
  { "operation": "conversation_high_latency", "unit": "per_minute",
    "credits_per_unit": 4, "price_per_minute_inr": "1.8000",
    "minimum_units": 1, "is_basis": true },
  { "operation": "conversation_low_latency",  "unit": "per_minute",
    "credits_per_unit": 8, "price_per_minute_inr": "3.6000",
    "minimum_units": 1, "is_basis": false },
  { "operation": "static_reel", "unit": "per_minute",
    "credits_per_unit": 2, "price_per_minute_inr": "0.9000",
    "minimum_units": 1, "is_basis": false }
]
```

**A plan missing a rate for a tier cannot price a call on it** — every call on that tier
raises for every tenant on the plan. `python manage.py audit_billing_readiness` reports
which plans have holes.

> **Editing a rate here is not possible, and editing one anywhere does not change what
> existing subscribers pay.** The rate card is copied onto the wallet when a
> subscription activates, renews or changes plan. A price change reaches a tenant at
> their next renewal, never mid-cycle. See
> [Billing → Where the price comes from](../billing/README.md#where-the-price-comes-from).

---

## Endpoint Details

### 0. Match a quantity of minutes to a plan 🆕

**The entry point for the pricing page.** The customer types how many minutes they need;
this returns the one plan that sells that quantity, priced, with the credit grant worked
out. **Public** — no token required.

- **Path**: `GET /api/v1/onboard/plans/for-minutes/`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `minutes` | integer | **yes** | 1 to 1,000,000 |
| `billing_cycle` | string | no | `monthly` (default) or `yearly` — the two ladders are separate |
| `currency` | string | no | `INR` by default |

**`200` response**

```json
{
  "status": true,
  "message": "Plan matched",
  "data": {
    "plan": {
      "id": "0f1c…", "code": "growth", "name": "Growth",
      "description": "For teams running regular campaigns",
      "billing_cycle": "monthly", "currency": "INR",
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
}
```

**Three fields that are easy to confuse**

| Field | Meaning |
|---|---|
| `requested_minutes` | What the customer typed |
| `selected_minutes` | **What is actually sold.** Send *this* to signup and to change-plan |
| `rounded_up` | `true` when a fixed-quantity pack covers a smaller request |

> **`selected_minutes` differs from `requested_minutes` only on a pack.** A catalogue of
> packs — 1,000 / 5,000 / 10,000 — has gaps by construction, so a request for 1,200 is
> rounded **up** to the smallest pack that covers it. Show that plainly: *"the
> 5,000-minute pack covers your 1,200"*. Sending `requested_minutes` onward would charge
> for the pack and grant the smaller amount.

> **`equivalent_minutes` is what makes the offer honest.** The grant is exact on the
> basis tier only — 4,800 credits is 1,200 standard minutes *or* 600 realtime ones.
> A page that prints "1,200 minutes" without saying on which tier is printing half a fact.

**Failure responses**

| Status | `errors` | When | What to show |
|---|---|---|---|
| `400` | `INVALID_MINUTES` | Missing, non-numeric, ≤ 0, or above 1,000,000 | A field error |
| `404` | `NO_PLAN_FOR_MINUTES` | Nothing covers that quantity | `data.below` and `data.above` carry the nearest bands — *"our largest pack is 10,000 minutes"* |
| `409` | `PLAN_NOT_SELLABLE` | A plan matched but the catalogue cannot price it | A configuration fault, not the customer's. Contact-support copy |

```json
// 404
{
  "status": false,
  "message": "We do not have a plan for that many minutes yet.",
  "errors": ["NO_PLAN_FOR_MINUTES"],
  "data": {
    "below": {"id": "…", "code": "scale", "name": "Scale",
              "min_minutes": 2001, "max_minutes": 10000},
    "above": null
  }
}
```

---

### 1. List Plans

The pricing page. **Public** — no token required.

- **Path**: `GET /api/v1/onboard/plans/`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `page` | integer | Page number. Default `1`. | Beyond the last page → **404**. |
| `page_size` | integer | Rows per page. Default `10`. | Capped at `100`. |
| `search` | string | Partial, case-insensitive match on `code`, `name`, `description`. | Words split on spaces and **AND**ed. |
| `ordering` | string | Sort. Default `sort_order`, then newest first. | Only `name`, `sort_order`, `created_at`, optionally `-` prefixed. **`base_price` was removed** — a plan has no single price to sort by. |
| `is_active` | boolean | **Use `?is_active=true` on a public pricing page** — inactive plans are otherwise included. | `true` / `false`. |
| `code` | string | Exact match on the machine code. | — |
| `plan_type` | enum | Exact match. | `paid`, `free`, `pay_as_you_go`. |
| `currency` | enum | Exact match. | `INR` is the only supported value. |
| `billing_cycle` | enum | Exact match. | `monthly`, `yearly`. |
| `tax_percentage__gte` / `__lte` | decimal | Tax range. | — |
| `discount_percentage__gte` / `__lte` | decimal | Discount range. | — |
| `created_at__date` / `__gte` / `__lte` | date / datetime | Creation window. | — |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "1a2b3c4d-…",
        "code": "growth",
        "name": "Growth",

        "min_minutes": 501,
        "max_minutes": 2000,
        "credit_basis": "conversation_high_latency",
        "is_minutes_based": true,
        "is_fixed_quantity": false,

        "discount_percentage": "0.00",
        "discount_amount": "0.00",
        "net_base_price": "901.80",
        "tax_percentage": "18.00",
        "tax_amount": "162.32",
        "total_price": "1064.12",
        "currency": "INR",
        "billing_cycle": "monthly",
        "billing_cycle_display": "Monthly",
        "plan_type": "paid",
        "plan_type_display": "Paid",
        "sort_order": 2,
        "is_most_popular": true,
        "is_active": true,
        "created_at": "2026-06-03T18:00:00Z",

        "cost_per_additional_credit": "0.54",
        "max_rollover_credits": null,
        "max_rollover_ratio": "1.00",
        "low_balance_percent": 10,
        "allow_rollover_on_renewal": true,
        "allow_rollover_on_upgrade": true,
        "allow_rollover_on_downgrade": false,
        "allow_use_after_expiry": false,

        "entitlements": [
          { "resource": "agent", "included": 2, "addon_available": true,
            "price_per_block": "500.00", "block_size": 1, "max_additional": 10 }
        ],
        "rates": [
          { "operation": "conversation_high_latency", "unit": "per_minute",
            "credits_per_unit": 5, "minimum_units": 1 }
        ]
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

> **Deleted plans are excluded automatically, but inactive ones are not.** Always filter with
> `?is_active=true` for anything customer-facing, or you will advertise a retired plan.

> **Order by `sort_order` is the default and is what the pricing page wants** — it is the
> deliberate display order, unlike `created_at`.

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more plans. | `success` |
| **400** | Unknown enum value, or a malformed number or date in a filter. | filter error |
| **404** | `page` beyond the last page. | `Invalid page.` |

---

### 2. Retrieve a Plan

- **Path**: `GET /api/v1/onboard/plans/{id}/` — **public**.
- **Success Response (200 OK)**: as §1, plus `description`, `currency_display`,
  `low_balance_threshold`, and `updated_at`.

| Status | When | `message` |
|---|---|---|
| **200** | Found — **including inactive plans**. | `Retrieved successfully` |
| **404** | No such plan, or soft-deleted. | `Not found.` |

> **An inactive plan retrieves successfully.** Only soft-deleted ones 404. Check `is_active`
> yourself before offering it, and note that [register-pending](./signup_flow.md#3-start-the-signup)
> rejects inactive plans with `"Selected plan is invalid or inactive."`

---

### 3. Writing a plan — not here

`POST`, `PUT`, `PATCH` and `DELETE` on this path return **`405 Method Not Allowed`**, for
everybody including a superuser.

They used to work, and that was the problem: a plan created through them was instantly
sellable with **zero entitlements and zero rates**. A missing allowance is not a limit of
zero but no limit at all, and a missing rate cannot price a call — so such a plan was
simultaneously unlimited and unbillable, silently, because neither failure raises anything.

Catalogue writes live at [`/api/v1/ops/plans/`](../ops/plans.md), where they require the
`plan.change` permission, demand a `reason`, leave an audit row, and — the point — are
**refused unless the plan is complete**.

---


### 4. Dropdown

- **Path**: `GET /api/v1/onboard/plans/dropdown/` — **public**.
- **Query Parameters**: `limit` (≤ **1000**; over that → **400 `Limit cannot exceed 1000`**), plus all List filters and `search`.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [
      {
        "label": "Growth (Paid - 1064.12 INR/Monthly)",
        "value": "1a2b3c4d-…",
        "is_most_popular": true
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

`label` is built as `"<name> (<Type> - <total_price> <currency>/<Cycle>)"`, so it is
already human-readable. **`total_price` here is the plan's entry price** — what its
smallest sellable quantity costs — so the label reads as a "from" figure. This dropdown uniquely also returns **`is_most_popular`**, letting you badge an
option without a second call.

> **Filter with `?is_active=true`.** The dropdown does not exclude inactive plans on your behalf.

---

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **`discount_percentage` is not capped at 100.** | A misconfigured plan can compute a negative `total_price`. The ops console warns (`DISCOUNT_OVER_100`); nothing refuses it |
| 2 | **`plan_type` is decorative.** What gets charged is the basis tier's rupee price, not the type. | A plan labelled `free` whose basis is priced still charges; one labelled `paid` at ₹0/min skips payment |
| 3 | **A retired plan's `code` and `name` stay reserved permanently.** | Codes cannot be reused. Nothing is ever deleted, deliberately — subscriptions and audit rows point at these |
| 4 | **`is_most_popular` is not exclusive.** | Several plans can carry the badge at once |
| 5 | **Editing a live plan's rate affects unpaid in-flight orders.** | Mostly closed: an order freezes its per-minute figures, so a customer is charged and granted what they were quoted. What *can* still change is a **new** quote between page load and submission |
| 6 | **The four price fields are an entry price, and look like a plan price.** | A client that charges from `total_price` will charge for the plan's smallest quantity regardless of what the customer chose. Quote through `for-minutes/` |
| 7 | **The rollover flags are read-only here but their effect is frozen at subscribe time.** | Changing them in the console alters what *new and renewing* subscribers get, never a live cycle. Deliberate — but the pricing page and an existing tenant's terms can legitimately disagree |

## Related

- [Signup flow](./signup_flow.md) — where a plan is selected and charged
- [Razorpay webhook](./webhooks.md) — how payment completion is confirmed
- [Billing overview](../billing/README.md) — credits vs entitlements, and what a minute costs
- [Subscription API](../billing/subscription.md) — how a tenant moves between these plans

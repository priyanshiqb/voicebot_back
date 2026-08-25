# Plans API Documentation

The subscription plans a prospect chooses from during [signup](./signup_flow.md), and the pricing
that flows through to the payment gateway.

**Base path:** `/api/v1/onboard/plans/`

Three things govern this API:

- **Reads are public; writes are superuser-only.** `list`, `retrieve`, and `dropdown` require no
  authentication at all — the pricing page needs them before anyone has an account. Everything else
  demands a Django superuser.
- **You set percentages; the server computes money.** `base_price`, `discount_percentage`, and
  `tax_percentage` are yours to write. `discount_amount`, `net_base_price`, `tax_amount`, and
  `total_price` are derived and read-only.
- **`total_price` is what gets charged.** [create-order](./signup_flow.md#10-create-the-order)
  multiplies it by 100 and hands that to the gateway, so a plan whose `total_price` is `0.00`
  bypasses payment entirely and completes onboarding on the spot.
- **A plan is more than a price.** It also carries what it grants — credits, per-minute
  rates, and an allowance of every countable resource. All of it is on the list
  response, because a plan you cannot compare on those is just a number with a name.

## Endpoints Summary

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/onboard/plans/` | List plans (paginated) | **None** |
| `GET` | `/onboard/plans/{id}/` | Retrieve one plan | **None** |
| `GET` | `/onboard/plans/dropdown/` | Label/value picker options | **None** |
| `POST` | `/onboard/plans/` | Create a plan | Superuser |
| `PUT` / `PATCH` | `/onboard/plans/{id}/` | Update a plan | Superuser |
| `DELETE` | `/onboard/plans/{id}/` | Soft-delete a plan | Superuser |
| `POST` | `/onboard/plans/bulk_delete/` | Bulk soft-delete | Superuser |

---

## How pricing is calculated

Given `base_price`, `discount_percentage`, and `tax_percentage`, every other figure is derived —
each step rounded to 2 decimal places, half-up:

```
discount_amount  = base_price × discount_percentage / 100
net_base_price   = base_price − discount_amount
tax_amount       = net_base_price × tax_percentage / 100
total_price      = net_base_price + tax_amount
```

Worked example — `base_price: 4000.00`, `discount_percentage: 0.00`, `tax_percentage: 18.00`:

| Field | Value |
|---|---|
| `discount_amount` | `0.00` |
| `net_base_price` | `4000.00` |
| `tax_amount` | `720.00` |
| `total_price` | `4720.00` |

> **Tax is charged on the discounted price, not the list price.** Discount is applied first, then
> tax on what remains.

> **All money is returned as a string**, e.g. `"4720.00"`, so no float rounding can occur in
> transit. Parse as a decimal, never as a float.

> **`plan_type` does not determine price.** A plan typed `free` with a non-zero `base_price` will
> still be charged, and a `paid` plan priced at zero will skip the gateway. What matters is
> `total_price`.

---

## What a plan grants

Beyond price, every plan carries three groups of fields. **All of them are on the list
response**, so a pricing page needs one call.

### Credits — writable

| Field | Type | What it means |
|---|---|---|
| `allocated_credits` | integer | Credits granted at the start of **every** billing cycle |
| `cost_per_additional_credit` | decimal string | Pre-tax price of one credit on a mid-cycle top-up. **`0.00` disables top-ups** on this plan — the answer to "I need more" becomes "upgrade" |
| `max_rollover_credits` | integer \| null | Ceiling on credits carried into the next cycle. `null` is uncapped — without a cap a light user on an annual plan can bank a year's worth |
| `allow_rollover_on_renewal` | boolean | Whether unspent plan credits survive a renewal |
| `allow_rollover_on_upgrade` | boolean | …an upgrade |
| `allow_rollover_on_downgrade` | boolean | …a downgrade |
| `allow_use_after_expiry` | boolean | Whether credits stay spendable once the cycle lapses. This is what separates pay-as-you-go from a trial that is meant to end |
| `low_balance_threshold` | integer | Warn the tenant at or below this balance. `0` disables the alert *(detail response only)* |

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

The per-minute credit price of each call tier on this plan. Only the **currently
effective** rate appears; superseded ones stay in the catalogue and out of here.

```json
"rates": [
  { "operation": "conversation_high_latency", "unit": "per_minute",
    "credits_per_unit": 5, "minimum_units": 1 },
  { "operation": "conversation_low_latency",  "unit": "per_minute",
    "credits_per_unit": 10, "minimum_units": 1 },
  { "operation": "static_reel", "unit": "per_minute",
    "credits_per_unit": 2, "minimum_units": 1 }
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

### 1. List Plans

The pricing page. **Public** — no token required.

- **Path**: `GET /api/v1/onboard/plans/`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `page` | integer | Page number. Default `1`. | Beyond the last page → **404**. |
| `page_size` | integer | Rows per page. Default `10`. | Capped at `100`. |
| `search` | string | Partial, case-insensitive match on `code`, `name`, `description`. | Words split on spaces and **AND**ed. |
| `ordering` | string | Sort. Default `sort_order`, then newest first. | Only `name`, `base_price`, `sort_order`, `created_at`, optionally `-` prefixed. |
| `is_active` | boolean | **Use `?is_active=true` on a public pricing page** — inactive plans are otherwise included. | `true` / `false`. |
| `code` | string | Exact match on the machine code. | — |
| `plan_type` | enum | Exact match. | `paid`, `free`, `pay_as_you_go`. |
| `currency` | enum | Exact match. | `INR` is the only supported value. |
| `billing_cycle` | enum | Exact match. | `monthly`, `yearly`. |
| `base_price__gte` / `base_price__lte` | decimal | Price range. | — |
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
        "code": "PRO",
        "name": "Pro",
        "base_price": "4000.00",
        "discount_percentage": "0.00",
        "discount_amount": "0.00",
        "net_base_price": "4000.00",
        "tax_percentage": "18.00",
        "tax_amount": "720.00",
        "total_price": "4720.00",
        "currency": "INR",
        "billing_cycle": "monthly",
        "billing_cycle_display": "Monthly",
        "plan_type": "paid",
        "plan_type_display": "Paid",
        "sort_order": 2,
        "is_most_popular": true,
        "is_active": true,
        "created_at": "2026-06-03T18:00:00Z",

        "allocated_credits": 4000,
        "cost_per_additional_credit": "1.00",
        "max_rollover_credits": 2000,
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

### 3. Create a Plan

**Superuser only.**

- **Path**: `POST /api/v1/onboard/plans/`
- **Request Payload**:
  ```json
  {
    "code": "PRO",
    "name": "Pro",
    "description": "For growing teams.",
    "base_price": "4000.00",
    "discount_percentage": "0.00",
    "tax_percentage": "18.00",
    "currency": "INR",
    "billing_cycle": "monthly",
    "plan_type": "paid",
    "sort_order": 2,
    "is_most_popular": true,

    "allocated_credits": 4000,
    "cost_per_additional_credit": "1.00",
    "max_rollover_credits": 2000,
    "allow_rollover_on_renewal": true,
    "allow_rollover_on_upgrade": true,
    "allow_rollover_on_downgrade": false,
    "allow_use_after_expiry": false,
    "low_balance_threshold": 200
  }
  ```

> **Creating a plan does not give it a rate card or allowances.** `entitlements` and
> `rates` are read-only here and start empty — a plan created through this endpoint
> **cannot price a call** until its rates are added in the admin. Configure both before
> anyone subscribes.

### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `code` * | string | Machine-readable identifier, e.g. `PRO`. Used by integrations and reporting. | Required. Max **50**. **Unique across all plans**, including soft-deleted ones. |
| `name` * | string | Display name on the pricing page. | Required. Max **100**. **Unique**, including soft-deleted ones. |
| `description` | string | Feature blurb. No length limit. | Optional, nullable. |
| `base_price` | decimal | List price before discount and tax. | Optional, default `0.00`. Max 10 digits, 2 decimal places. **Negatives are not rejected.** |
| `discount_percentage` | decimal | Percentage off, e.g. `10.00` for 10%. | Optional, default `0.00`. Max 5 digits, 2 decimals — so up to `999.99`. **Not capped at 100**, which would make `net_base_price` negative. |
| `tax_percentage` | decimal | Tax rate, e.g. `18.00` for 18%. | Optional, default `0.00`. Same limits as above. |
| `currency` | enum | ISO currency code. | Optional, default `INR`. **`INR` is the only permitted value.** |
| `billing_cycle` | enum | How often it renews. | Optional, default `monthly`. `monthly` or `yearly`. |
| `plan_type` | enum | Classification, for display and reporting. | Optional, default `free`. `paid`, `free`, or `pay_as_you_go`. Does **not** affect what is charged. |
| `sort_order` | integer | Display position. Lower comes first. | Optional, default `0`. Must be ≥ 0. |
| `is_most_popular` | boolean | Renders the "most popular" badge. | Optional, default `false`. **Not exclusive** — you can flag several plans at once. |
| `is_active` | boolean | Whether the plan may be listed and selected. | Optional, default `true`. |
| `allocated_credits` | integer | Credits granted every cycle. | Optional, default `0`. Must be ≥ 0. |
| `cost_per_additional_credit` | decimal | Pre-tax price of one top-up credit. | Optional, default `0.00`. **`0.00` disables top-ups.** |
| `max_rollover_credits` | integer \| null | Cap on credits carried forward. | Optional, nullable. `null` is uncapped. |
| `allow_rollover_on_renewal` | boolean | Unspent plan credits survive a renewal. | Optional, default `false`. |
| `allow_rollover_on_upgrade` | boolean | …an upgrade. | Optional, default `false`. |
| `allow_rollover_on_downgrade` | boolean | …a downgrade. | Optional, default `false`. |
| `allow_use_after_expiry` | boolean | Credits stay spendable after the cycle lapses. | Optional, default `false`. |
| `low_balance_threshold` | integer | Alert at or below this balance. | Optional, default `0` — which disables the alert. |

**Read-only — computed:** `discount_amount`, `net_base_price`, `tax_amount`, `total_price`, plus
`id`, `created_at`, `updated_at`, the three `*_display` fields, and **`entitlements` and
`rates`**.

> **`renews_without_payment` is not exposed on this API.** It makes a zero-cost cycle
> roll over instead of expiring — the pay-as-you-go and free-tier shape — and a database
> constraint refuses it on any plan with a non-zero `base_price`. Set it in the admin.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **201** | Created. | `Created successfully` | `null` |
| **400** | `code` or `name` already used. | `plan with this code already exists.` | `{"code": [...]}` |
| **400** | `code` or `name` missing. | `This field is required.` | `{"<field>": [...]}` |
| **400** | A price or percentage exceeds its digit limits. | `Ensure that there are no more than 10 digits in total.` | `{"<field>": [...]}` |
| **400** | Unknown `currency`, `billing_cycle`, or `plan_type`. | `"x" is not a valid choice.` | `{"<field>": [...]}` |
| **401** | Not authenticated. | `Authentication credentials were not provided.` | `{"detail": …}` |
| **403** | Authenticated but not a superuser. | permission denied | `{"detail": …}` |

> **Uniqueness ignores soft deletion.** A deleted plan keeps its `code` and `name` reserved
> forever, so retiring `PRO` means you can never create another `PRO`. Rename before deleting if
> you may want the code back.

---

### 4. Update a Plan

- **Path**: `PUT` / `PATCH /api/v1/onboard/plans/{id}/` — **superuser only**.
- **Request Payload**: any subset of the writable fields from §3.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Updated. | `Updated successfully` | `null` |
| **400** | Any validation error from §3. | as §3 | `{"<field>": [...]}` |
| **401** / **403** | As in §3. | — | — |
| **404** | No such plan, or soft-deleted. | `Not found.` | `{"detail": …}` |

> **Changing a price does not change what existing subscribers pay.** A `Subscription` created at
> signup points at the plan, so an edit here affects **new** orders — and, dangerously, any
> in-flight signup that has an order created but unpaid. Prefer creating a new plan and
> deactivating the old one over editing a live plan's price.

> **Deactivating a plan mid-signup strands anyone who selected it.**
> [update-plan](./signup_flow.md#8-update-the-plan) and
> [register-pending](./signup_flow.md#3-start-the-signup) both reject inactive plans, so a user who
> resumes will be unable to proceed until they choose another.

---

### 5. Delete a Plan

- **Path**: `DELETE /api/v1/onboard/plans/{id}/` — **superuser only**.
- **Success Response (200 OK)**: `{ "status": true, "message": "Deleted successfully", "data": null, "errors": null }`

| Status | When | `message` |
|---|---|---|
| **200** | Soft-deleted; it disappears from every list. | `Deleted successfully` |
| **401** / **403** | As in §3. | — |
| **404** | No such plan, or already deleted. | `Not found.` |

> **There is no in-use guard.** Deleting a plan that pending registrations or active subscriptions
> point at is allowed. `TempUser.selected_plan` and `Subscription.plan` both survive as references
> to the deleted row, so existing subscribers are not broken — but a resuming signup will show a
> plan nobody can select. Deactivate rather than delete for anything that has been sold.

> **`code` and `name` stay reserved.** See §3.

---

### 6. Dropdown

- **Path**: `GET /api/v1/onboard/plans/dropdown/` — **public**.
- **Query Parameters**: `limit` (≤ **1000**; over that → **400 `Limit cannot exceed 1000`**), plus all List filters and `search`.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [
      {
        "label": "Pro (Paid - 4720.00 INR/Monthly)",
        "value": "1a2b3c4d-…",
        "is_most_popular": true
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

`label` is built as `"<name> (<Type> - <total_price> <currency>/<Cycle>)"`, so it is already
human-readable. This dropdown uniquely also returns **`is_most_popular`**, letting you badge an
option without a second call.

> **Filter with `?is_active=true`.** The dropdown does not exclude inactive plans on your behalf.

---

### 7. Bulk Delete

- **Path**: `POST /api/v1/onboard/plans/bulk_delete/` — **superuser only**.
- **Request Payload**: `{ "ids": ["1a2b…", "…"] }`

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Plans to soft-delete. | Non-empty list. Every id must exist and be undeleted. All-or-nothing. |

| Status | When | `message` |
|---|---|---|
| **200** | All soft-deleted. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | Any id unknown or already deleted — **nothing is deleted**. | `Some ids are invalid` |
| **401** / **403** | As in §3. | — |

> **Duplicate ids fail the whole request.** De-duplicate client-side.

---

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **`discount_percentage` is not capped at 100**, and `base_price` may be negative. | A misconfigured plan can compute a negative `total_price`, which becomes a negative gateway amount. |
| 2 | **`plan_type` is decorative.** What gets charged is `total_price`, not the type. | A plan labelled `free` with a price still charges; one labelled `paid` at zero skips payment silently. |
| 3 | **Deleting a plan has no in-use guard**, and its `code` and `name` stay reserved permanently. | Retired codes cannot be reused, and in-flight signups can be left pointing at an unselectable plan. |
| 4 | **`is_most_popular` is not exclusive.** | Several plans can carry the badge at once. |
| 5 | **Editing a live plan's price affects unpaid in-flight orders.** | A user who created an order and then paid could be charged an amount that differs from what they were shown. Existing *subscribers* are unaffected — their terms are frozen on the subscription. |
| 6 | **A plan can be created with no rates and no allowances**, and nothing refuses it. | Every call by a tenant on that plan raises `RateNotConfigured`, and `entitlements` comes back empty so no limit applies. Run `audit_billing_readiness` after adding a plan. |
| 7 | **`allocated_credits` and the rollover flags are writable but their effect is frozen at subscribe time.** | Changing them alters what *new and renewing* subscribers get, never a live cycle. That is deliberate — but it means the pricing page and an existing tenant's terms can legitimately disagree. |

## Related

- [Signup flow](./signup_flow.md) — where a plan is selected and charged
- [Razorpay webhook](./webhooks.md) — how payment completion is confirmed
- [Billing overview](../billing/README.md) — credits vs entitlements, and what a minute costs
- [Subscription API](../billing/subscription.md) — how a tenant moves between these plans

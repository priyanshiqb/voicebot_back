# Purchases API Documentation

Buying more of what a plan does not already include: credits, or capacity.

**Base path:** `/api/v1/billing/`

The two behave differently, and the difference is not arbitrary:

| | Credit top-up | Entitlement add-on |
|---|---|---|
| Buys | Talk time | Capacity — agents, seats, storage |
| Priced | Flat, per credit | Per **block**, recurring every cycle |
| Prorated | **No** | **Yes**, for the rest of the current cycle |
| One-off or recurring | One-off | **Recurring** — re-charged at every renewal |
| Effect | Credits in the purchased bucket | A higher limit |
| Writes a ledger row | Yes | **No** — capacity is never a credit |

A credit is worth the same whenever it is bought and never expires out of the
purchased bucket. Capacity is *rented* for the remainder of the cycle, so buying 20 GB
on day 20 of 30 costs a third of a cycle's price.

## Endpoints Summary

- `POST /api/v1/billing/top-up/quote/` — Price N credits
- `POST /api/v1/billing/top-up/` — Order N credits
- `POST /api/v1/billing/add-ons/quote/` — Price extra capacity
- `POST /api/v1/billing/add-ons/` — Order extra capacity

Every one requires a **live** subscription (`409 NO_ACTIVE_SUBSCRIPTION` otherwise), and
only one unpaid order may be open at a time (`409 PENDING_ORDER_EXISTS`).

> **These are the endpoints that stay closed to a lapsed tenant**, and the only ones.
> Everything under `/billing/subscription/` answers for an expired subscription so the
> tenant can see their state and come back — but credits and capacity both belong to a
> cycle, and selling either for a cycle that is not running would take money for
> something unusable until they renew anyway. See
> [Subscription → When the cycle has ended](./subscription.md#when-the-cycle-has-ended).

---

## Credit top-ups

### `POST /billing/top-up/quote/`

**Request:** `{ "credits": 500 }`

```json
{ "data": {
    "net": "600.00", "tax": "108.00", "total": "708.00",
    "tax_percentage": "18.00", "credits": "500", "unit_price": "1.20" } }
```

Priced at the plan's `cost_per_additional_credit`, taxed at the plan's rate. A plan
with that field at `0.00` does not sell credits at all and returns
`409 TOP_UPS_UNAVAILABLE` — on the free plan, upgrading is the way to get more.

A single order is capped at 1,000,000 credits. Not policy so much as a guard: a
mistyped quantity should be refused, not charged.

### `POST /billing/top-up/`

Same request. Returns a Razorpay order:

```json
{ "data": {
    "razorpay_order_id": "order_9273770b23b548",
    "amount": 70800,
    "amount_display": "708.00",
    "currency": "INR",
    "razorpay_key_id": "rzp_test_…",
    "expires_at": "2026-08-24T09:44:22Z",
    "breakdown": { "net": "600.00", "tax": "108.00", "total": "708.00", "credits": "500" } } }
```

> `amount` is **paise**, for the checkout widget. `amount_display` is rupees, for
> humans. Sending the wrong one to Razorpay charges a hundred times too much or too
> little.

**No credits are granted here.** They land in the `purchased` bucket when the
`payment.captured` webhook arrives, keyed on the order id — so a redelivered webhook
credits exactly once.

---

## Entitlement add-ons

### `POST /billing/add-ons/quote/`

**Request:** `{ "resource": "storage_gb", "quantity": 20 }`

Valid resources: `agent`, `user`, `contact`, `knowledge_base`, `crawl_page`,
`campaign_monthly`, `storage_gb`.

```json
{ "data": {
    "net": "132.65", "tax": "23.88", "total": "156.53", "tax_percentage": "18.00",
    "resource": "storage_gb",
    "additional_units": "20", "quantity_after": "20",
    "addon_step": "10", "price_per_block": "199.00",
    "recurring_net": "398.00", "added_recurring_net": "398.00",
    "unused_ratio": "0.3333" } }
```

**Two numbers, and they are different:**

- `added_recurring_net` — ₹398, what this adds to **every future renewal**
- `total` — ₹156.53, what is charged **now**, prorated for the third of the cycle
  remaining

Showing only the prorated figure would understate the commitment being taken on.

**Blocks, rounded up.** Storage sells in 10 GB blocks at ₹199, so 20 GB is two blocks
and 12 GB is also two. The delta is priced as *(price of the new total) − (price of
what they already pay for)*, so buying 10 GB twice costs exactly what buying 20 GB once
does — block rounding cannot be gamed by splitting a purchase.

| Status | When |
|---|---|
| `409 ADDON_UNAVAILABLE` | Not sellable on this plan, or beyond `max_additional` |

`max_additional` caps each plan below the next tier's included amount. Without it a
Starter tenant could assemble a Professional footprint at add-on prices.

### `POST /billing/add-ons/`

Same request. Returns a Razorpay order in the shape above.

**Late in a cycle the prorated amount can fall below ₹1**, which a gateway will not
accept. Refusing a purchase the tenant wants would be absurd, so the capacity is
granted outright and the full price starts at the next renewal:

```json
{ "data": { "charged": false, "breakdown": { "…": "…" } },
  "message": "Capacity added at no charge for the remainder of this cycle; the full amount applies from your next renewal." }
```

### What a paid add-on does

- `additional_quantity` increases on the tenant's entitlement row
- `price_per_unit` and `addon_step` are **frozen at purchase**

A later catalogue rise does not re-price capacity a tenant already holds; buying *more*
re-prices the whole block to current. That is how seat billing works — "you have 5
extra agents at ₹299 each", not five rows at four historical prices.

**Extras survive plan changes and are re-charged each cycle.** They are included in
renewal orders and credited into upgrade proration.

---

## Hitting a limit

Once `entitlements_enforced` is on for a tenant, creating a resource past its limit
returns **402**:

```json
{ "status": false,
  "message": "You are using 3 of 3 available agent. Upgrade your plan or buy more capacity to continue.",
  "errors": ["QUOTA_EXCEEDED"],
  "data": { "resource": "agent", "used": 3, "limit": 3, "requested": 1 } }
```

Guarded endpoints: agents, contacts, knowledge bases, campaigns, users, and crawled
pages. Creating a knowledge base with 50 websites consumes one knowledge base *and*
fifty crawl pages, and both are checked — otherwise a tenant could hold unlimited pages
by putting them all in one KB.

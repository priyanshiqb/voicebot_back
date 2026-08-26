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

> **The arithmetic behind every price on this page** — proration, plan changes,
> add-on deltas, and how a payment travels from Checkout to applied — is in
> [How Money Moves](./money-and-payments.md).

- `POST /api/v1/billing/top-up/quote/` — Price N credits
- `POST /api/v1/billing/top-up/` — Order N credits
- `POST /api/v1/billing/add-ons/quote/` — Price extra capacity
- `POST /api/v1/billing/add-ons/` — Order extra capacity
- `POST /api/v1/billing/verify-payment/` — Confirm a payment from the browser
- `GET  /api/v1/billing/transactions/` — Payment history
- `GET  /api/v1/billing/transactions/{order_id}/` — One payment
- `POST /api/v1/billing/transactions/{order_id}/cancel/` — Abandon an unpaid order
- `POST /api/v1/billing/transactions/{order_id}/retry/` — Resume paying one

The four quote/order endpoints require a **live** subscription
(`409 NO_ACTIVE_SUBSCRIPTION` otherwise), and only one unpaid order may be open at a
time (`409 PENDING_ORDER_EXISTS`). `verify-payment/` and everything under
`transactions/` have neither restriction — they act on orders that already exist.

**Blocked by `PENDING_ORDER_EXISTS`?** The 409 hands back the blocking
`razorpay_order_id`. Take it to `POST /transactions/{order_id}/cancel/` to abandon it,
or `POST /transactions/{order_id}/retry/` to finish paying it.

**The full flow for any purchase:**

```
1. POST .../quote/          optional — show the price first
2. POST .../               creates the Razorpay order
3. ── Razorpay Checkout opens in the browser ──
4. POST /verify-payment/   confirm it, and get the new balance back
   └─ races the webhook at POST /api/v1/onboard/webhook/, which
      applies the same purchase if the browser never comes back
```

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

---

## `POST /verify-payment/`

Confirm a payment the browser has just completed. Call this when Razorpay Checkout
reports success, with the three values it hands back.

**This does not replace the webhook — it races it.** Whichever arrives first applies
the purchase; the other sees a transaction already marked successful and returns the
same body. Not calling it loses nothing, but the tenant then waits on Razorpay's
server-to-server call to see their credits.

### Request

```json
{
  "razorpay_order_id": "order_PxYz123",
  "razorpay_payment_id": "pay_PxYz456",
  "razorpay_signature": "9ef4dffbfd84f1318f6739a3ce19f9d85851857ae648f114332d8401e0949a3d"
}
```

| Field | Type | Description |
|---|---|---|
| `razorpay_order_id` * | string | The order id returned when the purchase was created. |
| `razorpay_payment_id` * | string | Razorpay's id for the payment that settled the order. |
| `razorpay_signature` * | string | HMAC Razorpay returns alongside the payment. Proves the browser is not reporting a success that never happened. |

Works for **top-ups, add-ons, renewals and upgrades** — one endpoint for every
purchase type. Signup payments are the exception: they belong to a TempUser rather
than a tenant, and go through `/api/v1/onboard/verify-payment/` instead.

### Response `200`

```json
{
  "status": true,
  "message": "Payment verified",
  "data": {
    "razorpay_order_id": "order_PxYz123",
    "transaction_type": "top_up",
    "amount": "1180.00",
    "already_applied": false,
    "credits": { "total": 1500, "allocated": 500, "purchased": 1000 },
    "entitlements": [ { "resource": "agent", "included": 2, "used": 1, "...": "..." } ]
  }
}
```

| Field | Meaning |
|---|---|
| `already_applied` | `true` when the webhook got there first. **The purchase is still complete** — it means this call changed nothing, not that it failed. |
| `credits` | Wallet balance *after* the purchase. `null` if it could not be read. |
| `entitlements` | Every resource with its limit and live usage — the same shape `GET /billing/subscription/` returns. `null` when there is no active subscription. |

> **The balance comes back with the confirmation.** The whole point of this endpoint is
> removing a round trip; following it with `GET /billing/subscription/` would put that
> round trip back.

### Responses

| Code | When | `errors` |
|---|---|---|
| **200** | Applied, or already applied. Check `already_applied` to tell which. | — |
| **400** | A field is missing. | validation detail |
| **400** | The signature does not verify. **The order is marked `FAILED`.** | `SIGNATURE_VERIFICATION_FAILED` |
| **404** | No such order, or it belongs to another tenant. | `TRANSACTION_NOT_FOUND` |
| **409** | An `onboarding` payment. Use the onboarding flow. | `WRONG_ENDPOINT` |
| **409** | The order expired and was swept before the browser returned. | `ORDER_EXPIRED` |
| **409** | Razorpay reports the payment as authorised, not captured. Stays `pending`. | `PAYMENT_NOT_CAPTURED` |
| **409** | The amount received does not match the order. Marked `underpaid`; **nothing granted**. | `AMOUNT_MISMATCH` |
| **400** | The payment belongs to a different order. | `PAYMENT_ORDER_MISMATCH` |
| **401** / **403** | As elsewhere. | — |

> **"Not yours" and "does not exist" both read as 404**, deliberately. Distinguishing
> them would be an enumeration oracle over every order id in the system.

> **`ORDER_EXPIRED` needs a human.** An unpaid order is cancelled after 30 minutes. If
> the browser then reports a payment against it, money may have been taken against a
> dead order — so the endpoint refuses rather than applying the purchase, logs at
> ERROR, and returns the order id for support to trace.

### Idempotency

Two layers, the same as the webhook:

1. **The transaction row is locked** before its status is read, so a webhook arriving
   in the same instant blocks until this commits and then sees `SUCCESSFUL`.
2. **Credit grants are keyed on the order id** (`CreditLedger` unique
   `(entry_type, reference_id)`), so even a bypassed outer guard deposits once.

Calling it five times in a row credits exactly once — there is a test that says so.

### Where the effect actually lives

Both this endpoint and the webhook call `onboard.services.payment_application.apply()`.
The dispatch used to live inside the webhook view, which meant the browser path and
the server path each had their own copy — the shape defects grow in, where a fix to
one side leaves the other applying a stale rule.

---

## `GET /transactions/`

Every payment this tenant has made or attempted, newest first — signup, renewals, plan
changes, credit top-ups and capacity add-ons.

Signup payments appear here too. Hiding the first charge would make the list look like
it starts mid-story.

### Query parameters

| Parameter | Description |
|---|---|
| `status` | `pending` · `successful` · `failed` · `canceled` · `underpaid` |
| `transaction_type` | `onboarding` · `renewal` · `upgrade` · `top_up` · `addon` |
| `created_after` / `created_before` | ISO datetimes |
| `search` | Matches `razorpay_order_id` or `razorpay_payment_id` |
| `ordering` | `created_at`, `amount`, `status` — prefix `-` to reverse |

### Response `200`

```json
{
  "id": "…",
  "razorpay_order_id": "order_PxYz123",
  "razorpay_payment_id": "pay_PxYz456",
  "transaction_type": "top_up",
  "status": "successful",
  "amount": "1180.00",
  "summary": "1,000 credits",
  "can_cancel": false,
  "can_retry": false,
  "expires_at": null,
  "created_at": "2026-08-26T09:12:00Z"
}
```

| Field | Meaning |
|---|---|
| `summary` | What the payment bought, in words — `"1,000 credits"`, `"3 × agent"`, `"Starter → Professional"`. Built from the breakdown **frozen at order time**, so a historical row keeps describing what the tenant agreed to even after prices or plan names move. |
| `can_cancel` / `can_retry` | Whether those actions would succeed **right now**. |

> **Use `can_retry`, don't infer it from `status`.** An order whose `expires_at` has
> passed is still `pending` in the database until something sweeps it — rendering a
> "Pay now" button on that would send the tenant to a dead order. Both flags already
> account for it.

> **`raw_webhook_payload` is deliberately absent.** It is gateway diagnostics and can
> carry contact details from the payment method. It stays in the admin.

---

## `GET /transactions/{order_id}/`

**Looked up by `razorpay_order_id`, not by database id** — that is the identifier every
other part of the payment flow speaks, including the `409 PENDING_ORDER_EXISTS` that
sends a tenant here in the first place.

Adds `plan_name`, `target_plan_name`, `credits_purchased`, `amount_breakdown` (the full
price breakdown as quoted) and `updated_at`.

---

## `POST /transactions/{order_id}/cancel/`

Abandons an order that was never paid, releasing the one-open-order lock so the tenant
can start a different purchase immediately.

**This is what `409 PENDING_ORDER_EXISTS` has always told tenants to do.** Until this
endpoint existed there was no way to do it — the only route past the block was waiting
out the 30-minute TTL.

**The row is kept, not deleted.** It stays in the history as `canceled`, because a
payment record that disappears is worse than one that says nothing happened.

| Code | When | `errors` |
|---|---|---|
| **200** | Cancelled. Returns the updated row. | — |
| **409** | Already paid, failed or cancelled. | `ORDER_NOT_PENDING` |
| **409** | A signup payment — use the onboarding flow. | `WRONG_ENDPOINT` |
| **404** | No such order, or another tenant's. | — |

---

## `POST /transactions/{order_id}/retry/`

Returns the Checkout payload for an order that was created but never paid, so the
tenant can reopen Razorpay and finish.

```json
{
  "razorpay_order_id": "order_PxYz123",
  "amount": 118000,
  "amount_display": "1180.00",
  "currency": "INR",
  "razorpay_key_id": "rzp_test_xxx",
  "expires_at": "2026-08-26T10:42:00Z",
  "breakdown": { "...": "..." }
}
```

Identical in shape to what `POST /top-up/` returned originally — the frontend can feed
it to Checkout with the same code.

> **The same Razorpay order is reused, not a new one.** That keeps the order id stable,
> and every idempotency guard downstream is keyed on it: the credit ledger's
> `(entry_type, reference_id)`, and the transaction status check in both
> `verify-payment/` and the webhook. Issuing a fresh order for the same intent would
> risk crediting a tenant twice if both were eventually paid.

> **The expiry clock restarts** at 30 minutes. An order the tenant is actively paying
> should not expire mid-Checkout because the original window happened to be nearly up.

Same four response codes as `cancel/`. Nothing is settled — the order stays `pending`
until the payment actually clears.

---

## Checking that what was paid is what was owed

Both paths verify the amount before applying anything. **They do it differently, and
the reason is what each signature can actually prove.**

| | Webhook | `verify-payment/` |
|---|---|---|
| Signature covers | the whole raw request body | `order_id\|payment_id` only |
| Amount is | in the signed payload — free to check | not sent at all |
| Proves capture? | yes, it *is* a `payment.captured` event | **no** |
| So it | compares the payload amount | fetches the payment from Razorpay |

That second row is why the browser path makes an outbound call. A valid payment
signature proves Razorpay issued *a* payment against *that order*. It does not say how
much was paid, and it does not say whether the payment was **captured** rather than
merely **authorised** — authorised money is held, not taken, and can still be voided.
Granting credits against one would be giving away something nobody has paid for yet.

### What each outcome does

| Outcome | Result |
|---|---|
| Exact match | Applied. |
| **Underpaid** | **Nothing granted.** Status becomes `underpaid`. |
| Overpaid | **Applied**, and logged at ERROR so the difference can be refunded. |
| Amount unavailable | Applied on the signature alone — see the fail-open note below. |

**Zero tolerance.** Razorpay reports integer paise and we send integer paise, so a
genuine payment matches exactly. A tolerance would only ever mask a rounding bug in our
own pricing.

### Why `underpaid` is its own status

Neither of the existing ones is honest. `failed` says no money changed hands — money
did. `successful` would apply a purchase that was not paid for. So an underpaid order
sits in its own state: the payment id is recorded, nothing is granted, and a human
decides between refunding it and collecting the difference.

It does **not** block the next purchase — only `pending` does that — so a tenant is
never locked out by an order that is already settled as far as they are concerned.

> **What this actually protects against.** Underpayment is not reachable through
> Razorpay as orders are currently created: the amount is fixed server-side and partial
> payments are not enabled. So a mismatch means a pricing bug of ours, an order edited
> in the dashboard, or partial payments switched on without this code knowing. Worth
> catching — but the capture check is the more valuable half.

### Fail-open, deliberately

If the gateway cannot be reached, or the payload carries no amount, the purchase is
**applied** on the strength of the signature, and the reason is logged.

This matters most in **development**, where Razorpay cannot reach the host and no
webhook will ever arrive — `verify-payment/` is then the *only* thing that applies a
purchase, with nothing behind it. Refusing because an outbound call had a blip would
mean a tenant who paid gets nothing and no safety net puts it right.

In production the webhook is that safety net, and it checks the amount from its own
signed payload without needing any outbound call at all.

### Development setup

To exercise the real checks against Razorpay test credentials:

```
RAZORPAY_KEY_ID=<test key>
RAZORPAY_KEY_SECRET=<test secret>
PAYMENTS_ALLOW_UNSIGNED=False
```

`PAYMENTS_ALLOW_UNSIGNED=False` is what turns on both the signature verification and
the gateway fetch — with it `True`, orders are stubbed and neither runs.

`RAZORPAY_WEBHOOK_SECRET` is only needed where webhooks actually arrive. On a
development host they do not, so leaving it unset is fine; the system check reports it
as a warning under `DEBUG=True` and an error otherwise.

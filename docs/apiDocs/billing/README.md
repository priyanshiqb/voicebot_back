# Billing, Credits & Entitlements

Everything a tenant is charged for, and everything they are allowed to hold. One base
path covers all of it:

**Base path:** `/api/v1/billing/`

Deliberately not under `/api/v1/onboard/` — that prefix is the signup flow and
authenticates a `TempUser`, not an account. Everything here requires a normal user JWT
and is scoped to that user's tenant.

---

## Two things are metered, and they behave nothing alike

This is the single most important thing to understand before building against these
endpoints. Getting them confused is why credit systems end up with unanswerable
support tickets.

| | **Credits** | **Entitlements** |
|---|---|---|
| What they are | Talk time | Capacity — agents, seats, contacts, storage |
| Nature | A **flow**: spent and gone | A **gauge**: held, then released |
| Measured by | A balance | Counting live rows against a limit |
| Priced in | Credits | **Rupees**, recurring every cycle |
| Bought via | `POST /billing/top-up/` | `POST /billing/add-ons/` |
| Refused with | `402` insufficient credits | `402` quota exceeded |
| Touches the wallet | Always | **Never** |

**The test for which one you are looking at:** does deleting it give the resource
back? Delete a file and the GB returns → entitlement. Hang up a call and the minutes
do not → credit.

That is why storage is sold as capacity rather than credits per GB-month. Charging
credits for storage would make deleting a file a refund question with no good answer:
re-credit the unused days and a tenant can churn uploads to game the meter; do not,
and they paid for storage they released.

---

## What a minute costs

Three service tiers, priced **per plan**:

| Tier | What it is |
|---|---|
| `static_reel` | One-way playback. No conversation |
| `conversation_high_latency` | Standard conversational model |
| `conversation_low_latency` | Realtime model |

A tenant on Starter pays 5 credits a minute for a standard conversation; on
Professional it is 3. The same 90-second call therefore costs 10 credits or 6
depending on the plan — and **rounding is up, to whole minutes**, so a 90-second call
bills two minutes and a 4-second call bills one.

Direction is not priced. An inbound and an outbound minute on the same tier cost the
same.

### Where the price comes from

Not from the live catalogue. When a subscription activates, renews, or changes plan,
the plan's rate card is **copied onto the wallet**. Every call is priced from that
copy.

This matters for two reasons. Editing a plan's prices changes what *new and renewing*
subscribers pay, never what someone mid-cycle already agreed to. And pricing a call is
a single row read rather than a join.

`GET /billing/wallet/rates/` returns that stamped copy — what *this* tenant pays,
which is not necessarily what the pricing page currently advertises.

---

## Metering runs before blocking

Both credits and entitlements are **metered from day one but do not refuse anything
until switched on**, per tenant:

| Setting | Effect while `false` |
|---|---|
| `credits_enforced` | Calls are rated and deducted; a tenant who cannot afford one is logged, not refused |
| `entitlements_enforced` | Usage is counted; a tenant over their cap is logged, not refused |

Both default to `false`. That is not timidity — turning caps on for a tenant who is
*already* over one breaks a working account with no warning, and a wrong credit rate
would stop a paying customer's campaign before anyone noticed the rate was wrong.

`python manage.py audit_billing_readiness` reports which tenants are safe to enforce.

---

## A lapsed tenant is still a tenant

When a cycle ends unpaid the subscription expires and the wallet freezes — but the
relationship has not ended, and the API says so:

- Everything under `/billing/subscription/` and `/billing/wallet/` still answers. The
  tenant can see what they had, what happened to their credits, and what every plan
  would now cost.
- Coming back is **one payment on any plan**, at full price. There is no unused time
  left to prorate, so a cheaper plan is a purchase rather than a scheduled downgrade.
- Buying credits or capacity stays closed (`409 NO_ACTIVE_SUBSCRIPTION`) — both belong
  to a cycle, and there is not one running.
- **Purchased credits are frozen, not confiscated.** They are spendable again the
  moment a payment clears.

See [Subscription → When the cycle has ended](./subscription.md#when-the-cycle-has-ended).

---

## The endpoints

### [Wallet](./wallet.md) — what you have

| Method | Path |
|---|---|
| `GET` | `/billing/wallet/` |
| `GET` | `/billing/wallet/rates/` |
| `POST` | `/billing/wallet/quote/` |
| `GET` | `/billing/wallet/ledger/` |

### [Subscription](./subscription.md) — what plan you are on

| Method | Path |
|---|---|
| `GET` | `/billing/subscription/` |
| `GET` | `/billing/subscription/eligible-plans/` |
| `POST` | `/billing/subscription/renew/` |
| `POST` | `/billing/subscription/preview-change/` |
| `POST` | `/billing/subscription/change-plan/` |
| `DELETE` | `/billing/subscription/scheduled-change/` |

### [Purchases](./purchases.md) — buying more

| Method | Path |
|---|---|
| `POST` | `/billing/top-up/quote/` · `/billing/top-up/` |
| `POST` | `/billing/add-ons/quote/` · `/billing/add-ons/` |

---

## Payment is always two steps

Nothing that costs money is applied when you call the endpoint. Every purchase
endpoint returns a Razorpay order; the effect lands when the payment clears and the
webhook arrives.

```
POST /billing/top-up/     ->  { razorpay_order_id, amount, ... }   nothing granted yet
        |
   checkout at Razorpay
        |
   payment.captured webhook  ->  credits appear in the wallet
```

An abandoned checkout therefore costs nothing and grants nothing. Unpaid orders expire
after 30 minutes and are cleared by a sweep, so a closed tab does not lock the tenant
out of buying anything.

**One open order at a time.** A second purchase attempt while one is unpaid returns
`409 PENDING_ORDER_EXISTS` with the outstanding order id. Without that rule a tenant
can pay two upgrade orders and have only one applied — money taken, effect missing.

---

## Error codes

| Code | Status | Meaning |
|---|---|---|
| `NO_SUBSCRIPTION` | 409 | **Never subscribed.** Show a plan picker |
| `NO_ACTIVE_SUBSCRIPTION` | 409 | **Lapsed.** Only the purchase endpoints refuse this — the subscription endpoints answer 200 so the tenant can see what they had |
| `PENDING_ORDER_EXISTS` | 409 | An unpaid order is open |
| `PLAN_UNCHANGED` | 409 | That is the plan you are already on |
| `PLAN_NOT_FOUND` | 404 | No such active plan |
| `TOP_UPS_UNAVAILABLE` | 409 | This plan does not sell credits |
| `ADDON_UNAVAILABLE` | 409 | Not extendable, or beyond the plan's ceiling |
| `RENEWAL_WINDOW_NOT_OPEN` | 409 | Renewal opens 14 days before the cycle ends |
| `NO_SCHEDULED_CHANGE` | 409 | Nothing is scheduled to cancel |
| `RATE_NOT_CONFIGURED` | 409 | The plan has no price for that operation |
| `QUOTA_EXCEEDED` | **402** | At a plan limit |
| *(insufficient credits)* | **402** | Balance will not cover it |

**402 rather than 403 throughout.** 403 means "you may not do this"; these all mean
"you may, but you have used up what you bought" — a billing state the tenant can
resolve themselves. One status code for the whole "you need to pay for more" family.

# How Money Moves

Every price this system calculates, and every path a payment takes from Checkout to
the thing the tenant bought.

**This is the arithmetic and the plumbing.** For the data model and a narrative
walkthrough see
[Billing End-to-End Reference](../../implementation-plan-scratch/billing-end-to-end-flow.md);
for the endpoints themselves see [Top-Ups & Add-Ons](./purchases.md) and
[Subscription Lifecycle](./subscription.md).

| | |
| :--- | :--- |
| [1. Two meters](#1-two-meters-and-they-behave-nothing-alike) | Why credits and capacity are priced differently |
| [2. Proration](#2-proration-the-one-ratio-everything-uses) | The single ratio behind every mid-cycle price |
| [3. Plan changes](#3-plan-changes) | Full price plus bonus days, and why a downgrade sometimes costs money |
| [4. Top-ups](#4-credit-top-ups) | Flat, never prorated |
| [5. Add-ons](#5-capacity-add-ons) | Rent, per block, per cycle |
| [6. Renewals](#6-renewals) | Full price, plus whatever add-ons are still held |
| [7. A payment's journey](#7-a-payments-journey) | Order → Checkout → two confirmation paths → applied |
| [8. Verifying what was paid](#8-verifying-what-was-paid) | Amount and capture checks |
| [9. The guarantees](#9-the-guarantees) | Idempotency, frozen terms, never a refund |

---

## 1. Two meters, and they behave nothing alike

Every price below belongs to one or the other. Confusing them is where subscription
billing goes wrong.

| | **Credits** | **Capacity** |
| :--- | :--- | :--- |
| What | Talk time | Agents, seats, contacts, KBs, crawled pages, storage, campaigns |
| Behaves like | Petrol — spent and gone | A parking space — occupied, then released |
| Priced in | Credits per minute | **Rupees per billing cycle** |
| Bought via | `POST /billing/top-up/` | `POST /billing/add-ons/` |
| Prorated? | **No** | **Yes** |
| The test | Hang up — the minutes do *not* come back | Delete a file — the storage *does* |

That test is the whole distinction. A credit is worth the same whenever it is bought,
so charging less for one bought late in a cycle would make no sense. Capacity is
*rented for the remainder of the cycle*, so it must be.

---

## 2. Proration: the one ratio everything uses

```python
ratio = seconds_remaining_in_cycle / seconds_in_whole_cycle
```

`pricing.unused_ratio()`, quantised to four decimal places. Day 20 of 30 gives
`0.3333`; day 0 gives `1.0000`; a lapsed cycle gives `0`.

Every mid-cycle price is this ratio applied to a full-cycle figure. Nothing else in
the system prorates.

> **Wall-clock seconds, not whole days.** A cycle spanning a DST boundary is 719 or
> 721 hours, not 720, and the ratio reflects that. Counting days would drift.

---

## 3. Plan changes

### The formula

```
unused_value = (old_plan_net ÷ days_in_old_cycle) × days_remaining
             + (addon_recurring ÷ days_in_old_cycle) × days_remaining

charged      = new_plan_net + tax at the NEW plan's rate      ← full list price
bonus_days   = unused_value ÷ (new_plan_net ÷ days_in_new_cycle)
```

**The tenant pays the new plan's full price, and the value left on the old plan buys
extra days rather than a discount.** The cycle runs long by exactly what the unused
time was worth.

Three details, all deliberate:

- **The old price comes from `Subscription.plan_snapshot`, never the live plan.** The
  tenant is compensated for what they actually paid; the catalogue may have moved since.
- **Cycle length is measured, not assumed.** A monthly cycle is a *calendar* month —
  28, 29, 30 or 31 days. A flat 30 would misprice a February upgrade against a March one.
- **Both sides of the conversion are pre-tax.** Tax is charged on the new plan's full
  price separately; mixing a tax-inclusive figure in would buy days with money that was
  never the plan's to spend.

### Worked example — ₹12,000/month → ₹30,000/month, upgrading on day 10 of 30

```
old plan 12000 over 30 days      ->    400.00 per day
20 days unused                   ->   8000.40 of value
new plan 30000 over 30 days      ->   1000.00 per day
8000.40 ÷ 1000.00                ->      8.00 extra days

charged   30000.00 + 18% = 35400.00     the plan's list price
cycle     30 + 8 = 38 days
```

The same ₹8,000.40 against a ₹365,000 yearly plan also buys 8 days, because that plan
is likewise ₹1,000 a day. The conversion is always at the **destination** plan's rate.

### The charge never moves

```
 upgrade on    unused   bonus days      charged   cycle runs
      day 1  11600.40        11.99     35400.00       42.99d
      day 5   9999.60        10.00     35400.00       40.00d
     day 10   8000.40         8.00     35400.00       38.00d
     day 20   3999.60         4.00     35400.00       34.00d
     day 29    399.60         0.40     35400.00       30.40d
```

**Every upgrade invoices at the plan's list price.** Only the cycle length moves.

That is the point of the model. The previous approach discounted the new plan by the
unused value, which reduced the taxable value of one supply using time already taxed on
another, and produced an invoice for an amount matching no published price. Now each
supply is taxed at its full value and the tenant is made whole in service instead.

It is also **neutral on the day it happens** — whatever date they move, the total
service received is identical:

```
upgrade day  1:  paid 42000  ->   1d old (400)   + 41.60d new (41600) = 42000
upgrade day 10:  paid 42000  ->  10d old (4000)  + 38.00d new (38000) = 42000
upgrade day 30:  paid 42000  ->  30d old (12000) + 30.00d new (30000) = 42000
```

### Where the days come from at apply time

`bonus_days` is frozen onto `Transaction.amount_breakdown` when the order is created,
and `_apply_upgrade` reads it back from there. **It is never recomputed.** Days pass
between an order and its payment, and recomputing would quietly hand the tenant fewer
days than they paid for.

The extension is applied once, in `activate()`, as `end_date = start + cycle + bonus`.
Fractional days are kept to the second — a partial day is real money, and rounding it
away would keep the difference on every upgrade.

**It does not repeat at renewal.** `renew()` extends one cycle from the *extended* end
date, so the bonus is bought once with time the tenant had already paid for.

### Two edges

**Moving to a free plan buys no days.** Nothing costs anything per day there, so there
is no rate to convert at and the leftover value has nowhere to go. The alternative is an
infinite cycle.

**Downgrades split in two, and the second half is new.**

```
 downgrade on     charged   change type
        day 1        0.00   scheduled_change
       day 10        0.00   scheduled_change
       day 20    14160.00   paid_change      ← full price of the CHEAPER plan
       day 29    14160.00   paid_change
```

While the time left is worth more than the cheaper plan, nothing is charged and the move
waits for the cycle to end — unchanged.

**Late in the cycle it is a paid change, and now costs the cheaper plan's full price.**
A day-20 downgrade charges ₹14,160 where the old discount model charged ₹2,360. It is
value-neutral — ₹10,000 of unused time buys 25 bonus days, so the cycle runs 55 days and
the tenant receives ₹22,000 of service for ₹22,000 paid — but the **cash outlay is six
times higher**, on a transaction the customer thinks of as spending less.

⚠️ **This is the most surprising case in the system.** A UI that says "downgrade" and
then asks for ₹14,160 will generate tickets. Lead with the amount and the new end date,
not the word.

### Change type still follows the money

There is no ranking of plans anywhere in this system. `new_plan_net − unused_value`
still decides — but it is now only a **signal**, not the amount charged:

| Condition | `change_type` | What happens |
| :--- | :--- | :--- |
| Owes ≥ ₹1 | `paid_change` | Full list price charged; bonus days granted when it clears |
| Owes < ₹1 | `immediate_free` | Applied outright — below the gateway minimum, charging is impossible |
| Owes nothing | `scheduled_change` | Stored and applied at `end_date`. No charge, no refund, no bonus days |

That falls out correctly for monthly→yearly, yearly→monthly and same-price sidegrades
without anyone having to rank anything.

> **`taxable_difference` in the API response is now a misnomer.** It is kept under that
> name so existing clients do not break, but it is what the tenant *would* have owed
> under the old discount model — not what they are charged. The charge is `net` + `tax`.

### Never a refund

Money never flows backwards. Where a tenant is owed value they receive it as *time* —
bonus days on an upgrade, or the remainder of a cycle already paid for on a downgrade.

### Reproduce these numbers

```bash
PYTHONPATH=. python docs/implementation-plan-scratch/plan_change_pricing_probe.py
```

Runs inside a transaction it rolls back, so it is safe against a live database.

---

## 4. Credit top-ups

```
net     = credits × subscription.rules["cost_per_additional_credit"]
charged = net + tax at the plan's rate
```

**The unit price comes from the subscription's frozen rules, not the live plan** — the
same principle as everywhere else. A tenant tops up at the rate they signed for.

**Never prorated.** A credit is worth the same whenever it is bought, and once it
lands in the `purchased` bucket it never expires — so there is no partial cycle to
discount for.

Setting `cost_per_additional_credit` to zero **disables top-ups**, making an upgrade
the only route to more credits. A single order is capped at 1,000,000 credits — a
guard against a mistyped quantity, not a policy.

Purchased credits go to the `purchased` bucket, which rollover rules never touch.
Cash-bought credits are never confiscated at a cycle boundary; they are held while a
subscription is lapsed and become spendable again the moment a payment clears.

---

## 5. Capacity add-ons

```
wanted    = already_held + units
recurring = ceil(wanted ÷ addon_step) × price_per_additional      ← every cycle
added     = recurring − ceil(already_held ÷ addon_step) × price   ← the delta
charged   = added × ratio + tax                ← this cycle only, prorated
```

**The delta is priced as (price of the new total) − (price of what they already pay
for).** That is not the same as pricing the increment on its own, and the difference
matters: with a 10-unit block, buying 5 then 5 again would otherwise bill two whole
blocks for what a single purchase of 10 bills as one. Pricing the *total* means buying
in steps never costs more than buying the same amount at once.

**Two numbers, and they are different.** The tenant pays a prorated amount today and
the full recurring amount at every renewal until they remove the capacity.
`POST /billing/add-ons/quote/` returns both, because showing only one is how a
customer ends up surprised at renewal.

**Billed per whole block, rounded up.** On a 10 GB step, asking for 12 GB bills two
blocks. Billing a fraction would mean either giving away the remainder or invoicing a
number the tenant cannot reconcile against what they asked for.

`max_additional` caps how much extra may be bought. Setting each plan's cap below the
next plan's included amount is what stops a tenant assembling an enterprise footprint
at the cheapest plan's add-on prices.

**Campaigns cannot be bought as an add-on.** Prorated pricing misprices a count inside
a window: a tenant buying on day 20 of 30 would pay a third and receive the whole
allowance. See
[Campaign Lifecycle §8.3](../../implementation-plan-scratch/campaign-lifecycle-and-billing.md).

---

## 6. Renewals

```
net     = plan.net_base_price + Σ(add-on recurring charges)
charged = net + tax at the plan's rate
```

Full price, no proration — a renewal starts a whole cycle. Add-ons still held are
included, which is what makes them *rent* rather than a purchase.

**Renewals are manual.** Nothing is charged automatically; the tenant creates the
order through `POST /billing/subscription/renew/`.

**The window opens 14 days before the cycle ends** (`RENEWAL_WINDOW_DAYS`). Earlier
than that returns `409 RENEWAL_WINDOW_NOT_OPEN`. There is no upper bound — a lapsed
tenant can renew at any point afterwards, which is the same endpoint doing
double duty as the "come back" path.

> **Renewal is the one price not computed in `pricing.py`.** `RenewSubscriptionView`
> builds its `Quote` inline. It is simple enough that this is defensible, but it does
> mean a change to how tax or add-ons are applied has to be made in two places — worth
> knowing before editing either.

---

## 7. A payment's journey

```
1. POST .../quote/        optional — price it, create nothing
2. POST .../              Transaction written PENDING, then the Razorpay order
3. ── Razorpay Checkout opens in the browser ──
4a. POST /billing/verify-payment/     the browser confirms
4b. POST /onboard/webhook/            Razorpay's server confirms
5. payment_application.apply()        the effect
```

### Steps 4a and 4b race, deliberately

Either can arrive first, either can be the only one that arrives. Whichever wins marks
the transaction `SUCCESSFUL` under a row lock and applies the effect; the loser sees
the settled status and returns the same answer without applying anything twice.

**Both call `onboard.services.payment_application.apply()`.** The dispatch used to live
inside the webhook view while the browser path kept a near-copy — the shape defects
grow in, where a fix to one side leaves the other on a stale rule.

> **In development the webhook never arrives.** Razorpay cannot reach a dev host, so
> `verify-payment/` is the *only* thing that applies a purchase and there is no safety
> net behind it. That is why the gateway checks below fail *open*. In production the
> webhook is the safety net.

### The order is written before the gateway is called

`_create_order` persists the `Transaction` first, then asks Razorpay for an order id.
A gateway timeout therefore leaves a `PENDING` row to reconcile against, rather than a
payment nobody can attribute.

### One open order at a time

A tenant with an unpaid order gets `409 PENDING_ORDER_EXISTS` on any new purchase, with
the blocking `razorpay_order_id`. Without this a tenant could pay two upgrade orders
and have only one applied — money taken, effect missing.

The two exits are `POST /billing/transactions/{order_id}/cancel/` and
`.../retry/`. Orders also expire after 30 minutes and are swept on the next purchase
attempt.

---

## 8. Verifying what was paid

Both confirmation paths check the amount. **They do it differently, because their
signatures prove different things.**

| | Webhook | `verify-payment/` |
| :--- | :--- | :--- |
| Signature covers | the whole raw request body | `order_id\|payment_id` only |
| Amount is | in the signed payload — free to check | not sent at all |
| Proves capture? | yes, it *is* a `payment.captured` event | **no** |
| So it | compares the payload amount | fetches the payment from Razorpay |

That last row is the more valuable half. A valid payment signature proves Razorpay
issued *a* payment against *that order*. It does not prove the payment was **captured**
rather than merely **authorised** — authorised money is held, not taken, and can still
be voided. Granting credits against one gives away something nobody has paid for yet.

| Outcome | Result |
| :--- | :--- |
| Exact match | Applied |
| **Underpaid** | **Nothing granted.** Status becomes `underpaid` |
| Overpaid | Applied, logged at ERROR so the difference can be refunded |
| Amount unavailable | Applied on the signature alone, logged |

**Zero tolerance.** Razorpay reports integer paise and we send integer paise, so a
genuine payment matches exactly. A tolerance would only mask a rounding bug of ours.

**`underpaid` is its own status** because neither existing one is honest: `failed` says
no money changed hands, `successful` would apply a purchase that was not paid for. The
payment id is recorded, nothing is granted, and a human decides between refunding and
collecting the difference. It does not block the next purchase — only `pending` does.

> **What this actually protects against.** Underpayment is not reachable through
> Razorpay as orders are currently created: the amount is fixed server-side and partial
> payments are not enabled. So a mismatch means a pricing bug of ours, an order edited
> in the dashboard, or partial payments switched on without this code knowing.

---

## 9. The guarantees

### Idempotency, three layers

| Layer | Mechanism | Catches |
| :--- | :--- | :--- |
| Event | `RazorpayWebhookLog.event_id` unique | A redelivered webhook |
| Transaction | Row lock + status check | Both confirmation paths racing |
| Ledger | `CreditLedger` unique `(entry_type, reference_id)` | Anything past the first two |

Calling `verify-payment/` five times in a row credits exactly once. There is a test
that says so.

### Frozen terms

A tenant's commercial terms are stamped at subscribe and re-stamped at each renewal:

| Frozen onto | What |
| :--- | :--- |
| `Subscription.plan_snapshot` | Price, credit rules, entitlement allowances, reset periods |
| `Wallet.rate_snapshot` | The per-minute rate card |
| `TenantEntitlement` | The add-on price paid |

**Editing a plan changes what new and renewing tenants get.** Nobody's bill changes
under them mid-cycle, and nobody's limits shrink overnight.

### Money never flows backwards

No path in this system issues a refund. Where a tenant is owed value, they receive it
as *time already paid for* (a scheduled downgrade) or as *credit against a new charge*
(a plan change), never as money returned.

---

## Where things live

| | |
| :--- | :--- |
| `onboard/services/pricing.py` | Every formula on this page |
| `onboard/services/payment_application.py` | The effect of a paid transaction; the amount check |
| `onboard/services/payments.py` | Gateway calls: create order, verify signature, fetch payment |
| `onboard/services/subscriptions.py` | `activate`, `renew`, `apply_plan_change`, `schedule_plan_change` |
| `onboard/services/entitlements.py` | Capacity limits and live usage |
| `wallet/services/rating.py` | What a call costs in credits |
| `wallet/services/wallet.py` | Grants, consumption, the ledger |

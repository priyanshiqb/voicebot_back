# Subscription API Documentation

The plan a tenant is on, and every way it can change.

**Base path:** `/api/v1/billing/subscription/`

Four rules govern everything here:

- **Exactly one active subscription per tenant**, enforced by a database constraint.
  Whatever else happens, "the current plan" is never ambiguous.
- **Upgrades apply on payment. Downgrades apply at the end of the cycle.** Which one
  something is depends entirely on whether money is owed — there is no tier ladder
  anywhere in the system.
- **Nothing is ever refunded.** A tenant keeps what they paid for until the cycle ends.
- **Renewals are manual.** There is no auto-pay mandate; a tenant who does not renew
  lapses.
- **A lapsed tenant can still see everything and come back on any plan.** The
  relationship did not end with the cycle — see [When the cycle has
  ended](#when-the-cycle-has-ended).

> **Two "no subscription" errors, and they mean different things.**
> `NO_SUBSCRIPTION` is *never subscribed* — show a plan picker. `NO_ACTIVE_SUBSCRIPTION`
> is *lapsed* — show what they had and what it costs to return. Every endpoint on this
> page answers the second case with a 200, because a 409 cannot explain a block.

## Endpoints Summary

- `GET /api/v1/billing/subscription/` — Current plan, cycle, credits, capacity
- `GET /api/v1/billing/subscription/eligible-plans/` — Every plan, priced for *this* tenant
- `POST /api/v1/billing/subscription/renew/` — Order the next cycle
- `POST /api/v1/billing/subscription/preview-change/` — Cost and credit impact of a move
- `POST /api/v1/billing/subscription/change-plan/` — Commit to the move
- `DELETE /api/v1/billing/subscription/scheduled-change/` — Drop a pending downgrade

---

## Upgrade or downgrade is decided by arithmetic

`preview-change` and `change-plan` both compute the prorated difference and branch on
its sign:

```
credit for unused cycle  =  current plan net price  x  fraction of cycle remaining
                         +  unused portion of any add-ons already paid for

amount due               =  new plan net price  -  that credit,  taxed at the NEW plan's rate
```

| Result | `change_type` | What happens |
|---|---|---|
| Owes money | `paid_change` | Order created; applied when it clears |
| Owes nothing | `scheduled_change` | Stored, applied at `end_date`. No charge, no refund |
| Owes under ₹1 | `immediate_free` | Applied now. Below what a gateway will accept |

This falls out correctly for monthly↔yearly and same-price sidegrades without anyone
ranking the plans.

**Tax is applied at the new plan's rate on the taxable difference**, not on a figure
that already includes the old plan's tax.

**Prices for the outgoing plan come from the frozen snapshot**, never the live
catalogue — the tenant is owed credit for what they actually paid.

---

## `GET /billing/subscription/`

```json
{
  "data": {
    "id": "…",
    "status": "active",
    "plan": { "id": "3f2a…", "code": "starter_inr", "name": "Starter INR", "billing_cycle": "monthly" },
    "start_date": "2026-08-01T00:00:00Z",
    "end_date": "2026-09-01T00:00:00Z",
    "days_remaining": 8,
    "renewable_now": true,
    "scheduled_change": null,
    "credits": { "balance": 1030, "allocated": 980, "purchased": 50, "is_frozen": false, "…": "…" },
    "entitlements": [
      { "resource": "agent", "included": 3, "additional": 2, "limit": 5,
        "used": 4, "remaining": 1, "recurring_charge": "598.00" }
    ],
    "addons_recurring_net": "598.00"
  }
}
```

`entitlements[].used` is counted live from the rows that *are* the resource — there is
no counter to drift, and deleting an agent frees the allowance immediately.

`addons_recurring_net` is what purchased capacity adds to **every** future renewal,
pre-tax.

`days_remaining` floors at **0** — a lapsed cycle reports 0, never a negative number.

**Returned for a lapsed subscription too**, with `status: "expired"`. That is
deliberate: this is the endpoint a dashboard uses to explain why calls stopped, and it
cannot do that if it refuses to answer. Only a tenant who has *never* subscribed gets
`409 NO_SUBSCRIPTION`.

---

## `GET /billing/subscription/eligible-plans/`

Every active plan, already priced for this tenant. Saves calling `preview-change` once
per plan to build a pricing page.

```json
{
  "data": { "plans": [
    { "id": "3f2a…", "code": "starter_inr", "name": "Starter INR", "is_current": true,
      "change_type": "current", "amount_due": "0.00", "allocated_credits": 1000 },
    { "id": "7b1c…", "code": "professional_inr", "name": "Professional INR", "is_current": false,
      "change_type": "paid_change", "amount_due": "2792.03",
      "effective_at": "2026-08-24T09:14:22Z", "allocated_credits": 3500 },
    { "id": "9d4e…", "code": "free_plan", "change_type": "scheduled_change", "amount_due": "0.00",
      "effective_at": "2026-09-01T00:00:00Z", "allocated_credits": 100 }
  ]}
}
```

A plan that cannot be priced is still listed, with `change_type: "unavailable"` and a
`reason` — the tenant should see it exists.

> **`id` is what `preview-change/` and `change-plan/` take.** Pass it straight through
> as `plan_id`; there is no need to look the plan up by `code` first. Every response
> that names a plan carries its id for the same reason — the current subscription's
> `plan`, a pending `scheduled_change`, and what `change-plan/` echoes back.

**Available to a lapsed tenant**, where every plan is priced at its full price — see
below.

---

## `POST /billing/subscription/preview-change/`

**Zero side effects.** Nothing is created, charged or scheduled.

**Request:** `{ "plan_id": "<uuid>" }`

```json
{
  "data": {
    "money": {
      "change_type": "paid_change",
      "effective_at": "2026-08-24T09:14:22Z",
      "from_plan": "starter_inr", "to_plan": "professional_inr",
      "current_plan_net": "999.00", "new_plan_net": "2699.10",
      "unused_ratio": "0.3333", "credit_from_current": "332.97",
      "credit_from_addons": "0.00", "bonus_value": "332.97",
      "bonus_days": "3.70", "new_plan_cycle_days": "30",
      "net": "2699.10", "tax_percentage": "18.00", "tax": "485.84",
      "total": "3184.94", "taxable_difference": "2366.13",
      "reactivation": "False"
    },
    "credits": {
      "current_balance": 1030, "allocated": 980, "purchased": 50,
      "carried_forward": 980, "forfeited": 0,
      "new_plan_allocation": 3500, "balance_after_change": 4530,
      "warnings": []
    }
  }
}
```

> **An upgrade charges the new plan's full list price and grants extra days.** The
> unused value of the old plan (`bonus_value`) is converted at the new plan's daily rate
> into `bonus_days`, which extend the cycle — it is not deducted from the price. So
> `total` is always the plan's published price, and the customer's compensation shows up
> as a later end date.
>
> **`taxable_difference` is a misnomer, kept for compatibility.** It is what would have
> been owed under the old discount model, and now only decides upgrade vs scheduled
> downgrade. The amount charged is `total`.
>
> **Show `bonus_days` in the confirmation.** "₹35,400 today, and your cycle runs to
> 3 Oct instead of 25 Sep" is the whole value proposition; without it the customer sees
> only a full-price charge for a plan they were already partway through paying for.

**`credits.warnings` is what the confirmation dialog must show.** A tenant about to
lose credits has to be told before they click, not after:

> `"Your 980 remaining plan credits do not carry over on a downgrade."`

`reactivation` is `"True"` when the cycle has already ended. Every value under `money`
is a string — the whole block is stored verbatim on the transaction, so it is
serialised the same way it is persisted.

---

## `POST /billing/subscription/change-plan/`

Recomputes the quote server-side — a client-supplied amount is never trusted — then
takes one of three paths.

**Request:** `{ "plan_id": "<uuid>" }`

**Paid change** returns a Razorpay order, same envelope as any other purchase. The plan
does not change until the payment clears.

**Scheduled change** stores the intent and returns immediately:

```json
{ "data": { "change_type": "scheduled_change",
            "plan_id": "9d4e…", "plan_code": "free_plan",
            "effective_at": "2026-09-01T00:00:00Z", "charged": false },
  "message": "Downgrade to Free Tier scheduled for 01 Sep 2026. Nothing changes until then." }
```

Nothing about the current plan changes in the meantime — no second subscription is
created, so the one-active constraint is never approached.

**An upgrade clears any pending downgrade.** Otherwise one scheduled weeks ago fires
later and silently undoes a change the tenant has just paid for.

**Choosing the plan you are already on** returns `409 PLAN_UNCHANGED`. For a lapsed
tenant the message points at `renew/`, which extends the existing subscription rather
than opening a second one.

---

## `DELETE /billing/subscription/scheduled-change/`

Drops a pending downgrade; the current plan simply continues.

Returns `409 NO_SCHEDULED_CHANGE` if nothing is scheduled.

---

## `POST /billing/subscription/renew/`

Creates an order for the next cycle — **the plan's price plus any add-ons still held.**

Opens **14 days** before `end_date`. Earlier returns `409 RENEWAL_WINDOW_NOT_OPEN` with
the days remaining. **An already-expired subscription can always be renewed** — the
window is a floor, not a ceiling, and being late must never be a reason we cannot take
the money.

### What renewal does when the payment clears

1. Forfeits whatever the plan's rollover rules do not carry
2. Extends `end_date` by one cycle — **the same subscription row**, not a new one
3. Re-freezes the plan snapshot from the live plan, because a renewal *is* a future
   cycle and this is where a price change legitimately reaches an existing customer
4. Re-stamps the wallet's rate card
5. Grants the new cycle's credits
6. Unfreezes the wallet if it had lapsed

A renewal paid long after expiry starts **now**, not back-dated — nobody pays for a
cycle they could not use.

---

## When the cycle has ended

**See everything, buy only a plan.** A lapsed tenant needs to understand why they are
blocked and what it costs to return. Renting capacity for a cycle that is not running
does not.

| | Active, >14 days left | Active, ≤14 days left | **Expired** |
|---|---|---|---|
| `GET /subscription/` | ✅ | ✅ | ✅ |
| `GET /eligible-plans/` | ✅ prorated | ✅ prorated | ✅ **full price** |
| `POST /preview-change/` | ✅ | ✅ | ✅ |
| `POST /change-plan/` | ✅ prorated | ✅ prorated | ✅ **full price** |
| `POST /renew/` | ❌ window not open | ✅ | ✅ **extends the same row** |
| `POST /top-up/`, `/add-ons/` | ✅ | ✅ | ❌ `NO_ACTIVE_SUBSCRIPTION` |

### Coming back is one payment, on any plan

Before this, returning on a bigger plan meant renewing the old one **and then**
upgrading — two payments for one outcome — and `GET /subscription/` returned 409, so
the dashboard could not even say why.

Once a cycle is over there is no unused time to credit and no cycle to schedule
against, so "upgrade" and "downgrade" stop being meaningful:

| | Mid-cycle | After the cycle ends |
|---|---|---|
| Credit for unused time | Prorated | **0.00** |
| A cheaper plan | `scheduled_change`, ₹0 | `paid_change`, **full price** |
| The free plan | `scheduled_change` at `end_date` | `immediate_free`, applied now |
| `effective_at` | Now, or `end_date` | **Now** — never a date in the past |
| `money.reactivation` | `"False"` | `"True"` |

A subscription past its `end_date` counts as lapsed **even if the expiry sweep has not
reached it yet** — the date decides, not just the status. Otherwise a tenant would be
credited for time they no longer have.

### What reactivating does

Same plan → use `renew/`. It extends the existing subscription; no second row is
created, and the history of a customer who came and went twice stays readable.

Different plan → `change-plan/`, which closes the lapsed subscription with
`superseded_by` pointing at the new one, stamps the new plan's rate card, grants its
allocation and unfreezes the wallet.

**Purchased credits survive all of it** — the expiry, the freeze, the plan change and
the fresh allocation.

---

## Expiry

A cycle that ends unpaid is expired by an hourly sweep:

| | |
|---|---|
| Subscription | `status: expired` |
| Plan credits | Forfeited (`expiry_forfeit`) |
| **Purchased credits** | **Kept.** Frozen, and spendable again the moment a renewal clears |
| Wallet | Frozen — calls refused |

Confiscating cash-bought credits when a subscription lapses would be taking money and
giving nothing back, so expiry burns only the allocated bucket.

A plan with `allow_use_after_expiry` skips all of it — the subscription lapses but the
wallet stays live and the whole balance stays spendable.

### Order of operations

The sweep applies a **scheduled change first, expiry second**, in one pass. Run as two
independent jobs they race at exactly the moment both fire, and a tenant who scheduled
a downgrade for the end of their cycle gets expired instead — losing access they paid
to keep.

# Plan v2 — Minutes-Driven Plans

The customer says how many minutes they need. Everything else — which plan, what it
costs in rupees, how many credits land in the wallet — is derived from that one number.

**Status:** 🟢 Built — all phases, including the removals · **Supersedes:** the static `base_price` + `allocated_credits`
model ·
**Implementation plan:** [plan-v2-implementation-plan.md](../implementation-plan-scratch/plan-v2-implementation-plan.md)

---

## Contents

1. [What is wrong with plans today](#1-what-is-wrong-with-plans-today)
2. [The shape of the change](#2-the-shape-of-the-change)
3. [Why money and credits live on the same row](#3-why-money-and-credits-live-on-the-same-row)
4. [The band — min_minutes and max_minutes](#4-the-band--min_minutes-and-max_minutes)
5. [The basis tier](#5-the-basis-tier)
6. [Worked example, end to end](#6-worked-example-end-to-end)
7. [Where the chosen minutes live](#7-where-the-chosen-minutes-live)
8. [What this does to the rest of billing](#8-what-this-does-to-the-rest-of-billing)
9. [The static fields, and which of them go](#9-the-static-fields-and-which-of-them-go)
10. [Completeness rules for a v2 plan](#10-completeness-rules-for-a-v2-plan)
11. [Backward compatibility](#11-backward-compatibility)
12. [Edge cases](#12-edge-cases)
13. [Deliberately out of scope](#13-deliberately-out-of-scope)

---

## 1. What is wrong with plans today

A plan today is a **fixed bundle**: `base_price` rupees for `allocated_credits` credits,
take it or leave it. Both numbers are typed into the catalogue by hand and neither moves.

That forces the customer to shop in the wrong currency. What a customer knows is *"we run
about 1,200 minutes of calls a month."* What the pricing page offers is *"3,500 credits
for ₹2,999."* Translating between the two requires knowing the rate card, and the rate
card is per-tier, so the honest answer is *"it depends how you call."*

Three consequences, all of them ours to fix:

| | Today |
| :--- | :--- |
| A customer needing 1,200 minutes | Has to work out which bundle covers it, or guess |
| A customer needing 1,900 minutes | Buys the 2,000-credit plan and wastes it, or buys the smaller one and tops up at a premium |
| Commercial wanting a new price point | Files a request for a new plan row, because a plan is one price |

**The fix is to make minutes the thing the customer chooses**, and to derive the plan, the
price and the credits from it. A plan stops being a point on a price list and becomes a
**band** — a range of usage with its own per-minute rates.

---

## 2. The shape of the change

Three new fields on `onboard.Plan`, **one new field on `wallet.CreditRate`**, and one new
idea: a plan is priced **for a quantity**, not on its own.

```
                       customer types 1,200 minutes
                                    |
                                    v
                   +--------------------------------+
                   |  which ACTIVE plan's band      |    bands never overlap,
                   |  contains 1,200?               |    so this has one answer
                   +----------------+---------------+
                                    v
                              Plan "Growth"
                          band 501-2,000
                          credit_basis = high latency
                                    |
                                    v
                   CreditRate(Growth, high_latency)
                   price_per_minute_inr = 1.8000
                   credits_per_unit     = 4
                                    |
                  +-----------------+------------------+
                  v                                    v
        1,200 x Rs 1.80 = Rs 2,160 net        1,200 x 4 credits/min
        + 18% tax       = Rs 2,548.80 charged = 4,800 credits granted
```

### On `onboard.Plan`

| New field | Type | Meaning |
| :--- | :--- | :--- |
| `min_minutes` | int, nullable | Lowest quantity this plan sells, **inclusive**. Null = no floor |
| `max_minutes` | int, nullable | Highest quantity this plan sells, **inclusive**. Null = no ceiling |
| `credit_basis` | choice, nullable | Which `CreditRate` operation the plan is **sold at** — the row both the price and the grant are read from |

### On `wallet.CreditRate`

| New field | Type | Meaning |
| :--- | :--- | :--- |
| `price_per_minute_inr` | decimal(10,4), nullable | Pre-discount, pre-tax rupees per minute of *this tier*, on *this plan* |

Every rate row now answers the whole question about one tier: **what a minute of it costs
in money, and what it costs in credits.** Those were always two views of a single
commercial decision; this is the first time they sit in the same place.

Four decimal places, deliberately. Two is not enough: at ₹1.80 vs ₹1.85 the smallest step
commercial can express on a 2,000-minute plan is ₹100 of monthly revenue. The *total* is
still quantized to paise before it reaches the gateway.

### On `onboard.Subscription`

| New field | Type | Meaning |
| :--- | :--- | :--- |
| `selected_minutes` | int, nullable | The quantity this subscription committed to |
| `allocated_credits` | int | What this cycle granted. **Moved here from `Plan`**, where a static number can no longer answer the question |

**`Plan.allocated_credits` goes away and is replaced by a method**, `credits_for(minutes)`.
The grant is no longer a property of the plan — it is a property of a *sale*, and §7
explains why that split matters more than it looks.

### The complete `Plan` field list

Every field on the model once v2 ships, with what happens to it later. **Phase** marks a
field that changes after the initial release — see §9.

| Field | Type | v2 | Phase | Meaning |
| :--- | :--- | :--: | :--: | :--- |
| **Selection** | | | | |
| `min_minutes` | int, null | 🆕 | | Lowest quantity sold, inclusive. Null = no floor |
| `max_minutes` | int, null | 🆕 | | Highest quantity sold, inclusive. Null = no ceiling |
| `credit_basis` | choice(3), null | 🆕 | | Which `CreditRate` row the plan is sold at — price *and* grant come from it |
| **Identity** | | | | |
| `code` | char(50), unique | | | Machine-readable identifier |
| `name` | char(100), unique | | | Display name |
| `description` | text, null | | | Marketing copy |
| **Price** | | | | |
| `base_price` | — | ❌ | | **Removed.** Now `price_components(minutes)`; the catalogue properties report the plan's entry price |
| `discount_percentage` | decimal(5,2) | | | Applied to the computed net |
| `tax_percentage` | decimal(5,2) | | | Applied after discount |
| `currency` | char(3) | | | INR only |
| `billing_cycle` | char(20) | | | monthly / yearly. Each cycle has its own ladder |
| **Credits** | | | | |
| `allocated_credits` | — | ❌ | | **Removed.** Now `credits_for(minutes)`, and a column on `Subscription` |
| `cost_per_additional_credit` | decimal(10,2) | | | Top-up price. **Kept** — top-ups are bought in credits, not minutes. 0 disables |
| `max_rollover_credits` | int, null | | | Absolute cap. **Superseded by `max_rollover_ratio`**, read first; kept as the fallback for older snapshots |
| `max_rollover_ratio` | decimal(5,2), null | 🆕 | | Cap as a multiple of the cycle's own grant. `1.00` = one cycle's worth |
| `low_balance_threshold` | int | | | Absolute alert level. **Superseded by `low_balance_percent`**, read first |
| `low_balance_percent` | int, null | 🆕 | | Alert at this percentage of the cycle's grant |
| `allow_rollover_on_renewal` | bool | | | Unchanged |
| `allow_rollover_on_upgrade` | bool | | | Unchanged |
| `allow_rollover_on_downgrade` | bool | | | Unchanged |
| `allow_use_after_expiry` | bool | | | Unchanged |
| `renews_without_payment` | bool | | | Zero-cost cycle rolls itself. Separates a trial from pay-as-you-go |
| **Presentation & lifecycle** | | | | |
| `plan_type` | char(20) | | | paid / free / pay_as_you_go |
| `sort_order` | int | | | Pricing-page order |
| `is_most_popular` | bool | | | Badge |
| `status` | char(16) | | | draft / active / retired. Drives `is_active` |
| `is_active` | bool | | | Derived from `status` in `save()`. From `BaseModelCore` |

🆕 new · ⚠️ still present, superseded later · ❌ removed

**Methods that replace the removed columns** — the entire legacy/v2 split lives here:

| Method | Returns |
| :--- | :--- |
| `is_minutes_based` | `credit_basis` set **and** its open rate row has a rupee price |
| `basis_rate()` | The open `CreditRate` for `credit_basis`, or `None` |
| `contains_minutes(n)` | Whether `n` falls in this plan's band |
| `is_fixed_quantity` | `min_minutes == max_minutes`, both non-null — a pack, not a range. Derived, never stored |
| `credits_for(minutes)` | v2: `minutes × basis.credits_per_unit`. Legacy: `allocated_credits` |
| `price_components(minutes)` | `(net, discount, tax, total)`. v2: `minutes × basis.price_per_minute_inr`. Legacy: `base_price` |

**Constraints:** the existing `ck_plan_free_renewal_only_when_zero_cost` (re-expressed in
Phase 9), plus the new `ck_plan_minutes_band_valid` — `min ≤ max` when both are set.
Everything else needs the rate card or the rest of the ladder, so it is a readiness check
rather than a database constraint (§10).

---

## 3. Why money and credits live on the same row

This was the first version's mistake and it is worth being explicit about, because the
wrong shape looks perfectly reasonable.

**The wrong shape** — a single `price_per_minute` on the plan, credits from the rate card:

```
Plan.price_per_minute = 1.80        Plan.credit_basis = high latency
                                    CreditRate(high) = 4 credits/min

  someone switches credit_basis to low latency (8 credits/min)
        -> credits granted DOUBLES
        -> the price does not move
        -> nothing detects it
```

The two halves of one decision sat in two tables, so they could be changed independently,
and there was nothing to compare one against the other. Putting `price_per_minute_inr` on
the rate row makes them move together by construction — switching the basis switches
both, because both come from the row the basis names.

### It also gets version history for free

`CreditRate` is already **supersede-don't-edit**: re-pricing closes the current row
(`effective_to = now`) and opens a new one, and a partial unique constraint permits only
one open row per (plan, operation). Putting the rupee price there means **every money
price change is now versioned**, with a date and a reason.

`Plan.base_price` never had any of that. Editing it rewrites the catalogue silently, with
no record of what the price was yesterday.

### And it makes a real consistency rule checkable

```
rupees per credit  =  price_per_minute_inr  /  credits_per_unit
```

When that ratio is the same across a plan's three tiers, the plan sells credits at one
price and the tier is purely a matter of how the customer chooses to spend them. When it
differs, the plan is quietly selling credits at three prices, and which one a customer
gets depends on a basis they never chose:

| Growth | credits/min | ₹/min | ₹/credit |
| :--- | ---: | ---: | ---: |
| Static reel | 2 | 0.90 | **0.45** |
| Conversation — standard | 4 | 1.80 | **0.45** |
| Conversation — realtime | 8 | 3.60 | **0.45** |

That is a coherent plan. A row at ₹0.30/credit while the others sit at ₹0.45 is not
necessarily wrong — commercial may want realtime subsidised — but it is never an accident
worth leaving unreported, so it becomes a readiness **warning** (§10).

### What the non-basis rows are for

Only the basis row prices the sale, so it is fair to ask what the other two rupee prices
do. Three things:

- **The pricing page.** "Your plan is sold at ₹1.80/min standard; realtime runs ₹3.60/min"
  is the sentence a customer needs to size their own usage.
- **The consistency check** above, which needs at least two rows to say anything.
- **Switching the basis without re-pricing.** The number is already there and already
  reviewed, so moving a plan from standard to realtime is one field, not a re-quote.

They are never read at purchase, and never on the call path.

---

## 4. The band — min_minutes and max_minutes

Both bounds are inclusive, and either may be null:

| `min_minutes` | `max_minutes` | Sells |
| ---: | ---: | :--- |
| `null` | `500` | Anything up to and including 500 minutes |
| `501` | `2000` | 501 to 2,000 minutes |
| `2001` | `null` | 2,001 minutes and up, with no ceiling |
| `null` | `null` | Any quantity at all — legal, and only sane if it is the only v2 plan |

This is exactly the rule as stated: *a null minimum means anything below the maximum, a
null maximum means anything above the minimum.*

**One qualification: "no maximum" means no maximum *this plan* imposes.** Every quantity
is still bounded by a system ceiling of 1,000,000 minutes per cycle. Above that a number
is a typo rather than an enterprise deal, and letting one through would carry it into a
price the `Transaction.amount` column cannot hold — a 500 in the middle of a checkout
instead of a sentence the customer can read. Genuinely larger arrangements go through
`onboard_custom_deal`, which is what it is for.

### Bands must not overlap, and ranged bands must not leave gaps

Auto-selection only works if `minutes -> plan` is a **function**. Two plans claiming 1,200
minutes gives the frontend a coin-flip between two prices, and a customer who reloads the
page can be quoted a different number for the same request.

So, checked across every `ACTIVE` v2 plan sharing a `billing_cycle` and `currency`:

| Rule | Severity | Why |
| :--- | :--- | :--- |
| No two bands may overlap | **Blocking** | Ambiguous selection; two prices for one request |
| No gap between adjacent **ranged** bands | **Blocking** | A customer needing 501 minutes would be silently rounded up and overcharged |
| Some plan should be open below (`min_minutes = null`) | Warning | Otherwise the smallest customers cannot buy at all |
| Some plan should be open above (`max_minutes = null`) | Warning | Otherwise the largest customers cannot buy at all |

Both blocking rules are cross-row, so they cannot be database constraints. They live in the
activation gate alongside the existing completeness checks — a plan that would overlap or
strand a gap **cannot be activated**, which is the same door every other silent
misconfiguration is already stopped at.

Monthly and yearly ladders are checked independently. A yearly plan is a separate plan with
its own band, and 12,000 yearly minutes has nothing to say about a monthly ladder.

### Fixed-quantity plans: a band of width one

A plan that sells **exactly 5,000 minutes** — a pack, not a range — needs no new
machinery. It is a band whose bounds are equal:

```
min_minutes = 5000
max_minutes = 5000     ->  contains_minutes(5000) is the only true answer
                           price   = 5000 x basis.price_per_minute_inr
                           credits = 5000 x basis.credits_per_unit
```

**`is_minutes_based` is `true` for such a plan**, and that is the point: the price is still
₹/min × a quantity, the grant still comes from the basis row, and every consistency check
in §3 and §10 still applies. Selling a fixed quantity is a statement about *which*
quantities are on offer, not about how they are priced.

A derived property, `is_fixed_quantity` (`min_minutes == max_minutes`, both non-null), lets
the frontend render a card instead of a slider. It is derived rather than stored, because a
flag that can disagree with the bounds is a flag that eventually will.

### A quantity that was named is never re-resolved

`resolve_minutes()` is a convenience for a caller who names a *destination* without a
quantity — "move me to Scale", meaning "the same usage, over there". It must never be
applied to a quantity somebody actually named.

The failure is expensive and silent. A tenant orders an upgrade to 2,500 minutes; before
their payment clears, commercial narrows that plan's band to 5,000-9,000. If the paid
quantity is resolved into the band at the moment it is applied, the tenant is handed
5,000 minutes for a 2,500-minute payment, and nothing anywhere reports it.

So: **an explicit quantity wins, always.** Resolution happens once, at quote time, and
the result is frozen onto the order. A scheduled downgrade follows the same rule, because
it stored its quantity weeks before it fires.

This also means a subscription may legitimately hold a quantity outside its plan's
current band — a negotiated deal, or a band edited after the sale. The band is a
published offer, not a constraint on what has already been agreed.

### Selection when the ladder is made of packs

A ladder of packs — 1,000 / 5,000 / 10,000 — has gaps by construction, and exact
containment would refuse a customer who asks for 1,200. So selection has two steps:

```
1  exact containment    the one band that contains N        <- ranged ladders end here
2  round up             the smallest plan whose max >= N    <- packs
```

Step 2 only ever reaches a pack, because a gap between two ranged bands is blocked — so
step 1 cannot fail inside the continuous part of a ladder. One rule serves both shapes with
no setting to configure.

**What was asked for and what was bought are then different numbers**, and both matter:

| | Value | Where it lives |
| :--- | :--- | :--- |
| Requested | 1,200 | `Transaction.amount_breakdown["requested_minutes"]` |
| Bought | 5,000 | `Subscription.selected_minutes` — drives price and credits |

The API returns both so the page can say *"the 5,000-minute pack covers your 1,200"* rather
than silently quoting a number the customer did not type.

### Mixing packs and ranges in one ladder

Legal, and the rules above handle it — a ranged ladder with a couple of large packs above it
works, because the packs sit in territory no ranged band claims.

What is **refused** is a pack that overlaps a ranged band: a 5,000-minute pack cannot
coexist with a `2001–null` band, because 5,000 then has two answers. Either cap the ranged
band below the pack, or sell the pack off-catalogue — `onboard_custom_deal` exists for
exactly the enterprise arrangement that does not want to be on a pricing page.

### Bands are where volume pricing lives

Each band's basis row sets its own ₹/min, so moving up the ladder gets cheaper per minute
without anybody writing a discount rule:

| Plan | Band | basis ₹/min | 500 min | 1,200 min | 2,500 min |
| :--- | :--- | ---: | ---: | ---: | ---: |
| Starter | –500 | 2.75 | ₹1,375 | — | — |
| Growth | 501–2,000 | 1.80 | — | ₹2,160 | — |
| Scale | 2,001– | 1.20 | — | — | ₹3,000 |

> **Every boundary is a cliff, and that is what a volume discount is.** At 500 minutes
> Starter charges ₹1,375; at 501 minutes Growth charges ₹901.80 — *less money for more
> minutes*. This is not a misconfiguration to be warned about: avoiding it would require
> `rate_next >= rate_prev`, a ladder that never gets cheaper per minute, which is a ladder
> with no volume discount at all.
>
> So the review screen **reports** the four numbers at each boundary rather than flagging
> them. Commercial decides whether the dead zone at the top of each band is acceptable;
> engineering does not get a vote, and a warning that fires on every legitimate ladder is
> just noise that teaches people to ignore warnings.

`discount_percentage` and `tax_percentage` stay on the plan and apply exactly as they do
today, to the computed net rather than to `base_price`.

---

## 5. The basis tier

The wallet is denominated in credits, not minutes, because a minute costs a different
number of credits depending on how the call was served. So the promise *"1,200 minutes"*
has to be made **on a specific tier**, and `credit_basis` is where the plan says which:

```python
credit_basis in {
    static_reel,                 # one-way playback
    conversation_high_latency,   # standard conversation
    conversation_low_latency,    # realtime conversation
}

row = open CreditRate(plan, credit_basis)

net_price         = selected_minutes * row.price_per_minute_inr
allocated_credits = selected_minutes * row.credits_per_unit
```

**The plan chooses the basis, not the customer.** That is what keeps the band a function
of minutes alone: at any quantity there is one plan, one basis row, one price and one
grant. It also removes any question of a customer buying on the cheapest tier and spending
on the dearest — they never pick.

### What the customer really gets

The basis is a floor, not a cap. Credits are fungible across tiers, so granting at the
basis buys **exactly** the minutes promised on that tier, more if the customer calls more
cheaply, and fewer if they call more expensively:

```
Growth . basis = high latency . rates: reel 2 | high 4 | low 8

4,800 credits granted for 1,200 selected minutes
  |- 1,200 minutes if every call is high-latency   <- what was sold
  |-   600 minutes if every call is low-latency
  \- 2,400 minutes if every call is a static reel
```

> **Choosing the basis is a commercial decision with a real cost.** A plan based on reel
> and used for realtime delivers a quarter of the minutes the page implied. That is not a
> bug — credits are fungible on purpose — but it needs to be a choice somebody made, and
> the API returns the equivalent minutes on all three tiers so the page can say it plainly.

### Non-per-minute basis rows

A `CreditRate` may be `per_second`, `per_minute` or `per_event`. Converting a minutes
quantity requires a per-minute figure, so:

| Basis row's unit | Credits per minute |
| :--- | :--- |
| `per_minute` | `credits_per_unit` |
| `per_second` | `credits_per_unit × 60` |
| `per_event` | **Not usable as a basis** — a flat per-call price has no per-minute meaning. Blocking at activation |

`price_per_minute_inr` is per *minute* whatever the row's `unit` is, because it prices a
purchase rather than a call. `rounding` and `minimum_units` belong to rating a call and are
deliberately ignored here — applying a per-call minimum to a monthly grant would inflate it
by the number of calls the customer has not yet made.

---

## 6. Worked example, end to end

**Catalogue** — three monthly plans. Each rate row now carries both numbers:

| Plan | Band | basis | | reel | standard | realtime | ₹/credit |
| :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: |
| Starter | –500 | standard | credits/min | 3 | **5** | 10 | 0.55 |
| | | | ₹/min | 1.65 | **2.75** | 5.50 | |
| Growth | 501–2,000 | standard | credits/min | 2 | **4** | 8 | 0.45 |
| | | | ₹/min | 0.90 | **1.80** | 3.60 | |
| Scale | 2,001– | standard | credits/min | 2 | **3** | 7 | 0.40 |
| | | | ₹/min | 0.80 | **1.20** | 2.80 | |

Every plan is internally consistent at one ₹/credit, and the ladder gets cheaper per
credit as it goes up — which is the volume discount, expressed once.

**A customer needs 1,200 minutes a month.**

```
1  frontend  GET /api/v1/plans/for-minutes/?minutes=1200&billing_cycle=monthly
             -> Growth, and the whole quote below

2  basis     CreditRate(Growth, conversation_high_latency)
             price_per_minute_inr = 1.8000    credits_per_unit = 4

3  price     net    1,200 x 1.8000        = Rs 2,160.0000 -> Rs 2,160.00
             disc   0%                    = Rs     0.00
             tax    18% of 2,160.00       = Rs   388.80
             total                          Rs 2,548.80   -> 254880 paise to Razorpay

4  credits   grant  1,200 x 4             = 4,800 credits
             = 1,200 standard min | 600 realtime | 2,400 reel

5  signup    TempUser.selected_plan = Growth, TempUser.selected_minutes = 1200
             order raised for Rs 2,548.80

6  payment   subscriptions.activate(tenant, Growth, minutes=1200)
             |- Subscription.selected_minutes = 1200
             |- plan_snapshot frozen (see section 7)
             |- wallet rate card stamped from Growth
             \- wallet granted 4,800 credits (CYCLE_ALLOCATION)
```

**The same customer at 2,500 minutes** lands on Scale: 2,500 × ₹1.20 = ₹3,000 net, and
2,500 × 3 = 7,500 credits — more minutes, a lower per-minute price *and* a lower price per
credit, which is the incentive to move up the ladder working correctly.

---

## 7. Where the chosen minutes live

The quantity is part of what was bought, so it is stored on the same rows the rest of the
purchase is:

| Row | Field | Why |
| :--- | :--- | :--- |
| `TempUser` | `selected_minutes` | Chosen before a tenant exists; survives session resume |
| `Subscription` | `selected_minutes` | The live commitment; what a renewal repeats |
| `Subscription` | **`allocated_credits`** | **New column.** What this cycle actually granted — see below |
| `Subscription` | **`scheduled_minutes`** | **New column.** The quantity a pending downgrade moves to — see below |
| `Subscription` | `plan_snapshot` | The **frozen contract**, below |
| `Transaction` | `amount_breakdown` | How the charge was computed, for a dispute six weeks later |

### `allocated_credits` moves from the plan to the subscription

It is on `Plan` today, and that is the conflation at the heart of the old model: one column
being asked two different questions.

| Question | Where it belongs | Answered by |
| :--- | :--- | :--- |
| *How many credits did **this subscription** get?* | A fact about one sale | `Subscription.allocated_credits` — a real column |
| *How many credits would **N minutes** get on this plan?* | A function of the catalogue | `plan.credits_for(minutes)` — computed, never stored |

A plan cannot answer the first question once the grant depends on a quantity, and it should
never have been asked to. Splitting them means the plan holds only what is true of the
plan, and the subscription holds what is true of the sale.

Making it a **column** rather than leaving it in the snapshot JSON buys three things:

- **`Subscription.rules` no longer needs a plan fallback for it.** That fallback —
  `getattr(self.plan, key)` for keys an old snapshot predates — is the single reason
  dropping `Plan.allocated_credits` was risky. With the column present, the risk is gone.
- **It is queryable.** "Credits granted across active subscriptions this month" becomes a
  SQL aggregate instead of a JSON extraction over every row.
- **It records what was actually granted**, including a negotiated `credit_grant` override
  that the plan's own number never reflected.

> **Invariant worth holding onto:** `Subscription.allocated_credits` is written from the
> same expression that drives `wallet.grant()`. The column and the credit ledger cannot
> disagree about what a cycle was worth, because they are the same number written twice.

### The snapshot is where this change pays for itself

`plan_snapshot` already freezes the price at purchase, and **everything downstream already
reads it instead of the live plan** — proration, upgrade bonus-days, renewal orders,
plan-change quotes, entitlement limits. So the snapshot writes the *computed* figures
rather than the catalogue's:

```jsonc
{
  "code": "growth",
  "billing_cycle": "monthly",

  "selected_minutes": 1200,          // new - the quantity bought
  "credit_basis": "conversation_high_latency",
  "price_per_minute_inr": "1.8000",  // new - frozen from the basis row
  "credits_per_minute": 4,           // new - frozen from the same row

  "base_price":     "2160.00",       // computed for 1,200 min, not the catalogue value
  "net_base_price": "2160.00",
  "tax_amount":      "388.80",
  "total_price":    "2548.80",

  "allocated_credits": 4800,         // same key, same name, same meaning as always -
                                     // now 1,200 x 4 instead of a number off the plan
  "entitlements": { }
}
```

**`allocated_credits` keeps its name and its place.** It was always the answer to *"how
many credits does this subscription get this cycle"* — the only thing that changes is where
the number comes from. No rename, no alias, and every existing reader of
`rules["allocated_credits"]` is correct without being touched.

Writing the computed price into the **existing** `net_base_price` and `total_price` keys
is deliberate. `pricing.quote_plan_change()` reads `snapshot["net_base_price"]` to value
the customer's unused time; `unused_ratio()` reads the dates; `bonus_days_for()` divides
by the new plan's daily rate. All of that stays correct with no changes, because the
number it was already reading now means "what this customer actually pays per cycle"
instead of "what the catalogue says this plan costs".

**`price_per_minute_inr` and `credits_per_minute` are frozen here, not looked up later.**
The basis row can be superseded the day after a customer subscribes, and their cycle must
not silently re-price or re-grant. Renewal is the one place a new rate legitimately
reaches them, and renewal re-freezes.

---

## 8. What this does to the rest of billing

| Area | Effect |
| :--- | :--- |
| **Rating a call** | **None.** Calls are rated against `Wallet.rate_snapshot`, stamped from `CreditRate` exactly as before. `price_per_minute_inr` is **not** added to that snapshot — the call path has no use for rupees, and keeping it out is what stops money leaking onto the hot path |
| **Proration** | None. `unused_ratio × snapshot net` — both sides already correct |
| **Upgrade bonus days** | None. Unused value ÷ the new plan's daily rate, where the daily rate now comes from the new plan **at the requested minutes** |
| **Renewal** | Re-quotes the *stored* minutes at the basis row that is open today. Renewal has always been where a price change legitimately reaches an existing customer |
| **Top-ups** | None. `cost_per_additional_credit` stays on the plan — a top-up is priced in credits, not minutes |
| **Entitlement add-ons** | None. Capacity is rupees per cycle and has nothing to do with talk time |
| **Expiry / rollover** | Unchanged mechanically, but see [§9](#9-the-static-fields-and-which-of-them-go) |

### One genuinely new transaction shape: same plan, more minutes

Today `change-plan` refuses a request for the plan you are already on. With bands, *"I
need 1,800 minutes instead of 1,200"* is a real change that does not move plan — and it is
an upgrade, worth money.

The existing sign test handles it without a new concept:

```
owes = new_net(plan, new_minutes) - unused_value_of_current_cycle

  owes >  0  ->  upgrade     order raised, applied when it clears, unused time
                             converted into bonus days on the new quantity
  owes <= 0  ->  downgrade   scheduled for the end of the paid cycle
```

So `change-plan` gains a `minutes` field, and refuses only when **both** the plan and the
minutes are unchanged. Crossing a band boundary — 1,800 → 2,400 minutes moving Growth to
Scale — is the same code path with a different plan on the right-hand side. No tier
ladder, exactly as today.

### A downgrade that moves no plan needs somewhere to be stored

Dropping 2,000 minutes to 600 on the same plan is a downgrade like any other, so it is
scheduled for the end of the cycle the tenant has already paid for. But the existing
`scheduled_plan` cannot express it: it would hold **the plan they are already on**, and
when the scheduler fired at cycle end it would default the quantity to the current one
and move nothing. The tenant's downgrade would silently evaporate, and they would keep
paying for 2,000 minutes.

Hence `Subscription.scheduled_minutes`, written when the change is scheduled, cleared
when it is cancelled, and passed to `apply_plan_change()` by the billing-cycle task.
Null means "keep the current quantity", which is what an ordinary plan-only downgrade
wants.

> This one is not visible until "same plan, different quantity" becomes a real
> transaction shape — which is why it was not in the first version of this document.

---

## 9. The static fields, and which of them go

Once price and credits are both derived from (minutes × basis row), the plan's fixed
numbers are either dead or miscalibrated. There are four, and they do not all get the same
answer.

| Field | Verdict | Why |
| :--- | :--- | :--- |
| `allocated_credits` | **Move to `Subscription`** | The grant is a fact about a sale, not about a plan. On the plan it becomes `credits_for(minutes)` |
| `base_price` | **Remove, second** | Fully replaced by `minutes × price_per_minute_inr`, but it anchors a check constraint and four derived properties — a separate step |
| `low_balance_threshold` | **Recalibrate** | An absolute credit count cannot serve a band whose grant varies 4× |
| `max_rollover_credits` | **Recalibrate** | Same, and it silently burns credits rather than just mis-warning |
| `cost_per_additional_credit` | **Keep** | Prices top-ups, which are bought in credits and have nothing to do with minutes |

### Moving `allocated_credits`

It is **not deleted — it is relocated**, from the row that cannot answer the question to the
row that can. §7 has the reasoning; this is the mechanical part.

```
Plan.allocated_credits   ->  Plan.credits_for(minutes)        a function, computed
                         ->  Subscription.allocated_credits   a column, one per sale
```

The name never changes, so `snapshot["allocated_credits"]` and `rules["allocated_credits"]`
keep meaning exactly what they have always meant. **There is no alias and no rename** — the
only thing that moves is where the number is produced.

The sequence:

```
1  add Subscription.allocated_credits (nullable)
2  backfill it: snapshot["allocated_credits"], else plan.allocated_credits
                -- the plan column still exists at this point, which is the whole
                   reason the backfill has to run before step 4
3  move the readers: "this sale" -> the column, "this plan at N minutes" -> credits_for()
4  drop Plan.allocated_credits
```

**The snapshots themselves are never rewritten.** They are the frozen contract — a data
migration over them changes what a customer agreed to, and destroys the only record of it.
The backfill in step 2 reads them and writes a column; it does not touch them.

Step 2 is also what dissolves the one real hazard here. `Subscription.rules` falls back to
`getattr(self.plan, key)` for any key an old snapshot predates, so dropping the plan column
used to risk `AttributeError` on a live subscription. Once the value is on the subscription
itself, there is nothing left to fall back to.

### Removing `base_price`

Same argument, one step behind, because it drags more with it:

- the check constraint `ck_plan_free_renewal_only_when_zero_cost`, which has to be
  re-expressed as "every rate row is ₹0" — cross-table, so it becomes a readiness check
  rather than a database constraint;
- the `discount_amount` / `net_base_price` / `tax_amount` / `total_price` properties, which
  become the minutes-parameterised `price_components(minutes)`;
- the ops serializer validation at `ops/serializers/plan/plan.py:99-110`.

Worth doing, not worth doing in the same change as everything else.

### Recalibrating the other two

Growth spans 501–2,000 minutes, so 2,004 to 8,000 credits. One fixed number cannot serve
both ends:

| Field | Replacement | Resolution |
| :--- | :--- | :--- |
| `low_balance_threshold` | `low_balance_percent` | `ceil(allocated_credits × pct/100)` — 10% is 480 credits at 1,200 min and 800 at 2,000 |
| `max_rollover_credits` | `max_rollover_ratio` | `floor(allocated_credits × ratio)` — `1.00` means "carry at most one cycle's worth" |

A fixed threshold of 350 nags the small customer constantly and warns the large one when
they have hours left. A fixed rollover cap of 3,500 forfeits nothing from the small
customer and silently burns 4,500 credits from the large one at every renewal — the same
class of bug, with money attached.

---

## 10. Completeness rules for a v2 plan

The activation gate exists because a half-configured plan is silently permissive rather
than obviously broken. Minutes-driven plans add several ways to be silently wrong.

**Blocking — activation is refused:**

| Check | What goes wrong without it |
| :--- | :--- |
| Band does not overlap another active plan | Two prices for one request; auto-select is a coin-flip |
| No gap between two **ranged** bands | Round-up would silently sell the customer a larger band than they asked for. A gap beside a fixed-quantity pack is expected and allowed |
| `credit_basis` is set when a band is set | Priced in minutes with nothing to price against |
| An open `CreditRate` exists for `credit_basis` | Minutes convert to nothing; the grant would be zero, silently |
| That row's `price_per_minute_inr` is not null | The sale cannot be priced at all |
| That row is not `per_event` | A flat per-call price has no per-minute meaning |
| `price_per_minute_inr > 0` **or** `max_minutes` is set | A free plan open at the top grants unbounded credits for ₹0 |
| `min_minutes ≤ max_minutes` when both are set | An empty band no customer can ever match |

Only the last is a database check constraint — it is a single-row fact. Everything else
needs the rate card or the rest of the ladder, so it lives in the readiness service.

**Warning — accepted, reported on the review screen:**

- `RUPEES_PER_CREDIT_INCONSISTENT` — the plan's three tiers do not sell credits at one
  price (§3)
- `LADDER_OPEN_BOTTOM_MISSING` / `LADDER_OPEN_TOP_MISSING` — the smallest or largest
  customers cannot buy
- `RATE_MISSING_INR_PRICE` — a non-basis row has no rupee price, so the pricing page cannot
  quote that tier

Existing checks — all 7 entitlement rows, all 3 rate rows — are unchanged and still apply.

---

## 11. Backward compatibility

**Nothing that exists today breaks**, and the two generations run side by side for as long
as it takes.

A plan is v2 if `credit_basis` is set and its basis row carries a rupee price. Otherwise it
is legacy, and both quantities come from where they come from now:

```
plan.price_components(minutes)   v2 -> minutes x basis.price_per_minute_inr
                             legacy -> base_price
                                       (discount and tax applied identically to both)

plan.credits_for(minutes)        v2 -> minutes x basis.credits_per_unit
                             legacy -> Plan.allocated_credits, while it still exists;
                                       afterwards Subscription.allocated_credits,
                                       i.e. "the same grant as last cycle"
```

| | Behaviour |
| :--- | :--- |
| The 5 plans in the database today | Untouched. No band, no basis, priced as they are now |
| Every live subscription | Untouched. Its snapshot already holds its own price and grant |
| A legacy subscription renewing | Renews as it always has — `selected_minutes` is null, legacy branch runs |
| A legacy customer upgrading to a v2 plan | Works. `old_net` from their snapshot, `new_net` from the v2 quote; the sign test does not care where either came from |
| A v2 customer moving to a legacy plan | Works, same reason |
| The migration | Additive and nullable throughout. `selected_minutes` backfills to **null**, not guessed — deriving it from `allocated_credits ÷ a rate` would invent a commitment nobody made. `Subscription.allocated_credits` **is** backfilled, from the snapshot |

### The one thing that keeps `Plan.allocated_credits` alive

Renewal. A legacy subscription renewing needs a grant, and it reads the plan:

```
renew():  allocation = plan.credits_for(subscription.selected_minutes)

          v2      -> minutes x today's basis rate
          legacy  -> today's plan.allocated_credits
```

One expression, no branch, because a renewal is a **future cycle** — the one place a
price or rule change legitimately reaches an existing customer. An earlier draft of this
document proposed reading last cycle's grant for legacy plans, on the theory that a
legacy plan's grant never changes. It does: an admin can edit it, and the existing test
SL-27 says that edit must land at the next renewal. The draft was wrong and the test
caught it.

**So the column cannot be dropped while any active plan lacks a basis.** The precondition
for §9 is therefore not "wait for legacy subscribers to churn" — they may never — but
**give every remaining active plan a band and a basis**. There are five of them. Once
`credits_for` has a basis rate to read on every plan, nothing reads the column and it can
go.

The removals in §9 happen **after** this phase, once every *new* sale is v2 — not as part
of it.

---

## 12. Edge cases

| Case | Behaviour |
| :--- | :--- |
| Requested minutes match no band, but a larger pack exists | Rounded up to the smallest pack that covers them. The response says so — `rounded_up: true`, with both the requested and the sold quantity |
| Requested minutes exceed every band and pack | `404` with the nearest bands, so the page can say "our largest pack is 10,000 minutes — talk to us" rather than "not found" |
| Requested minutes are 0 or negative | `400`. Zero minutes is not a purchase |
| Requested minutes exceed a sane ceiling | `400` above a configured absolute maximum. A mistyped 1200000 must be refused, not quoted at ₹1.4M and sent to a gateway |
| A v2 plan is retired mid-signup | The `TempUser` keeps its plan and minutes; the order is priced from that plan. Retiring stops *new* selection, and this selection already happened |
| The basis row is superseded between quote and payment | The order froze both numbers into `amount_breakdown`; activation reads them back rather than recomputing. The customer gets what they were shown |
| The **band** moves between quote and payment | The paid quantity is applied verbatim — an explicit quantity is never re-resolved against the band. See §4 |
| The basis row is superseded mid-cycle | Nothing happens until renewal, which re-freezes. This is the existing rule for every other term |
| `credit_basis` points at a row that was later closed and not reopened | Blocked at activation, but a *rate* edit happens afterwards — so the same check runs on rate writes too |
| A customer wants more minutes mid-cycle | `change-plan` with the same plan and higher minutes → an upgrade. Or a top-up, unchanged and still right for a one-off overrun |
| Free plan (`price_per_minute_inr = 0`) with a band | Legal and useful — a trial capped at 100 minutes. `max_minutes` is mandatory, per §10 |
| Two plans, same band, different billing cycles | Legal. Ladders are checked per `billing_cycle` |

---

## 13. Deliberately out of scope

- **Letting the customer choose the tier at purchase.** The plan chooses the basis, per
  §5. The model would support it — the rupee price is already per tier — but it changes
  band matching from a function of minutes into a function of (minutes, tier), and it is
  not what was asked for.
- **Selling minutes in steps** (blocks of 50, sliders that snap). One field —
  `minutes_step` — would do it. A UX refinement, not part of making plans minutes-driven.
- **Per-tier minute bundles** — "800 standard + 200 realtime" as one purchase. Expressible
  only by giving up the single-basis model.
- **Automatic band suggestion from usage history.** The frontend picks the plan from a
  number the customer types. Suggesting that number from last month's minutes is a separate
  feature on top of this one.
- **Changing how a call is rated.** Untouched, on purpose. Minutes and rupees are
  purchasing concepts and stop at the wallet's front door.

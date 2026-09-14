# Plan v2 — Implementation Plan

Making plans minutes-driven: the customer picks a quantity, the plan is selected from its
band, and both the rupee price and the credit grant are derived from the basis rate row.

**Feature detail:** [plan-v2-feature.md](../feature-detail-scratch/plan-v2-feature.md) ·
**Status:** 🟢 All phases built and green

---

## Contents

1. [What this touches, and what it must not](#1-what-this-touches-and-what-it-must-not)
2. [The two ideas that keep the blast radius small](#2-the-two-ideas-that-keep-the-blast-radius-small)
3. [Phase 1 — model and migrations](#3-phase-1--model-and-migrations)
4. [Phase 2 — pricing primitives](#4-phase-2--pricing-primitives)
5. [Phase 3 — plan selection and the public API](#5-phase-3--plan-selection-and-the-public-api)
6. [Phase 4 — the signup path](#6-phase-4--the-signup-path)
7. [Phase 5 — activation and renewal](#7-phase-5--activation-and-renewal)
8. [Phase 6 — plan changes](#8-phase-6--plan-changes)
9. [Phase 7 — ops console, readiness and clone](#9-phase-7--ops-console-readiness-and-clone)
10. [Phase 8 — dropping Plan.allocated_credits](#10-phase-8--dropping-planallocated_credits)
11. [Phase 9 — recalibration and removing base_price](#11-phase-9--recalibration-and-removing-base_price)
12. [Test plan](#12-test-plan)
13. [Rollout](#13-rollout)
14. [Risks](#14-risks)
15. [Open questions](#15-open-questions)

---

## 1. What this touches, and what it must not

| Layer | Change |
| :--- | :--- |
| `onboard.Plan` | 3 new nullable fields, 1 check constraint, 5 helper methods; **`allocated_credits` becomes a method** |
| **`wallet.CreditRate`** | **1 new nullable field — `price_per_minute_inr`** |
| `onboard.Subscription` | 3 new fields — `selected_minutes`, **`allocated_credits` relocated from `Plan`**, and `scheduled_minutes`; snapshot gains 4 keys |
| `onboard.TempUser` | 1 new nullable field |
| `onboard.services.pricing` | New `quote_plan()`; `quote_plan_change()` and `credit_impact()` gain `minutes` |
| `onboard.services.subscriptions` | `activate()` and `renew()` gain minutes awareness |
| **New** `onboard.services.plan_selection` | Band matching + ladder validation |
| Onboarding views | 7 views carry minutes through the signup flow |
| Billing views | 5 views accept or report minutes |
| `ops` plan module | Serializers, readiness (7 blocking, 4 warnings), clone notes, rate write guard |

**Must not change:**

- **`wallet.services.rating`** and everything on the call path. If a diff touches
  `rating.py`, something has gone wrong.
- **`CreditRate.as_snapshot_entry()`** — `price_per_minute_inr` is deliberately **not**
  stamped onto `Wallet.rate_snapshot`. The call path has no use for rupees, and the wallet
  snapshot is read on every call.
- **`onboard.PlanEntitlement` / `TenantEntitlement`** — capacity is rupees per cycle and
  has nothing to do with talk time.
- **Proration arithmetic** in `unused_ratio()` — see §2.

---

## 2. The two ideas that keep the blast radius small

### 2.1 Both numbers come from one row

`price_per_minute_inr` goes on `CreditRate`, beside `credits_per_unit`, so the money price
and the credit price of a tier cannot be changed independently. One lookup — the plan's
basis row — answers both halves of a sale.

It also means the rupee price inherits `CreditRate`'s supersede-don't-edit machinery,
including `uniq_current_rate_per_plan_operation`. No new versioning code.

### 2.2 The snapshot absorbs the change

`Subscription.plan_snapshot` already freezes the price at purchase, and every downstream
consumer already reads the snapshot rather than the live plan. So the v2 numbers are
written into **the keys that already exist**:

```
snapshot["net_base_price"]     was  the catalogue's net for this plan
                               now  minutes x basis.price_per_minute_inr, less discount

snapshot["allocated_credits"]  was  plan.allocated_credits
                               now  minutes x basis.credits_per_unit
                                    (same key, same name - no alias, no rename)
```

`allocated_credits` also becomes a **column on `Subscription`**, written from the same
expression. That is what lets `Plan.allocated_credits` be dropped later without the
`rules` fallback — see Phase 8.

Everything that reads those keys keeps working with **zero changes**:

| Reader | File | Why it stays correct |
| :--- | :--- | :--- |
| `quote_plan_change()` — value of unused time | `services/pricing.py:190` | Reads `snapshot["net_base_price"]`, now this customer's real cycle price |
| `Subscription.rules` | `models/subscription.py:213` | Exposes `allocated_credits` from the snapshot, now the real grant |
| `rollover_amount()` | `services/subscriptions.py` | Reads `rules`, unchanged |
| `process_billing_cycle()` | `tasks/billing_cycle.py` | Reads `rules["renews_without_payment"]`, unchanged |
| `backfill_wallet_stamps` | `management/commands/backfill_wallet_stamps.py:66` | Reads `rules.get("allocated_credits")`, unchanged |
| Entitlement limits | `services/entitlements.py` | Reads `snapshot["entitlements"]`, untouched |

> **Design rule: derive at the boundary, freeze into the snapshot, leave the interior
> alone.** Any patch adding `if subscription.selected_minutes:` deep inside proration or
> rollover is fighting this and should be reworked.

---

## 3. Phase 1 — model and migrations

### 3.1 `wallet/models/credit_rate.py`

```python
price_per_minute_inr = models.DecimalField(
    max_digits=10, decimal_places=4, null=True, blank=True,
    help_text=(
        "Pre-discount, pre-tax rupees per minute of this tier on this plan. Read only "
        "when a plan is SOLD at this tier (Plan.credit_basis); never on the call path. "
        "Lives here rather than on Plan so the money price and the credit price of a "
        "minute cannot drift apart, and so a price change is superseded rather than "
        "silently overwritten."
    ),
)
```

**`as_snapshot_entry()` is not changed.** The wallet stamp stays exactly as it is — see §1.

Four decimal places on the column; totals are quantized to paise once, in
`Plan.price_components()`, and never re-derived downstream.

### 3.2 `onboard/models/plan.py`

```python
class CreditBasis(models.TextChoices):
    """Mirrors wallet.CreditRate.Operation. String-equal and deliberately separate,
    exactly as core.constants.ServiceTier already is."""
    STATIC_REEL = "static_reel", "Static Reel"
    CONVERSATION_HIGH_LATENCY = "conversation_high_latency", "Conversation - Standard"
    CONVERSATION_LOW_LATENCY = "conversation_low_latency", "Conversation - Realtime"

min_minutes  = PositiveIntegerField(null=True, blank=True)
max_minutes  = PositiveIntegerField(null=True, blank=True)
credit_basis = CharField(max_length=32, choices=CreditBasis.choices, null=True, blank=True)
```

**One check constraint** — the only new single-row fact:

```python
models.CheckConstraint(
    condition=Q(min_minutes__isnull=True) | Q(max_minutes__isnull=True)
              | Q(max_minutes__gte=F("min_minutes")),
    name="ck_plan_minutes_band_valid",
)
```

Everything else needs the rate card or the rest of the ladder, so it goes in readiness
(§9), not the database.

**Methods** — the entire legacy/v2 split lives here and nowhere else:

| Method | Returns |
| :--- | :--- |
| `is_minutes_based` | `credit_basis` set **and** its open rate row has a non-null `price_per_minute_inr` |
| `basis_rate()` | The open `CreditRate` for `credit_basis`, or `None`. Cached per instance |
| `contains_minutes(n)` | `(min is None or n >= min) and (max is None or n <= max)` |
| `resolve_minutes(n)` | `n` expressed as a quantity this plan can sell — clamped into the band, or a pack's single value. **A method, not a `plan_selection` helper**: it is band arithmetic on one row, and keeping it here avoids a `pricing -> plan_selection` dependency |
| `is_fixed_quantity` | `min == max`, both set. Derived, never stored |
| `credits_for(minutes)` | v2: `minutes × credits_per_minute(basis)` — `per_second × 60`, `per_event` invalid. Legacy: `allocated_credits` |
| `price_components(minutes)` | `(net, discount, tax, total)`. v2 net = `minutes × basis.price_per_minute_inr`; legacy net = `base_price`. Discount and tax applied identically in both branches |

`price_components()` reuses the `ROUND_HALF_UP` quantization the existing
`discount_amount` / `net_base_price` / `tax_amount` / `total_price` properties use. Those
properties are **left exactly as they are** — they are the catalogue view, still correct
for legacy plans and still what the admin renders.

### 3.3 `onboard/models/subscription.py`

```python
selected_minutes  = PositiveIntegerField(null=True, blank=True)
allocated_credits = PositiveIntegerField(null=True, blank=True)   # relocated from Plan
```

`allocated_credits` is nullable **only until the backfill migration below runs**; it is
tightened to `default=0, null=False` in Phase 8. It records what this cycle actually
granted, including a negotiated `credit_grant` override that the plan's own number never
reflected.

> **Invariant:** written from the same expression that drives `wallet.grant()`. The column
> and the credit ledger cannot disagree about what a cycle was worth, because they are the
> same number written twice. A test asserts it.

`generate_plan_snapshot()` (currently `models/subscription.py:155-200`) computes
`base_price` / `net_base_price` / `tax_amount` / `total_price` through
`plan.price_components(self.selected_minutes)`, sets `allocated_credits` from
`self.allocated_credits`, and adds `selected_minutes`, `credit_basis`,
`price_per_minute_inr`, `credits_per_minute`.

For a legacy subscription (`selected_minutes is None`) the price helper falls through, so
the emitted snapshot is **byte-identical to today's** plus the new keys at null.

`rules` (line 213) gains the four new keys, and resolves `allocated_credits` as
snapshot → **column** → nothing. The `getattr(self.plan, key)` fallback for that one key is
what Phase 8 exists to retire.

### 3.4 `onboard/models/temp_user.py`

```python
selected_minutes = PositiveIntegerField(null=True, blank=True)
```

### 3.5 Migrations

| App | # | Contents |
| :--- | :--- | :--- |
| `wallet` | `00XX_creditrate_price_per_minute_inr` | 1 column |
| `onboard` | `00XX_plan_minutes_band` | 3 columns + `ck_plan_minutes_band_valid` |
| `onboard` | `00XX_subscription_minutes_and_credits` | 2 columns — `selected_minutes`, `allocated_credits` |
| `onboard` | `00XX_backfill_subscription_allocated_credits` | **Data migration**, below |
| `onboard` | `00XX_tempuser_selected_minutes` | 1 column |
| `onboard` | `0032_subscription_scheduled_minutes` | 1 column, added in Phase 6 — see there |

Everything except the backfill is additive and nullable. The backfill:

```python
allocated_credits = plan_snapshot.get("allocated_credits")
                    or plan.allocated_credits      # still present at this point
                    or 0
```

**It runs now, in Phase 1, not in Phase 8** — precisely because `Plan.allocated_credits`
still exists here and will not by the time Phase 8 wants it. Doing the read early is what
makes the later drop a one-line migration instead of a risk.

`selected_minutes` is deliberately **not** backfilled. Deriving it from
`allocated_credits ÷ a rate` would invent a commitment nobody made.

Add cases to `onboard/tests/test_migrations.py`: the constraint rejects an inverted band,
and the backfill populates a subscription whose snapshot predates `allocated_credits`.

---

## 4. Phase 2 — pricing primitives

### `onboard/services/pricing.py`

**New — `quote_plan(plan, minutes)`** returns the existing `Quote` dataclass, so it
serialises through `as_breakdown()` into `Transaction.amount_breakdown` like every other
quote:

```python
Quote(
    net=..., tax=..., total=..., tax_percentage=...,
    detail={
        "selected_minutes": minutes,
        "credit_basis": plan.credit_basis,
        "price_per_minute_inr": rate.price_per_minute_inr,   # frozen
        "credits_per_minute": credits_per_minute,            # frozen
        "allocated_credits": plan.credits_for(minutes),
        "discount_percentage": plan.discount_percentage,
    },
)
```

Freezing both per-minute figures into the breakdown is what makes the "rate superseded
between quote and payment" case correct — activation reads them back rather than
recomputing.

Raises `PricingError` when: minutes ≤ 0; minutes outside the band; minutes above
`MAX_SELECTABLE_MINUTES` (new module constant, same spirit as `MAX_TOP_UP_CREDITS`); the
basis row is missing, `per_event`, or has no rupee price.

**Changed — `quote_plan_change(subscription, new_plan, *, minutes=None, now=None)`:**

- `old_net` — unchanged, still `snapshot["net_base_price"]` (line 190)
- `new_net` — was `new_plan.net_base_price`, now `quote_plan(new_plan, minutes).net` when
  `new_plan.is_minutes_based`
- `minutes` defaults to `subscription.selected_minutes` **resolved through
  `new_plan.resolve_minutes(minutes)`** — contained if it fits the new band,
  otherwise rounded up to the band's minimum or down to its maximum. So "move me to Scale"
  without a quantity means "the same usage, on Scale", and moving onto a 5,000-minute pack
  means 5,000 whatever the customer had before. Plain clamping is wrong for a pack, which
  has exactly one legal value
- `detail` gains `selected_minutes`, `allocated_credits`, `credits_delta`
- `unused_ratio`, `credit_from_addons`, `bonus_value`, the `owes` sign test and the three
  change types are **untouched**

**Changed — `credit_impact(subscription, new_plan, *, reason, minutes=None)`** at lines
304-305. It currently reads `new_plan.allocated_credits` twice, for
`new_plan_allocation` and `balance_after_change`. Both become
`new_plan.credits_for(minutes)` — this is the "what happens to your credits" preview, so a
wrong number here is shown to the customer before they commit.

**Changed — `bonus_days_for(value, plan, *, anchor=None, minutes=None)`:** `per_day` is
`plan.net_base_price / cycle_days`. For a v2 plan that must be the net *at the requested
minutes*, or unused value buys days at a catalogue price nobody agreed to.

**Unchanged:** `quote_top_up`, `quote_addon`, `unused_ratio`, `cycle_days_for`,
`held_extra`.

---

## 5. Phase 3 — plan selection and the public API

### New: `onboard/services/plan_selection.py`

```python
def for_minutes(minutes, *, billing_cycle="monthly", currency="INR"):
    """The plan that sells `minutes`, and the quantity it sells them as.

    Returns (plan, sold_minutes) or (None, None). Two steps:

      1. exact containment -- the one band containing `minutes`
      2. round up          -- the smallest plan whose max_minutes >= `minutes`

    Step 2 serves ladders made of fixed-quantity packs (min == max), which have gaps
    by construction. It can only ever land on a pack, because a gap between two
    RANGED bands is blocked at activation -- so step 1 never fails inside the
    continuous part of a ladder.
    """

def ladder(*, billing_cycle, currency="INR"): ...
def ladder_problems(plans): ...      # overlaps + gaps BETWEEN RANGED bands only
def nearest_bands(minutes, *, billing_cycle, currency="INR"): ...
def rupees_per_credit(plan): ...     # {operation: Decimal} for the consistency warning
```

`ladder()` includes only plans where `is_minutes_based` holds, so **legacy plans are never
in the ladder and never collide with it.** That is what lets both generations be active at
once.

`ladder_problems()` has three callers — the activation gate, the ops review screen and
`audit_billing_readiness` — which is why it is a service. A gap adjacent to a
fixed-quantity band is expected and not reported; a gap between two ranged bands is
blocking, because round-up would silently overcharge into it.

### Fixed-quantity plans

`Plan.is_fixed_quantity` — `min_minutes == max_minutes`, both non-null — is a **derived
property, not a column**. A stored flag can disagree with the bounds; a derived one cannot.
The frontend switches on it to render a card rather than a slider.

Nothing else special-cases it. A pack is priced and granted through the same
`price_components()` / `credits_for()` as any other band, and `is_minutes_based` is `true`
for it.

> **`for_minutes()` returns a pair, and every caller must use the second element.** Pricing,
> granting and `Subscription.selected_minutes` all take `sold_minutes`; only
> `amount_breakdown["requested_minutes"]` keeps the original. Getting this backwards charges
> a customer for a 5,000-minute pack and grants them 1,200 minutes of credits.

### `GET /api/v1/plans/for-minutes/`

Public (`AllowAny`), a `@action(detail=False)` on the existing read-only `PlanViewSet`.

```
?minutes=1200&billing_cycle=monthly

200 {
  "plan": { "id", "code", "name", "description", "billing_cycle",
            "min_minutes": 501, "max_minutes": 2000,
            "is_fixed_quantity": false, "is_most_popular": true },
  "requested_minutes": 1200,         // what was asked for
  "selected_minutes": 1200,          // what is sold - differs on a pack
  "rounded_up": false,               // true when a pack covers a smaller request
  "money":   { "basis": "conversation_high_latency",
               "price_per_minute_inr": "1.8000",
               "net": "2160.00", "discount": "0.00", "tax": "388.80",
               "total": "2548.80", "tax_percentage": "18.00", "currency": "INR" },
  "credits": { "per_minute": 4, "granted": 4800,
               "equivalent_minutes": { "static_reel": 2400,
                                       "conversation_high_latency": 1200,
                                       "conversation_low_latency": 600 } },
  "tiers": [ { "operation", "credits_per_minute", "price_per_minute_inr" }, ... ],
  "entitlements": [ ... ]
}

404 { "errors": ["NO_PLAN_FOR_MINUTES"],
      "data": { "nearest_below": {...}, "nearest_above": {...} } }
400 { "errors": ["INVALID_MINUTES"] }
```

`equivalent_minutes` and `tiers` are what make the offer honest on the pricing page: 4,800
credits is 1,200 standard minutes *or* 600 realtime ones, and realtime is ₹3.60/min if they
want to size it themselves.

### Serializers

`PlanSerializer` / `PlanListSerializer` gain `min_minutes`, `max_minutes`, `credit_basis`,
`is_minutes_based`; their existing `get_rates()` gains `price_per_minute_inr` per row. The
pricing page then renders a slider from the ladder without a request per tick, and calls
`for-minutes/` once, on commit.

---

## 6. Phase 4 — the signup path

Minutes must survive OTP verification, a browser refresh and a resumed session, so they go
on `TempUser` at step 1 and are read back everywhere the plan is.

| File | Change |
| :--- | :--- |
| `serializers/onboarding/signup.py` | `minutes = IntegerField(required=False)`. In `validate()`: required and band-checked when the plan `is_minutes_based`, rejected when it is not. Persist in `create()` |
| `views/onboarding/verify_otp.py:91` | `billing_breakdown` from `quote_plan(plan, temp_user.selected_minutes)` plus a `credits` block. Currently reads `plan.base_price` directly |
| `views/onboarding/update_plan.py:47` | Accept `minutes`; re-validate against the **new** plan's band; recompute. A customer switching Growth→Starter with 1,200 minutes must be refused here, not at checkout |
| `views/onboarding/create_order.py:83` | `amount_in_paisa` from `quote_plan(...)`, not `plan.total_price`. Store `quote.as_breakdown()` on the `Transaction` |
| `views/onboarding/create_order.py:86-145` | The free-plan branch — passes `minutes=` to `activate()`. See the note below |
| `views/onboarding/verify_payment.py` | `activate(..., minutes=temp_user.selected_minutes)` |
| `services/payment_application.py::_apply_onboarding` | Same argument — the webhook's copy of the same promotion |
| `views/onboarding/resume_session.py:132` | Return `selected_minutes` and a breakdown computed from it |
| `views/onboarding/resume_session_token.py:36` | Same |
| `serializers/onboarding/responses.py:28` | `billing_breakdown` gains `selected_minutes`, `price_per_minute_inr`, `allocated_credits` |

> **Fold the free-plan branch into `_apply_onboarding()` as part of this phase.**
> `create_order.py:86-145` is a hand-copied version of it and they have already drifted —
> the inline copy does not pass `timezone`/`language` to the new user and does not set
> `transaction.tenant`. Adding `minutes` to both is a third chance to drift, and the next
> person will find a fourth difference.

---

## 7. Phase 5 — activation and renewal

### `onboard/services/subscriptions.py`

**`activate(tenant, plan, *, minutes=None, ...)`** — pass `selected_minutes=minutes` to
`Subscription.objects.create()`; `generate_plan_snapshot()` does the rest.

In `_ensure_wallet_funded()` (line 226):

```python
# was: allocation = plan.allocated_credits if credit_grant is None else credit_grant
allocation = (
    plan.credits_for(subscription.selected_minutes)
    if credit_grant is None else credit_grant
) or 0

# and then, before the grant, so the column records what was really given:
subscription.allocated_credits = allocation
subscription.save(update_fields=["allocated_credits", "updated_at"])
```

`credit_grant` still wins outright — that is the negotiated-contract override, it must keep
beating any derivation, **and it is exactly why the column is written from `allocation`
rather than recomputed.** A custom deal's grant appears nowhere else.

The write has to happen **before** `wallet_service.grant()`, inside the same atomic block:
a grant that succeeds while the column write fails would leave the ledger and the
subscription disagreeing, which is the one thing the invariant in §3.3 promises cannot
happen.

**The re-entry guard needs a second condition.** Line ~103 currently returns the existing
subscription when `existing.plan_id == plan.id and previous is None`. Compare
`existing.selected_minutes == minutes` too, or an upgrade from 1,200 to 1,800 minutes on
the same plan silently returns the old subscription and grants nothing.

**`renew()`** (line 404) — `selected_minutes` carries forward on the same row;
`generate_plan_snapshot()` re-freezes at the basis row open today.

```python
# One expression, no branch: credits_for already knows which kind of plan it holds.
allocation = plan.credits_for(subscription.selected_minutes) or 0
subscription.allocated_credits = allocation
```

**Not** last cycle's grant for a legacy plan. An earlier draft proposed that, reasoning
that a legacy grant never changes; it can be edited, and `test_sl27` asserts the edit
lands at renewal. This means renewal remains a live reader of `Plan.allocated_credits`,
so Phase 8's precondition is that **every active plan has a basis** — not that legacy
subscribers have churned, which may never happen.

**`apply_plan_change(..., minutes=None)`** passes straight through to `activate()`.

### `onboard/views/billing/subscription.py`

- `RenewSubscriptionView` — the quote is assembled inline from `plan.net_base_price +
  addons`. Replace the plan half with `quote_plan(plan, subscription.selected_minutes).net`,
  keep the add-on half.
- `CurrentSubscriptionView:117` — report `selected_minutes`, `price_per_minute_inr`,
  `allocated_credits` and `equivalent_minutes` alongside the existing payload.

---

## 8. Phase 6 — plan changes

### `PlanChangeSerializer` gains `minutes` (optional)

| Request | Outcome |
| :--- | :--- |
| Different plan, no minutes | Current minutes resolved into the new band via `Plan.resolve_minutes()` — a pack always resolves to its single value |
| Different plan, with minutes | Validated against the new plan's band |
| **Same plan, different minutes** | **New.** A real change, priced by the sign test |
| Same plan, same minutes | `409 PLAN_UNCHANGED` — the only case still refused |

### `Subscription.scheduled_minutes` — found while building this phase

A downgrade in quantity moves no plan, so `scheduled_plan` holds the plan the tenant is
already on and the scheduler would fire having changed nothing. The requested downgrade
would evaporate silently and the tenant would keep paying the higher amount.

| Site | Change |
| :--- | :--- |
| `models/subscription.py` | New nullable column; migration `0032` |
| `schedule_plan_change(..., minutes=)` | Records it |
| `cancel_scheduled_change()` | Clears it alongside the plan |
| `tasks/billing_cycle.py` | Passes it to `apply_plan_change()` |
| `ChangePlanView` | Supplies the settled quantity when scheduling |
| `CurrentSubscriptionView` | Reports it inside `scheduled_change` |

Null means "keep the current quantity" — what a plan-only downgrade wants. Covered by a
test that runs the real scheduler and asserts the new subscription comes out at the
reduced quantity with the matching grant.

`ChangePlanView`'s guard moves from `plan.pk == subscription.plan_id` to a check on the
*pair*. `PreviewPlanChangeView` takes the same argument and needs no other change.
`EligiblePlansView` takes an optional `?minutes=` and prices every candidate at it, clamped
into each band — so the table answers "what would *my* usage cost on each plan" rather than
"what does each plan cost at a quantity nobody asked for".

### `services/payment_application.py::_apply_upgrade`

Already reads `bonus_days` back from the frozen breakdown. It must read `selected_minutes`
the same way and pass it to `apply_plan_change()`. Recomputing from the live plan would
hand the customer a different quantity than the one they paid for.

---

## 9. Phase 7 — ops console, readiness and clone

### `ops/services/plan/readiness.py`

`blocking_problems()` gains, all guarded by "this plan has a band or a basis":

| Code | Condition |
| :--- | :--- |
| `BAND_OVERLAPS` | Band intersects another active v2 plan, same cycle + currency |
| `BAND_GAP` | Gap to an adjacent **ranged** band. A gap beside a fixed-quantity pack is expected and not reported |
| `CREDIT_BASIS_UNSET` | A band is set but `credit_basis` is null |
| `CREDIT_BASIS_RATE_MISSING` | No open `CreditRate` for `credit_basis` |
| `CREDIT_BASIS_NO_INR_PRICE` | The basis row's `price_per_minute_inr` is null — the sale cannot be priced |
| `CREDIT_BASIS_PER_EVENT` | The basis row is `per_event` — no per-minute meaning |
| `FREE_MINUTES_PLAN_UNCAPPED` | `price_per_minute_inr = 0` with no `max_minutes` |

`warnings()` gains `RUPEES_PER_CREDIT_INCONSISTENT`, `LADDER_OPEN_BOTTOM_MISSING`,
`LADDER_OPEN_TOP_MISSING`, `RATE_MISSING_INR_PRICE` (a non-basis row with no rupee price
— the pricing page cannot quote that tier).

**`BAND_PRICE_CLIFF` was specced and then dropped.** Implementing it showed it fires on
every legitimate volume ladder: no cliff at a boundary requires
`rate_next >= rate_prev * max/(max+1)`, essentially `rate_next >= rate_prev`, which means
a ladder with no volume discount. `plan_selection.boundary_prices()` reports the four
numbers at each boundary instead, as information on the review screen.

`report()` gains a `ladder` block: every active band in this plan's cycle, the price at
each boundary on both sides, and the problems above — the review screen should show the
ladder the plan is joining, not the plan alone.

### `ops/services/plan/config.py::replace_rates`

**A rate edit can break a plan that is already active.** Activation checks the basis row,
but rates are edited afterwards, and closing or blanking the basis row leaves a live plan
that cannot price a sale. Refuse a rate write that would leave an active plan's basis row
missing, `per_event`, or without a rupee price.

`_rate_changed()` iterates `row.items()`, so `price_per_minute_inr` is picked up
automatically — a rupee price change correctly triggers a supersede.

### Serializers

- `ops/serializers/plan/rate.py` — `price_per_minute_inr`, required on create
- `ops/serializers/plan/plan.py:53` — the 3 new plan fields; `PlanListSerializer:137` gains
  a `band` display string (`"501–2,000 min"`) so the list is scannable

### `ops/services/plan/clone.py`

`_copyable_fields()` walks concrete fields, so the band and the rupee prices are copied
automatically — wrong in both directions. Two `_review_notes()` entries:

- **Same cycle** → `BAND_COLLIDES`: an exact duplicate band. Harmless as a draft, refused
  at activation, much better said now.
- **Cycle changed** → `BAND_NEEDS_CONVERSION`: 500 monthly minutes is not 500 yearly
  minutes, and `price_per_minute_inr` was copied verbatim — the same failure mode the
  existing `CYCLE_CHANGED` note already warns about for add-on block prices.

### Commands

- `onboard_custom_deal:299,323` — prompt for minutes when the plan is v2, default to the
  band minimum, pass to `activate(minutes=...)`. `credit_grant` still overrides everything,
  which is the point of the command
- `audit_billing_readiness:114` — add a ladder section: overlaps, gaps, and any active v2
  plan whose basis row has gone missing or lost its rupee price since activation

---

## 10. Phase 8 — dropping `Plan.allocated_credits`

**Not a removal — a relocation, finished.** The column moved to `Subscription` in Phase 1
and every writer moved in Phase 5. This phase moves the *readers* and drops what is left.

**The name never changes.** `snapshot["allocated_credits"]` and
`rules["allocated_credits"]` keep meaning what they have always meant, so there is no
alias, no rename, and no migration over the snapshots. Only the *source* of the number
moves.

The two questions the old column was answering get different homes:

| Question | Now answered by |
| :--- | :--- |
| *What did **this subscription** get?* | `subscription.allocated_credits` — the column |
| *What would **N minutes** get on this plan?* | `plan.credits_for(minutes)` — the method |

### 8.1 Pre-flight

The Phase 1 backfill should have left nothing behind, but confirm before dropping:

```sql
SELECT count(*) FROM onboard_subscription
WHERE  is_deleted = false
AND    allocated_credits IS NULL;
```

Must be `0`. Then tighten the column to `null=False, default=0` in the same migration that
drops the plan column.

### 8.2 Move the readers — the 25 sites

| Site | Change |
| :--- | :--- |
| `models/subscription.py:177` | Snapshot write — from `self.allocated_credits` |
| `models/subscription.py:213` | `rules` — resolve snapshot → column, **drop the plan fallback for this key** |
| `services/subscriptions.py:226,404` | Already `credits_for()` / column after Phase 5 |
| `services/pricing.py:304,305` | Already `credits_for(minutes)` after Phase 2 |
| `views/billing/subscription.py:117` | `plan.credits_for(minutes)` per eligible plan |
| `serializers/billing.py:143` | Now reads the subscription, not the plan |
| `serializers/plan.py:109,161` | **Drop.** A v2 plan has no single grant to report |
| `ops/serializers/plan/plan.py:56,85,137` | Drop; the `required: True` goes with it |
| `ops/services/plan/readiness.py:112` | `PAID_PLAN_NO_CREDITS` becomes "the basis row grants 0 credits/min" |
| `onboard_custom_deal.py:299,323,526` | Prompt defaults from `credits_for(minutes)`; the override still writes `allocated_credits` — same key |
| `audit_billing_readiness.py:114,124` | Report the band and basis instead of a fixed grant |
| `backfill_wallet_stamps.py:66` | Unchanged — `rules.get("allocated_credits")` still resolves |
| `admin/plan.py:95` | Drop from the plan fieldset; add it to the subscription admin |
| `models/plan.py:124` | The column itself, in 8.3 |

Two of these are worth calling out because they are *not* mechanical:

- **`serializers/plan.py:109,161`** — the public pricing API currently advertises a plan's
  grant. A v2 plan does not have one; it has a rate and a band. The field is dropped rather
  than faked, and `for-minutes/` is where a caller gets a real number.
- **`backfill_wallet_stamps.py:66`** — reads `rules.get("allocated_credits")` and needs no
  change at all, which is the clearest evidence the no-rename decision was the right one.

### 8.3 Drop the plan column

One migration, once 8.1 reads zero **and** no active plan is still legacy — `renew()`
calls `plan.credits_for()`, which falls through to this column for any plan without a
basis. Converting the five remaining plans is the precondition; waiting for their
subscribers to churn is not a plan.

---

## 11. Phase 9 — recalibration and removing `base_price`

Feature doc §9. Both are follow-ups; decide them deliberately rather than by omission.

### 11.1 Recalibration

| Field | Replacement | Resolution |
| :--- | :--- | :--- |
| `low_balance_threshold` | `low_balance_percent` | `ceil(subscription.allocated_credits × pct/100)` at activation and renewal |
| `max_rollover_credits` | `max_rollover_ratio` | `floor(subscription.allocated_credits × ratio)` in `rollover_amount()` |

Both follow the established pattern: new key from the snapshot, absolute column as
fallback. One change outside `onboard` — `wallet.services.wallet.set_plan_context()`
currently does `wallet.low_balance_threshold = plan.low_balance_threshold` and would take
the computed value as an argument.

Of the two, `max_rollover_credits` is the more urgent: a fixed cap of 3,500 forfeits
nothing from a 500-minute customer and silently burns 4,500 credits from a 2,000-minute one
at every renewal.

### 11.2 Removing `base_price`

Same argument as `allocated_credits`, one step behind because it drags more with it:

- `ck_plan_free_renewal_only_when_zero_cost` (`models/plan.py:200-211`) must be re-expressed
  as "every rate row is ₹0" — cross-table, so it becomes a readiness check rather than a
  database constraint
- `discount_amount` / `net_base_price` / `tax_amount` / `total_price` (lines 256-282)
  collapse into `price_components(minutes)`
- `ops/serializers/plan/plan.py:99-110` validates `renews_without_payment` against it
- 12 read sites across serializers, views and the admin

Worth doing. Not worth doing in the same change as everything else.

---

## 12. Test plan

Run with `--settings=config.settings.test_postgres` — the default SQLite test DB cannot
build these models.

**New — `onboard/tests/test_plan_minutes.py`**

- `contains_minutes` across all four null combinations, at both inclusive boundaries
- `credits_for` for `per_minute`, `per_second` (×60), `per_event` (raises), missing row,
  row with no rupee price
- `price_components` with discount and tax; legacy plan ignores minutes entirely
- `ck_plan_minutes_band_valid` rejects an inverted band

**New — `onboard/tests/test_plan_selection.py`**

- Exact match, both boundaries, open-below, open-above
- Overlap detected; a gap between two ranged bands detected; a contiguous ladder is clean
- Legacy plans never appear in the ladder and never collide with it
- `nearest_bands` for an unmatched quantity
- `rupees_per_credit` flags an inconsistent plan and passes a consistent one
- **Fixed quantity:** a `min == max` band matches only its own value; `is_fixed_quantity`
  is true; `is_minutes_based` is **also** true
- **Round-up:** on a pack ladder of 1,000 / 5,000 / 10,000, a request for 1,200 selects the
  5,000 pack (1,000 cannot cover it). `sold_minutes` is 5,000, and price *and* grant both
  use 5,000 — never the 1,200 that was asked for
- A gap adjacent to a pack is not reported; a gap between two ranged bands is blocking
- A pack overlapping an open-ended ranged band is blocking
- Above the largest pack → no match, and `nearest_bands` names it

**New — `onboard/tests/test_pricing_minutes.py`**

- `quote_plan` arithmetic to the paise; out-of-band and over-cap both raise
- The quote freezes `price_per_minute_inr` and `credits_per_minute`; **superseding the
  basis row between quote and activation does not change what is granted**
- `quote_plan_change` legacy→v2, v2→legacy, v2→v2, and **same plan, more minutes**
- `credit_impact` reports the v2 grant, not `allocated_credits`
- `bonus_days_for` uses the v2 daily rate

**Extended**

| File | Added |
| :--- | :--- |
| `test_subscription.py` | Snapshot holds the 4 new keys; `Subscription.allocated_credits` equals `minutes × basis` **and** equals the `CYCLE_ALLOCATION` ledger row; a `credit_grant` override is recorded on the column, not the derived number |
| `test_subscription_lifecycle.py` | Renewal repeats stored minutes, re-quotes at today's rate; a **legacy** renewal takes its grant from `subscription.allocated_credits`, not the plan |
| `test_billing_journeys.py` | Full v2 signup → activation → balance; a v2 upgrade |
| `test_billing_api_subscription.py` | `change-plan` same plan + higher minutes; `eligible-plans?minutes=` |
| `test_onboarding.py` | Minutes required for v2, refused for legacy, survives resume |
| `test_plan_status.py` | Activation refused on each of the 7 blocking codes |
| `ops/tests/test_plan_readiness.py` | The 7 blocking codes and 5 warnings |
| `ops/tests/test_plan_config_service.py` | A rate write cannot strand an active plan's basis |
| `ops/tests/test_plan_clone.py` | Both new review notes |
| `wallet/tests/` | `as_snapshot_entry()` still omits `price_per_minute_inr` |

**Regression, and the point of the whole design:** the existing billing suite must pass
**unmodified**. A test that needs editing is a signal that legacy behaviour changed, and
should be investigated rather than updated.

Per the working agreement in this repo, run affected modules while building
(`onboard.tests.test_plan_minutes`, then the billing files) rather than the full suite; one
full run at the end.

---

## 13. Rollout

Additive throughout — no cutover, no migration window.

```
1  migrate                  6 nullable columns, 1 constraint. Nothing behaves differently
2  deploy                   every plan is legacy; every path takes the legacy branch
3  seed v2 plans as DRAFTS   bands, basis, rate rows with both numbers, entitlements
4  review                    ops review screen -> ladder block and rupees-per-credit clean
5  activate                  the gate refuses an overlapping or gapped ladder
6  frontend                  slider + for-minutes/ + minutes on signup and change-plan
7  retire legacy plans       stops new signups; existing subscribers unaffected
8  Phase 8                   drop Plan.allocated_credits once step 7 is done and the
                            pre-flight query reads zero
```

Steps 5 and 6 are independent: a v2 plan can be active and simply not offered until the
frontend ships, because a plan is only reachable through a client that asks for it.

**Rollback** is retiring the v2 plans. Subscriptions already sold on them keep working —
their terms are in their own snapshots, and `Plan.on_delete=PROTECT` keeps the rows.

**No seed data.** Plans are created only from the super admin panel, and `json_files/`
carries no plan, rate or entitlement rows. A placeholder v2 seed existed briefly during
development and was removed: seeded plans would sit active beside the real catalogue, and a
seeded band can overlap a real one, which the activation gate would then refuse.

---

## 14. Risks

| Risk | Mitigation |
| :--- | :--- |
| A ladder gap reaches production and a customer's quantity sells nothing | Blocking at activation; `audit_billing_readiness` re-checks live plans |
| A rate edit strands an active plan's basis row | Guard in `replace_rates()`, §9. Activation alone is not enough — rates are edited later |
| Someone recomputes credits at activation instead of reading the frozen breakdown | Explicit test: supersede the basis row mid-flight, assert the grant is unchanged |
| `apply_plan_change` re-resolves a quantity that was explicitly paid for | **Found in testing.** `resolve_minutes()` now runs only when the caller passed nothing; an explicit quantity is applied verbatim. Test moves the band between order and payment and asserts the paid quantity survives |
| ₹/min at 4dp vs totals at 2dp produces a paise mismatch against Razorpay | `expected_paise()` already compares with zero tolerance. Quantize once, in `price_components()` |
| `price_per_minute_inr` leaks onto the call path | `as_snapshot_entry()` is explicitly unchanged, with a test asserting the key is absent |
| Dropping `Plan.allocated_credits` breaks `rules` for an old snapshot | Dissolved by design: `Subscription.allocated_credits` is backfilled in Phase 1, so `rules` has a source that is not the plan. Pre-flight query in §10.1 confirms it |
| `Subscription.allocated_credits` and the credit ledger disagree | Written from the same `allocation` expression, inside the same atomic block as `wallet.grant()`. Asserted by test |
| `renew()` on a legacy plan after the plan column is gone | Phase 5 already routes legacy renewals to `subscription.allocated_credits`. This is the last reader, and it closes before Phase 8 |
| The free-plan signup branch drifts further from `_apply_onboarding` | Fold them in Phase 4 |
| `activate()`'s re-entry guard treats a minutes upgrade as a duplicate | Explicit condition + test, §7 |

---

## 15. Open questions

1. **Answered by fixed-quantity packs.** A deliberate gap is now expressible — make the
   bands either side packs, and round-up covers the space between them. A gap between two
   *ranged* bands stays blocking, because there it is an accident that silently overcharges.
   What remains open is the top of a pack ladder: a request above the largest pack has no
   answer, and "contact sales" is a product decision rather than a default.
2. **`minutes_step`** — selling in blocks of 50. One column and a validator. Deferred, not
   refused.
3. **Phase 9 timing.** The calibration fields get worse the wider the bands are, so the
   answer depends on the first real ladder's spread.
4. **Does a downgrade in minutes need a floor?** Dropping 2,000 → 100 minutes at cycle end
   is legal today under the sign test. Whether that should also require moving plan is a
   commercial call.
5. **Should `rupees_per_credit` inconsistency ever block?** It is a warning here. If every
   real plan turns out to be internally consistent, promoting it to blocking removes a
   whole class of mispriced plan.

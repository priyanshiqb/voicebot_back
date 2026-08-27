# Plan Configuration API

Defining what is sold. **Base path:** `/api/v1/ops/plans/`

A plan is **not one record.** It is three models and 84 settings, and every one of them
has to be there before it can safely be offered to anybody.

| Model | Rows per plan | Settings |
|---|---:|---:|
| `Plan` | 1 | 20 |
| `PlanEntitlement` — one per resource | **7** | 7 each = 49 |
| `CreditRate` — one per call tier | **3** | 5 each = 15 |
| | **11** | **84** |

---

## Why the API is shaped like this

**A partly-configured plan is silently broken, not obviously broken:**

| Missing | What happens | When you find out |
|---|---|---|
| An entitlement row | `limit_for()` returns `None` and the check returns without comparing anything — **the resource is unlimited** | Never. There is no error; the cap simply is not there |
| A rate row | `resolve_rate()` raises rather than charging zero | **On a live call**, to a real customer |

`Plan.is_active` used to default to `True`, so a plan created through an API was
instantly sellable with neither. Three things now prevent that:

1. **A new plan is a `draft`** — `status` defaults to `DRAFT`, and `is_active` is
   derived from it.
2. **Create takes all 84 settings at once.** Nothing is defaulted on the wire.
3. **Activation is refused unless the plan is complete.** Not a warning — a refusal.

> **A default is a decision nobody made**, and both failures above come from exactly
> that. So a resource left unlimited arrives as `"included_quantity": null` — **present
> and null, not absent** — and "I chose unlimited" stops looking identical to "I forgot
> this row".

---

## Lifecycle

```
        ┌──────────────── activate (gated) ────────────────┐
        │                                                  │
     DRAFT ──── activate (gated) ────▶ ACTIVE ──── retire ────▶ RETIRED
        ▲                                                  │
        └──────────────────────────────────────────────────┘
                    (reactivate, same gate)
```

**Nothing is ever deleted.** A draft nobody finished is retired like anything else, so
the console needs one verb rather than three — and `RETIRED` reaches every state a
delete would without leaving subscriptions and audit rows pointing at something gone.

`RETIRED` is not terminal. A plan withdrawn a year ago comes back through the **same**
completeness gate, because a resource may have been added to the system since.

---

## Endpoints

| | | Permission |
|---|---|---|
| `GET` | `/ops/plans/` | read |
| `POST` | `/ops/plans/` | `plan.change` |
| `GET` | `/ops/plans/{id}/` | read |
| `PATCH` | `/ops/plans/{id}/` | `plan.change` |
| `PUT` | `/ops/plans/{id}/entitlements/` | `plan.change` |
| `PUT` | `/ops/plans/{id}/rates/` | `plan.change` |
| `GET` | `/ops/plans/{id}/readiness/` | read |
| `POST` | `/ops/plans/{id}/activate/` | `plan.change` |
| `POST` | `/ops/plans/{id}/retire/` | `plan.change` |
| `POST` | `/ops/plans/{id}/clone/` | `plan.change` |

**Every write requires a `reason`** in the body and leaves an audit row. Missing it is
`400 REASON_REQUIRED`.

---

## `GET /ops/plans/`

Every plan including drafts and retired ones, with the counts that make one safe to
judge.

```json
{
  "id": "c3e19c1d-…", "code": "starter", "name": "Starter",
  "status": "active", "billing_cycle": "monthly", "sort_order": 2,
  "base_price": "1000.00", "total_price": "1180.00",
  "allocated_credits": 1000, "is_most_popular": false,
  "subscriber_count": 0,
  "entitlements_configured": 7,
  "rates_configured": 3,
  "is_complete": true
}
```

**`entitlements_configured` and `rates_configured` are the columns that matter.** A plan
cannot reach `active` with anything but `7` and `3`, so a shortfall means a draft
somebody has not finished — or, on an active plan, a bug worth shouting about.

`subscriber_count` counts `active` **and `trialing`** subscriptions. A tenant on a trial
is on the plan, sees its limits and will be billed by its terms; leaving them out would
under-report the impact of an edit to precisely the tenants evaluating it.

Annotated in one query, not counted per row.

---

## `GET /ops/plans/{id}/`

The whole plan, in one payload — the editor loads once and holds all three models.
Three requests to render one screen would also be three chances to render it
half-loaded.

```jsonc
{
  "plan":         { /* 20 settings + 4 derived: discount_amount, net_base_price,
                       tax_amount, total_price */ },
  "entitlements": [ /* 7 rows */ ],
  "rates":        [ /* 3 rows */ ],
  "readiness":    { /* see below */ },
  "subscriber_count": 0
}
```

An entitlement row:

```json
{ "resource": "agent", "included_quantity": 2, "reset_period": "none",
  "is_addon_available": true, "price_per_additional": "500.00",
  "addon_step": 1, "max_additional": 10 }
```

A rate row:

```json
{ "operation": "conversation_high_latency", "credits_per_unit": 8,
  "unit": "per_minute", "rounding": "ceil", "minimum_units": 1, "notes": null }
```

---

## `POST /ops/plans/` — create

```jsonc
{
  "reason": "new tier for the enterprise segment",
  "plan":         { /* all 20 fields */ },
  "entitlements": [ /* all 7 resources, all 7 fields each */ ],
  "rates":        [ /* all 3 tiers, all 5 fields each */ ]
}
```

**Every field is required**, including the ones the model defaults. `sort_order` in
particular: the model defaults it to `0`, and every plan sharing that value orders
arbitrarily against the others on the pricing page.

Rejected if a resource is missing, a tier is missing, or any field is absent — **a
present `null` is an answer, an absent key is not.**

The plan is created as a **`draft`**, whatever was sent. Complete is not the same as
correct, and somebody still has to look at it.

| Code | When | `errors` |
|---|---|---|
| **201** | Created as a draft. Returns the whole plan. | — |
| **400** | A field, a resource or a tier is missing. | field detail |
| **400** | No `reason`. | `REASON_REQUIRED` |
| **409** | The database refused the combination. | `CONSTRAINT_VIOLATION` |
| **403** | Not a manager, and `plan.change` was not granted. | — |

> **Atomic.** A create that wrote the plan row and then failed on the fourth
> entitlement would leave behind precisely the half-configured thing this endpoint
> exists to prevent.

---

## `PATCH /ops/plans/{id}/` — the plan's own fields

Partial. `status` is **read-only** here — the lifecycle moves through `activate` and
`retire`, which check completeness and count subscribers, and letting a PATCH set it
would be a way round both.

Two constraints are caught here as sentences rather than reaching the database as an
opaque 500:

| Rejected | Message |
|---|---|
| `renews_without_payment` on a priced plan | *"Only a ₹0 plan can renew without payment…"* |
| An unlimited resource marked purchasable | *"An unlimited allowance cannot also be purchasable — set a quantity, or turn add-ons off."* |

---

## `PUT /ops/plans/{id}/entitlements/` — the capacity grid

All seven rows at once.

```json
{ "reason": "raising storage across the board", "entitlements": [ /* 7 rows */ ] }
```

**`PUT` on the collection, not `PATCH` per row.** The grid is one screen saved once;
seven round trips would let a plan exist half-updated between them, and the
completeness rule is about the set rather than any single row.

Rows are **updated in place**, so primary keys do not churn.

| Field | Note |
|---|---|
| `included_quantity` | **`null` = unlimited.** Visually distinct from `0`, which means none |
| `reset_period` | `none` is a standing total; anything else refills, counted from when the act happened |
| `price_per_additional` | **Per cycle**, not one-off |
| `addon_step` | Minimum 1 |
| `max_additional` | `null` = uncapped. **This is what protects the tiers** |

---

## `PUT /ops/plans/{id}/rates/` — the rate card

All three tiers at once. **What happens depends on the plan's status:**

| Status | Behaviour | Message |
|---|---|---|
| `draft` | Edits the open rows **in place** | `Rate card updated` |
| `active` / `retired` | **Supersedes** — closes each changed row and inserts a replacement | `Rate card superseded` |

A draft has no subscriber whose wallet carries a stamp of these rates, so there is no
history to preserve and a version boundary would mean nothing. A live plan supersedes,
which is what keeps every historical charge reproducible.

**Only changed tiers supersede.** Sending the card back with one tier edited closes one
row, not three — otherwise every save would fill the history with boundaries that say
nothing, and a real rate change would be indistinguishable from a save.

> Existing subscribers are unaffected either way. Their card was frozen onto their
> wallet when they subscribed, so **a rate change never re-prices a call already made,
> or a customer already on a cycle.**

---

## `GET /ops/plans/{id}/readiness/`

What is missing, and what looks wrong.

```json
{
  "status": "active", "can_activate": true,
  "entitlements_configured": 7, "entitlements_required": 7,
  "rates_configured": 3, "rates_required": 3,
  "blocking": [],
  "warnings": [
    { "code": "CAMPAIGNS_SOLD_AS_ADDON", "resource": "campaign_monthly",
      "message": "Campaigns are purchasable. Add-ons are prorated by time remaining, so a subscriber buying late in the window pays a fraction of the price for the whole allowance." }
  ]
}
```

### Blocking — refuses activation

| Code | Why it blocks |
|---|---|
| `MISSING_ENTITLEMENTS` | A resource with no allowance is **not limited to zero — it is not limited at all** |
| `MISSING_RATES` | Calls on an unpriced tier fail at the point of the call |

### Warnings — never block

Every one is accepted by the database and is a legitimate configuration in *some*
business, so refusing them would be wrong more often than the mistake it prevents.

| Code | Flags |
|---|---|
| `DISCOUNT_OVER_100` | Produces a negative price. The column accepts to 999.99 |
| `PAID_PLAN_NO_CREDITS` | Charged for, but a subscriber cannot make a call |
| `TOP_UPS_DISABLED` | ₹0 per credit — upgrading is the only route to more |
| `NO_SORT_ORDER` | Ties with every other plan at 0 |
| `ADDON_PRICED_AT_ZERO` | Capacity purchasable for nothing |
| `ADDON_UNCAPPED` | A subscriber can assemble the plan above at add-on prices |
| `CAMPAIGN_NOT_WINDOWED` | Deleting a campaign would hand the allowance back |
| `CAMPAIGNS_SOLD_AS_ADDON` | Prorated pricing misprices a count inside a window |
| `TIER_PRICED_AT_ZERO` | Those minutes cost money and earn nothing |
| `YEARLY_ADDONS_LOOK_MONTHLY` | **The 12× mispricing** — block prices are per cycle |

---

## `POST /ops/plans/{id}/activate/`

Makes a plan sellable. **Refused unless complete** — this is the gate the module is
built around.

```json
{ "reason": "launching the enterprise tier" }
```

| Code | When |
|---|---|
| **200** | Now `active`. Returns the whole plan |
| **409 `PLAN_NOT_READY`** | Incomplete. `data.blocking` names what is missing |
| **400 `REASON_REQUIRED`** | No reason given |

A refused activation is **recorded as a failed audit row.** An operator repeatedly
failing to activate is a signal, and it disappears if only successes are kept.

---

## `POST /ops/plans/{id}/retire/`

Withdraws a plan. **Stops new subscriptions and nothing else.**

Not refused when subscribers remain, because there is nothing to protect them from:
their terms are frozen on their own subscription, not read from the catalogue row. They
keep the plan, keep their price and keep renewing on it.

The message names the count, so the console can say what just happened rather than
leaving somebody to wonder:

```json
{ "message": "Starter withdrawn. 12 tenant(s) stay on it and keep their current terms." }
```

---

## `POST /ops/plans/{id}/clone/`

Duplicates a plan — all three models — as a new draft.

```json
{ "reason": "yearly version", "code": "starter_yearly",
  "name": "Starter Yearly", "billing_cycle": "yearly", "sort_order": 3 }
```

`code` and `name` are unique, so a copy cannot inherit them. `billing_cycle` and
`sort_order` are optional.

**Why this exists rather than "copy the fields by hand":** a yearly plan is a *separate
plan* with 31 mostly-identical fields, and retyping them is exactly how one ends up
carrying a **monthly** plan's add-on prices. Block prices are charged per cycle, so ₹199
per 10 GB buys a month on one plan and a whole year on the other — the same capacity for
a twelfth of the money.

> **Prices are copied verbatim, never converted.** Twelve times is arithmetic, but
> whether an annual price should carry the same discount as the plan is a commercial
> decision. **A wrong number somebody believes was calculated is harder to spot than a
> blank one.**

The response carries a `review` list naming what the copy specifically gets wrong:

| Code | Says |
|---|---|
| `CYCLE_CHANGED` | Every price was copied as-is and now buys a different amount of time |
| `ADDON_PRICES_NEED_REVIEW` | Names each add-on still carrying the original's block price |
| `SORT_ORDER_COLLIDES` | Both plans sit at the same order |

Two nets catch the mispricing: `clone` says it at the moment of copying, `readiness`
says it again at activation, when the source is no longer in mind.

---

## The public plans endpoint is read-only

`/api/v1/onboard/plans/` serves the **public pricing page** — `AllowAny` on read. It
used to accept writes from a superuser; it no longer accepts them from anybody.

That was a second door into the catalogue with **no audit trail and no completeness
gate**: a plan created through it was instantly `is_active` with zero entitlements and
zero rates — unlimited and unbillable at the same time, and neither failure raises
anything.

Blocked at the **verb** (`http_method_names`) rather than the permission, because a
permission check is one override away from being loosened by somebody who does not know
why it is there. `POST`, `PUT`, `PATCH`, `DELETE` and the inherited `bulk_delete` all
return **405**, superuser included.

**The catalogue now has exactly one write path.**

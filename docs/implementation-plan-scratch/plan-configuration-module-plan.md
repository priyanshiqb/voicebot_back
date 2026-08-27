# Plan Configuration — Implementation Plan

Module 1 of the admin console: everything commercial needs to define what is sold.
**The only module that blocks go-live** — nothing can be sold until plans exist.

A plan is **not one record**. It is three models and 84 settings, and every one of them
has to be there before the plan can safely be offered to anybody.

| | Model | Rows per plan | Settings |
| :--- | :--- | ---: | ---: |
| Plan | `onboard.Plan` | 1 | 21 |
| Capacity | `onboard.PlanEntitlement` | **7** — one per resource | 6 each = 42 |
| Rate card | `wallet.CreditRate` | **3** — one per call tier | 7 each = 21 |
| | | | **84** |

---

## 1. The problem this module has to solve

**A partly-configured plan is silently broken, not obviously broken.** Verified against
the code:

| Missing | What happens | When you find out |
| :--- | :--- | :--- |
| An entitlement row | `limit_for()` returns `None`, `check()` returns immediately — **the resource is unlimited** | Never. There is no error; the cap simply is not there |
| A rate row | `resolve_rate()` raises `RateNotConfigured` — deliberately loud rather than charging zero | **On a live call**, to a real customer |

And `Plan.is_active` **defaults to `True`**. So a plan created through an API is
immediately sellable with zero entitlements and zero rates: unlimited everything, and no
call can be priced.

The five plans in the database today are all `7/7` and `3/3` — because they were built
by hand, carefully, by people who knew. That is not a control.

> **The core design decision: a plan cannot go live until it is complete.** Not a
> warning on a list, not a checklist somebody is trusted to finish — activation is
> refused while any of the 11 rows are missing. That turns both silent failures above
> into states the system will not enter.

---

## 2. The configuration flow

One flow, four steps, and the plan is a **draft** throughout.

```
   ┌─ 1. PLAN ─────────┐   ┌─ 2. CAPACITY ────┐   ┌─ 3. RATE CARD ───┐   ┌─ 4. REVIEW ──┐
   │ 21 fields         │   │ 7 resources      │   │ 3 call tiers     │   │ everything   │
   │ price · cycle     │──▶│ included · reset │──▶│ credits · unit   │──▶│ + impact     │
   │ credits · rollover│   │ add-on · cap     │   │ rounding · min   │   │ → ACTIVATE   │
   └───────────────────┘   └──────────────────┘   └──────────────────┘   └──────────────┘
        draft                    draft                   draft              active
```

**Creation covers every field.** ✅ decided — `POST /ops/plans/` takes the whole
configuration in one payload: the 21 plan fields, all 7 entitlement rows, all 3 rate
rows. Nothing is defaulted silently on the wire.

That is stricter than "create the rows and let them be filled in later", and
deliberately so. A default is a decision nobody made, and the two failures in §1 are both
caused by a decision nobody made. If commercial wants a resource unlimited they send
`included_quantity: null` — **present and null, not absent.** The serializer requires the
key, so "I chose unlimited" and "I forgot this row" stop looking identical.

**After creation, steps are revisited in any order.** It is a checklist, not a wizard
that traps you — commercial will change the price after seeing the rate card, and will
want to save what they have without re-sending everything.

**The plan stays `DRAFT` until activated**, so a complete-but-wrong plan is still not
sellable while somebody is still thinking about it.

### The draft state — `Plan.status`, defaulting to `DRAFT` ✅ decided

```python
class Status(models.TextChoices):
    DRAFT = "draft", "Draft"        # never offered; still being configured
    ACTIVE = "active", "Active"     # offered to new and renewing customers
    RETIRED = "retired", "Retired"  # withdrawn; existing subscribers unaffected

status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
```

Every transition is allowed, in both directions:

```
        ┌──────────────── activate (gated) ────────────────┐
        │                                                  │
     DRAFT ──── activate (gated) ────▶ ACTIVE ──── retire ────▶ RETIRED
        ▲                                                  │
        └──────────────────────────────────────────────────┘
                    (reactivate, same gate)
```

**Nothing is ever deleted.** A draft nobody finished stays a draft; an unwanted one is
retired like anything else. A plan is the thing subscriptions point at and audit rows
name, so a delete verb buys tidiness at the cost of a dangling reference — and there is
no state a delete reaches that `RETIRED` does not.

**`RETIRED` is not terminal.** A plan withdrawn last quarter can come back, and comes
back through the same completeness gate — one retired a year ago may be missing a
resource added since.

`is_active` alone could not carry this. `is_active = False` means both *"draft, never
sold"* and *"retired, with 47 subscribers"* — indistinguishable, and the difference is
exactly what somebody needs to know before clicking either button.

**Defaulting to `DRAFT` is the point.** `Plan.is_active` defaults to `True`, so a plan
created through any API today is instantly sellable with no entitlements and no rates.
A new plan now starts unsellable and has to be *made* live.

> ⚠️ **`is_active` stays, and stays in sync.** It is inherited from `BaseModelCore` so it
> cannot be removed, and **six production call sites filter on it** — signup, eligible
> plans, preview-change, change-plan, update-plan, and the readiness audit. `status` is
> authoritative; `save()` derives `is_active = (status == ACTIVE)` so every one of those
> keeps working untouched.
>
> Two fields for one fact is normally how they drift — the derivation is what stops it,
> and it belongs in `save()` rather than in each caller.

**Migration**: one `AddField` plus a data migration setting every existing plan to
`ACTIVE` where `is_active` is true, `RETIRED` where it is not. Five rows today.

---

## 3. The screens

### 3.1 Plan list — completeness is a column, not a footnote

```
NAME             CYCLE    PRICE    CREDITS   STATE     SUBS   CAPACITY   RATES
Starter          monthly  ₹999     1,000     active    47     7/7        3/3
Starter Yearly   yearly   ₹9,990   12,000    active    12     7/7        3/3
Pro              monthly  ₹2,999   3,500     draft     0      6/7  ⚠     3/3
Legacy Basic     monthly  ₹499     500       retired   3      7/7        3/3
```

`CAPACITY` and `RATES` are the columns that exist because of §1. A plan cannot reach
`active` with anything but `7/7` and `3/3`, so a warning there means a draft somebody
has not finished — or, if it ever appears on an active plan, a bug worth shouting about.

### 3.2 Step 1 — Plan, 21 fields in five groups

Identity · price · cycle · credits · rollover · lapse, exactly as the commercial brief
groups them. The price arithmetic is derived live, because
`list − discount + tax = charged` is what people get wrong:

```
list price      ₹2,999
− discount 10%  ₹2,699.10
+ tax 18%       ₹3,184.94   ← charged
```

### 3.3 Step 2 — Capacity, a 7 × 6 grid

One row per resource. Not 42 fields — a grid, because that is how the decisions are
actually made ("how generous am I being across the board?").

| Column | Note for the UI |
| :--- | :--- |
| `included_quantity` | **Blank = unlimited.** Must be visually distinct from `0`, which means none |
| `reset_period` | The subtlest field in the system. `none` = standing total; anything else = refills |
| `is_addon_available` | Toggling on must reveal the three fields below it, and require a price |
| `price_per_additional` | **Per cycle**, not one-off. Say so on the label |
| `addon_step` | Minimum 1, enforced by a constraint |
| `max_additional` | Blank = uncapped. **This is what protects the tiers** |

Campaigns get a note in-place: `is_addon_available` must stay off, and `reset_period`
must not be `none`.

### 3.4 Step 3 — Rate card, 3 tiers × 7 settings

**Rates are versioned, and the editor has to respect that.**
`uniq_current_rate_per_plan_operation` permits one open row per (plan, operation), so
changing a rate is not an update: it closes the old row with `effective_to` and inserts a
new one. The Django admin already has `supersede_rates`; the API needs the same
semantics.

On a **draft** plan there are no subscribers, so a rate can be edited in place — nothing
has been stamped onto a wallet yet. On an **active** plan every edit supersedes. The
editor should say which it is doing.

> Existing subscribers keep the card frozen onto their wallet at subscribe time, so a
> rate change **never re-prices a call already made, or a customer already on a cycle.**
> Worth printing on the screen — it is the reassurance that makes editing safe.

### 3.5 Step 4 — Review and activate

The completeness gate, and the impact statement:

```
✓ Plan          Starter · monthly · ₹999 → ₹1,178.82 charged
✓ Capacity      7 of 7 resources configured
✓ Rate card     3 of 3 tiers priced
⚠ Warnings      storage add-on is purchasable at ₹0.00
                campaigns reset period is "none"

47 tenants are on this plan. Changes apply to new subscriptions and
renewals; all 47 keep their current terms until they renew.

                                              [ Activate ]
```

Warnings do not block. Missing rows do.

---

## 4. Endpoints

```
GET    /ops/plans/                       list, with completeness counts
POST   /ops/plans/                       create a DRAFT — plan + 7 entitlements + 3 rates,
                                         every field required, nothing defaulted
GET    /ops/plans/{id}/                  the whole configuration in one payload
PATCH  /ops/plans/{id}/                  the 21 plan fields
PUT    /ops/plans/{id}/entitlements/     all 7 rows at once
PUT    /ops/plans/{id}/rates/            all 3 tiers, superseding if live
GET    /ops/plans/{id}/readiness/        what is missing, what is suspicious
POST   /ops/plans/{id}/activate/         DRAFT -> ACTIVE. Refused unless complete
POST   /ops/plans/{id}/retire/           ACTIVE -> RETIRED. Stops new subscriptions only
POST   /ops/plans/{id}/clone/            duplicate as a DRAFT, optionally switching cycle
```

### Retiring

`ACTIVE → RETIRED` **stops new subscriptions and nothing else.** Existing subscribers
keep the plan, keep their frozen terms, and keep renewing on it — their subscription
snapshot is what governs them, not the catalogue row.

Not blocked when subscribers remain, because there is nothing to protect them from. The
response returns the count so the console can say *"12 tenants stay on this plan"* rather
than leaving somebody to wonder.

`RETIRED → ACTIVE` is allowed and goes through the same completeness gate as any other
activation — a plan retired a year ago may be missing a resource added since.

`DRAFT → RETIRED` is how an unwanted draft is put away. **There is no delete**, so the
console needs no third verb: retire is the one button, and it means the same thing
wherever it is pressed.

### The create payload

```jsonc
POST /ops/plans/
{
  "plan":         { /* all 21 fields */ },
  "entitlements": [ /* all 7 resources, all 6 fields each */ ],
  "rates":        [ /* all 3 tiers, all 7 fields each */ ]
}
```

Rejected if a resource is missing, a tier is missing, or any field is absent. **A
present `null` is an answer; an absent key is not.**

`sort_order` is among the required fields — the model defaults it to `0`, and a default
here means two plans ordering arbitrarily against each other on the pricing page.

**`PUT` on the collections, not per-row `PATCH`.** The seven entitlements are one grid
on one screen saved once; seven round trips would let a half-saved plan exist between
them.

**`GET /ops/plans/{id}/` returns all three models in one payload.** The editor loads
once and holds the whole plan — the alternative is three requests to render one screen.

Every write carries `plan.change` as `required_permission` and writes an
`OperatorAuditLog` row.

### Clone, and why it is not a nice-to-have

A yearly plan is *a separate plan* with 31 mostly-identical fields. Retyping them is
exactly how a yearly plan ends up carrying **monthly** add-on prices — the mispricing
already documented for commercial, where 20 GB costs ₹4,776/yr on one plan and ₹398/yr
on another.

Clone copies plan, all 7 entitlements and all 3 rates; blanks `code` and `name`; and when
the cycle changes, **flags every price for review** rather than converting it. Converting
would be guessing at a commercial decision.

---

## 5. Validation

### Constraints the database already enforces — surface as field errors, not 500s

| Constraint | Message |
| :--- | :--- |
| `ck_entitlement_no_addon_when_unlimited` | *"An unlimited allowance cannot also be purchasable — set a quantity or turn add-ons off."* |
| `ck_plan_free_renewal_only_when_zero_cost` | *"Only a ₹0 plan can renew without payment."* |
| `ck_entitlement_addon_step_positive` | *"Block size must be at least 1."* |
| `uniq_entitlement_per_plan_resource` | Cannot happen through `PUT` — the endpoint upserts |
| `uniq_current_rate_per_plan_operation` | Cannot happen — superseding closes the old row first |

### Blocks activation

- Fewer than 7 entitlement rows — **the resource would be unlimited**
- Fewer than 3 rate rows — **that tier's calls cannot be priced**

### Required on create

`sort_order` is **mandatory**, not defaulted. ✅ decided

The model defaults it to `0`, so every plan created without one lands on the same value
and the pricing page orders them by whatever the database happens to return — stable
enough in testing to look fine, and arbitrary enough to reorder itself on launch day.
Requiring it turns a silent tie into a decision somebody made.

The console warns, but does not block, when the value collides with another plan's.
Blocking would make reordering impossible: swapping two plans needs a moment where both
hold the same number.

### Warns, does not block

Every one of these is accepted by the database and is nearly always a mistake:

- `discount_percentage > 100` — accepted to 999.99, produces a negative price
- `is_addon_available` with `price_per_additional = 0` — sells capacity for nothing
- `campaign_monthly` with `reset_period = none` — makes an act a standing total, so
  deleting a campaign hands the allowance back
- A yearly plan whose add-on prices match a monthly plan's — the 12× mispricing
- `cost_per_additional_credit = 0` on a paid plan — silently disables top-ups
- `allocated_credits = 0` on a paid plan — the customer can make no calls

---

## 6. Where the code goes: a new `ops` app, owning no models

**No new models.** Every one already exists: `onboard.Plan`,
`onboard.PlanEntitlement`, `wallet.CreditRate`, `onboard.Subscription` for the impact
count, and `user.OperatorAuditLog` for the audit trail.

**One new field**, in `onboard`, not in `ops`: `Plan.status` (§2). It belongs where the
model lives — an app that owns no models should not start owning one field of somebody
else's.

It is justified by a boundary that can be **audited by inspection**. The console's
central safety rule is *never reuse the tenant-scoped viewsets* — those resolve the
tenant from `request.user.tenant_mapping`, and an operator has none. Scattered through
`onboard/views/` and `wallet/views/`, that rule holds by convention. In one directory
behind one router and one gate, "show me everything an operator can reach" is a
directory listing.

**Package-based throughout**, matching `onboard/views/billing/` and
`onboard/serializers/onboarding/`: every layer is a package whose `__init__.py`
re-exports, one file per concern. Nothing is a bare module that will need splitting
the first time a second concern arrives.

```
ops/
  __init__.py
  apps.py
  urls/
    __init__.py           # the single /api/v1/ops/ router, assembled from below
    plan.py               # plan routes
  permissions/
    __init__.py           # re-exports IsPlatformStaff, HasConsolePermission
  serializers/
    __init__.py
    plan/
      __init__.py
      plan.py             # the 21 plan fields
      entitlement.py      # the 7 x 6 grid
      rate.py             # the 3 x 7 rate card
      config.py           # the create payload: plan + entitlements + rates together
      readiness.py        # what is missing, what is suspicious
  services/
    __init__.py
    plan/
      __init__.py
      config.py           # create, update, the whole-plan read
      readiness.py        # completeness + warnings
      lifecycle.py        # activate, retire, reactivate
      clone.py
  views/
    __init__.py
    plan.py
  tests/
    __init__.py
    test_plan_crud.py
    test_plan_readiness.py
    test_plan_lifecycle.py
    test_plan_clone.py
    test_permissions.py
```

Three reasons this shape rather than four files:

**The serializers genuinely are four different things.** A write-capable entitlement
grid and a 21-field plan form share nothing but a model FK, and `config.py` composes
them for the create payload rather than any of them knowing about the others.

**Readiness is a service, not a view helper.** `activate` calls it, the list endpoint
calls it for its `7/7` column, and the console calls it directly for step 4. Three
callers is the point at which a private function in a view becomes a bug waiting to be
duplicated.

**Lifecycle is separate from config** because the transitions have rules and the fields
do not. `activate` refuses an incomplete plan; `retire` counts subscribers; `PATCH` on
the 21 fields does neither. Keeping them apart is what stops somebody adding a
completeness check to a field update because it happened to be in the same file.

No models means no migrations, which sidesteps the `AUTH_USER_MODEL` rewind hazard that
broke MG-10 when `user.0016` landed.

> ⚠️ **Do not extend the existing `PlanViewSet`.** It is the *public pricing page* API —
> `AllowAny` on read. Every field added there becomes public, and the console needs
> `max_additional`, retired plans and cost inputs. Two audiences, two endpoints, one set
> of models.
>
> ✅ **And it becomes read-only.** Its `IsSuperUser` write path is a second way to create
> plans, with no audit trail and no completeness gate — a superuser could still produce a
> plan that is instantly active and instantly unlimited, which is the exact failure this
> module exists to prevent. Writes move to `/ops/plans/`, where they are gated,
> completeness-checked and logged.

---

## 7. Work breakdown

**✅ Every step below is built.** The endpoints are documented at
[Plan Configuration API](../apiDocs/ops/plans.md); the access model they sit behind is
at [Admin Console API](../apiDocs/ops/README.md).

| | Step | Delivers |
| :--- | :--- | :--- |
| 0 | `Plan.status` field + data migration; `is_active` derived in `save()` | A new plan starts unsellable |
| 1 | `ops` app + `/api/v1/ops/` router behind `IsPlatformStaff` | The namespace |
| 2 | `GET` list + detail, with completeness counts | Commercial can see what exists |
| 3 | `POST` create — full payload, every field required | **A plan that cannot be silently permissive** |
| 4 | `PATCH` plan fields, constraint messages | Step 1 of the flow |
| 5 | `PUT` entitlements | Step 2 |
| 6 | `PUT` rates, with supersede semantics | Step 3 |
| 7 | `readiness` + `activate` gate | Step 4 — **and the guarantee in §1** |
| 8 | `retire`, `clone` | The lifecycle around it |
| 9 | Warnings | The mistakes the database will accept |
| 10 | Make the public `PlanViewSet` read-only | Closes the unaudited write path |

**Steps 0–7 are the module.** Anything less and a plan can still reach customers
half-configured, which is the problem this module exists to prevent. Step 10 can follow,
but until it lands there is still a superuser route around every guarantee above.

---

## 8. Decisions — resolved

| | Decision | Consequence |
| :--- | :--- | :--- |
| Public `PlanViewSet` | **Read-only** | Removes the only unaudited, ungated write path to the catalogue |
| Draft state | **`Plan.status`, default `DRAFT`** | A new plan starts unsellable. `is_active` derived in `save()` so six existing call sites are untouched |
| Retiring with subscribers | **Allowed — stops new subscriptions only** | Existing subscribers keep their frozen terms; the count is returned so the console can say so |
| Completeness at creation | **Every field required in the create payload** | A present `null` is an answer, an absent key is not. Neither of the §1 silent failures can be created |
| Deleting a draft | **No delete verb — retire it** | Nothing is ever deleted; `RETIRED` reaches every state a delete would, without dangling references |
| Reactivating a retired plan | **Allowed, through the same gate** | A plan retired a year ago may be missing a resource added since |
| `sort_order` | **Mandatory on create** | No silent ties on the pricing page. Collisions warn, so reordering stays possible |

### Nothing is open

Every decision this plan raised has been made. What is left is building it.

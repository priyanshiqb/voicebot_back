# The campaign lifecycle, and how it is counted

How a campaign moves through its states, where billing observes it, and what
commercial can set per plan.

**Sections 1-7** are how a campaign behaves and where billing observes it.
**Sections 8-12** are the configuration reference: what to set, where, and how to read
usage back.

| | |
| :--- | :--- |
| [1. The state machine](#1-the-state-machine) | Statuses, transitions, who writes each |
| [2. The one field billing depends on](#2-the-one-field-billing-depends-on) | Why `started_at` is load-bearing |
| [3. Where the allowance is checked](#3-where-the-allowance-is-checked) | Three checkpoints, and why each exists |
| [4. The counting rule](#4-the-counting-rule) | What consumes a slot and what gives it back |
| [5. Keeping the record intact](#5-keeping-the-record-intact) | The delete guards, and the manager that enforces them |
| [6. Known gap: `COMPLETED`](#6-known-gap-nothing-sets-completed) | Nothing sets it. What that does and does not break |
| [7. The credit side](#7-the-credit-side-which-is-separate) | Why campaigns and credits never talk |
| [8. What commercial sets](#8-what-commercial-sets-per-plan) | The two settings that matter, and four that do not |
| [9. How it reaches a tenant](#9-how-the-setting-reaches-a-tenant) | The snapshot, and the window anchor |
| [10. Every case, measured](#10-every-case-measured) | Probe output for all nine transitions |
| [11. Reading usage live](#11-reading-usage-live) | The endpoint, the payload, the cost |
| [12. A naming wart](#12-a-naming-wart-left-alone-deliberately) | Why `campaign_monthly` still says monthly |

---

## 1. The state machine

```
                                    ┌──► CANCELLED   (terminal, from any non-terminal state)
                                    │
  DRAFT ──launch──┬─► SCHEDULED ──due──► MATERIALIZING ──┬─► RUNNING ⇄ PAUSED
                  │                                      │
                  └────────► MATERIALIZING               └─► FAILED   (terminal)
                             (launch with no start time)
```

| Status | Meaning | Written by |
| :--- | :--- | :--- |
| `DRAFT` | Being built. Editable | create |
| `SCHEDULED` | Launched with a future start time | `POST /launch/` |
| `MATERIALIZING` | Audience being resolved | `POST /launch/` (immediate) or `start_due_campaigns` |
| `RUNNING` | Handed to the runner, calls going out | `materialize_campaign` |
| `PAUSED` | Runner told to stop pulling | `POST /pause/` |
| `COMPLETED` | Every member finished | **nothing — see §6** |
| `CANCELLED` | Stopped by a person; pending members cancelled with it | `POST /cancel/` |
| `FAILED` | Materialisation could not produce a callable audience | `_fail()` |

Groupings on the model:

- `OPEN_STATUSES = [DRAFT, SCHEDULED]` — not yet handed over; still editable and launchable.
- `LIVE_STATUSES = [MATERIALIZING, HANDING_OFF, RUNNING, PAUSED]` — cannot be deleted.
- `TERMINAL_STATUSES = [COMPLETED, CANCELLED, FAILED]` — cannot be cancelled again.

`handoff_status` (`PENDING → SENT → ACKED`, or `FAILED`) tracks the runner conversation
separately, so a retryable handoff problem is never confused with a campaign that
genuinely failed.

`/launch/` refuses up front rather than accepting and failing later: it rejects a
campaign whose bot has been deleted or deactivated since creation, and one whose
audience resolves to zero callable contacts. Both are re-checked at launch, not just at
create, because the world moves between the two.

---

## 2. The one field billing depends on

`Campaign.started_at` is written in exactly one place — `core/services/campaigns.py:182`,
at the end of `materialize_campaign`, in the same `save()` that sets `RUNNING`.

That single writer is what makes it trustworthy as a billing fact. It means
**"calls actually went out for this campaign"**, and both the counting rule and the
deletion guard hang off it.

A campaign that is launched but fails to materialise never gets a `started_at` — it
lands in `FAILED` and is not billable, which is correct: nobody was called.

---

## 3. Where the allowance is checked

Three points, deliberately:

| When | Where | Asks | On refusal |
| :--- | :--- | :--- | :--- |
| **Create** | `CampaignViewSet.entitlement_resource` | `used + 1 ≤ limit` | 402 at `POST /campaigns/` |
| **Immediate launch** | `CampaignViewSet.launch` | `used ≤ limit` | 402 at `POST /launch/` |
| **Scheduled start** | `_within_campaign_allowance` in `start_due_campaigns` | `used ≤ limit` | logged; campaign left `SCHEDULED` |

Create-time is early feedback — refusing at the point the customer types the name is far
better than accepting the campaign and refusing it silently at 9am.

The two start-time checks use `requested=0`, not `1`: the campaign already exists, so it
is already inside `used`. Asking for one more would refuse the last campaign of every
allowance.

The scheduled check exists because a campaign created in one window can be scheduled to
start in another. Without it, a customer could create twelve campaigns in January and
have them fire one a month all year against a single month's allowance.

Both start-time checks **fail open** — an exception in the quota lookup logs and lets the
campaign run. A billing query must never be the reason a paid-for campaign silently does
not go out.

---

## 4. The counting rule

```python
Campaign.all_objects
    .filter(tenant_id=tenant_id, created_at__gte=window_start)
    .filter(_campaign_consumes_allowance())
    .count()

# where the rule, defined once in onboard/services/entitlements.py, is:
Q(started_at__isnull=False) | (
    Q(is_deleted=False) & ~Q(status__in=Campaign.TERMINAL_STATUSES)
)
```

Three decisions in that query:

**`created_at`, not `started_at`.** The window is anchored to when the campaign was made.
A campaign created in this window but not yet started still counts — otherwise a customer
could stack up drafts and fire them all the moment the window turns over.

**`all_objects`, not `objects`.** Soft-deleted rows are visible to the count.

**`started_at`, not how the campaign ended.** That is the whole rule, and it cuts both
ways:

| State | Counts? | Why |
| :--- | :--- | :--- |
| Ever ran — whatever happened after | **yes, permanently** | The calls went out. Deleting or cancelling cannot un-call them |
| Draft or scheduled, still alive | **yes** | A claim on the allowance. The limit should be felt when a campaign is set up, not after an audience has been built |
| Mid-flight (`MATERIALIZING`, `HANDING_OFF`) | **yes** | No `started_at` yet, but not terminal either |
| Deleted before it ran | no | A mistyped draft, thrown away |
| **Cancelled before it ran** | no | The customer changed their mind; nothing was dialled |
| **Failed to materialise** | no | The bot was gone, or the audience resolved to nobody |

The first row closed a real bypass. Before it, the count was over live rows only:

```
create 3 (limit 3)  → used = 3, blocked
soft-delete all 3   → used = 0
create 3 more       → used = 3
```

Unlimited campaigns from a limit of three, free, by pressing delete.

The last two rows are the other half of the same idea. Keying the exemption on *deletion*
left cancelled and failed campaigns holding a slot for something that never happened: a
customer who cancelled before launch kept paying for it, and one whose campaign failed
because their bot had been deleted had to notice and tidy up by hand to get the slot back.
Keying it on `started_at` makes all three routes to "nothing was dialled" behave the same.

**The refund is not free re-entry.** Reclaiming the slot means passing the check again —
`/launch/` re-checks both the allowance and the credit balance. A customer at their limit
who cancels one campaign can start one more, not two.

---

## 5. Keeping the record intact

Once `started_at` decides who gets charged, the `Campaign` table *is* the billing ledger:
one row per act, timestamped, and unremovable once it has run. Three things enforce that.

**`soft_delete()` refuses in two cases.**

1. `status in LIVE_STATUSES` — pre-existing. You cannot delete a campaign mid-flight.
2. `started_at is not None` — a campaign that has ever run is part of the billing record
   and cannot be deleted at all, at any later point.

> *"This campaign has already run and forms part of your billing record, so it cannot be
> deleted. You can archive it instead."*

The guard is on the **model**, not the view, so `bulk_delete` (which loops
`obj.soft_delete()`), the admin, management commands and any future endpoint all inherit
it. On the view it would have left every other door open.

**`all_objects` no longer permits a bulk hard delete.** `BaseModelCore` declares it as a
plain `models.Manager`, whose `.delete()` is a real `DELETE` that raises nothing — so one
line could have erased the `started_at` history the entire allowance rests on. `Campaign`
now overrides it with `BaseQuerySet.as_manager()`, which keeps soft-deleted rows visible
(the count needs them) while refusing the hard delete exactly as `objects` already does.

**`Meta.default_manager_name = "objects"`.** Declaring `all_objects` on the model makes it
the newest manager, and Django would otherwise promote it to `_default_manager` — which
the admin, related-object lookups and `dumpdata` all read through. Left alone, that would
have quietly started showing soft-deleted campaigns in all three.

Drafts and campaigns that never ran are still freely deletable, and still correctly stop
counting.

---

## 6. Known gap: nothing sets `COMPLETED`

`CampaignMember` reaches `COMPLETED`; the campaign itself never does. Nothing in the
codebase assigns `Campaign.Status.COMPLETED`.

Consequences today:

- A campaign that has finished all its calls sits in `RUNNING` indefinitely, and the
  customer sees "Running" for a campaign that ended weeks ago.
- Because `RUNNING` is in `LIVE_STATUSES`, it also stays undeletable — which happens to be
  the correct billing outcome, but for the wrong reason.
- Nothing else breaks: the counting rule uses `started_at`, and the runner reads
  `WORKABLE_STATUSES = [RUNNING, HANDING_OFF]` and simply finds no pending members.

The fix is small — have `recompute_counts()` flip to `COMPLETED` when `pending_count` and
`in_progress_count` both reach zero on a `RUNNING` campaign — but it is a product
decision (does a campaign whose 200 members all failed count as completed?), so it is
flagged rather than assumed.

---

## 7. The credit side, which is separate

Campaign capacity and call credits are different meters and do not talk to each other.

- `_has_credit_for_campaign` is a **pre-flight** on the scheduled path: can this tenant
  afford at least one call on this bot's tier? It stops a campaign starting into an empty
  wallet. It reserves nothing.
- The actual charge happens per call, when `AgentCallLog` lands, at the rate frozen on the
  wallet's `rate_snapshot`.

So the campaign allowance controls *how many campaigns*; credits control *how many
minutes*. A customer can be inside their campaign allowance and still unable to place a
call, and vice versa.

---

## 8. What commercial sets, per plan

One row in `onboard_plan_entitlement` where `resource = campaign_monthly`. Seven columns
on it, but **only two matter for campaigns today**.

| Column | What it does | For campaigns |
| :--- | :--- | :--- |
| `included_quantity` | How many the plan grants. `NULL` = unlimited | **Set this** |
| `reset_period` | What that number *means* | **Set this** |
| `is_addon_available` | Whether the tenant may buy beyond the included amount | Must stay `false` — see §8.3 |
| `price_per_additional` | Pre-tax ₹ per block, charged **every cycle** | Inert while add-ons are off |
| `addon_step` | Block size an add-on is sold in | Inert |
| `max_additional` | Ceiling on extras, in units not blocks | Inert |

> ⚠️ **The seed plans carry dead add-on values.** The campaign row ships with
> `price_per_additional=150.00`, `addon_step=5`, `max_additional=50` — none of which is
> read, because `is_addon_available` is `false`. Do not take their presence as evidence
> that campaigns are sellable. If campaign add-ons are ever switched on, those numbers
> need deciding rather than inheriting.

### 8.1 `included_quantity`

How many campaigns the plan grants per window. `NULL` means unlimited — and a DB
constraint (`ck_entitlement_no_addon_when_unlimited`) refuses unlimited *plus* add-ons,
because selling more of something already uncapped is incoherent.

### 8.2 `reset_period` — the more important of the two

| Value | Meaning |
| :--- | :--- |
| `none` | A standing total. Three campaigns **ever** |
| `daily` | Refills every 24h from the anchor |
| `weekly` | Every 7 days |
| `monthly` | Calendar month, so a window opening on the 31st closes on the 30th or 28th rather than drifting |
| `quarterly` | 3 calendar months |
| `half_yearly` | 6 calendar months |
| `yearly` | 12 calendar months |

**The period is half of what the number means.** Ten a month and ten a year are the same
figure and completely different offers. `monthly` makes the allowance a *quota*; `daily`
on the same number makes it a *pace* — three a day means nobody fires a month's worth in
an afternoon.

`none` would make campaigns a standing total, where deleting one hands the slot back. That
is wrong for an act rather than a holding, and migration `0027` sets every existing row to
`monthly` — but the option exists because the same machinery serves all seven resources.

### 8.3 Why `is_addon_available` has to stay off

Add-ons are **prorated by time remaining**, which is right for a quantity held over time
and wrong for a count inside a window. A tenant buying storage on day 20 of 30 pays a third
of the price and gets a third of a month's storage. A tenant buying campaigns on day 20
would pay a third of the price and receive **the whole allowance**, then have it refill ten
days later.

Selling campaigns needs flat pricing rather than prorated. That is a small change, but it
has to be asked for — so today a tenant at their campaign limit must upgrade.

### 8.4 Where to set it

Django admin, two routes:

* **Plans → the plan → Plan Entitlements inline** — the normal route, editing a whole plan
  at once.
* **Plan Entitlements** as its own list — filterable by `resource` and `reset_period`,
  useful for auditing one setting across every plan.

Both surface `reset_period`. There is deliberately **no API for editing plan config**: it
is commercial configuration, not tenant self-service.

---

## 9. How the setting reaches a tenant

At subscribe **and at every renewal**, both values are stamped onto
`Subscription.plan_snapshot`:

```json
{ "entitlements": { "campaign_monthly": { "included": 5, "reset_period": "monthly" } } }
```

Everything downstream reads the snapshot, never the live plan. So editing a plan changes
what **new and renewing** tenants get, and nobody's allowance moves under them mid-cycle.

`Subscription.entitlement_reset_period()` and `_entitlement_entry()` read **both** the new
dict shape and the old bare-number shape, so subscriptions snapshotted before the
`reset_period` migration keep working and are treated as `none` until they renew.

### The window anchor

Anchored to `subscription.created_at`, **not the calendar**, and the anchor is stable
across renewals — a tenant who signed up on the 26th has windows that turn over on the
26th.

Calendar anchoring is what would let a tenant whose cycle runs 5 Aug – 4 Sep collect two
monthly allowances inside one billing cycle, and twelve inside a yearly one. Anchoring to
the subscription means every window sits wholly inside a cycle.

`window_start(period, anchor, now)` in `onboard/constants/reset_period.py` does the
arithmetic — fixed spans for daily/weekly, `relativedelta` for the month-based periods so
a window never drifts.

---

## 10. Every case, measured

A plan with `included_quantity=5`, `reset_period=monthly`. Actual output from a probe
against the real code, not a hand-worked example — rerun it any time with:

```bash
PYTHONPATH=. python docs/implementation-plan-scratch/campaign_usage_probe.py
```

It works inside a transaction it rolls back, so it is safe against a live database.

```
action                                        used  remaining
-------------------------------------------------------------
start: nothing created                           0          5
create a draft                                   1          4
second campaign runs                             2          3
  ... and completes                              2          3
delete the draft (never ran)                     1          4
create then CANCEL before launch                 1          4
create then FAIL to materialise                  1          4
launch one (mid-flight, no started_at)           2          3
CANCEL the one that already ran                  2          3
```

The last four lines are the whole rule:

* **Cancel and fail cost nothing** — provided nothing was dialled.
* **Mid-flight still holds its slot**, even though `started_at` is not stamped yet:
  `MATERIALIZING` is not terminal, so the campaign is still going to run.
* **Cancelling one that already ran gives nothing back.** The calls went out.

---

## 11. Reading usage live

There is **no separate usage endpoint**. Usage rides along with the subscription:

```
GET /api/v1/billing/subscription/   ->   data.entitlements[]
```

```json
{
  "resource": "campaign_monthly",
  "included": 5,
  "additional": 0,
  "limit": 5,
  "recurring_charge": "0.00",
  "used": 2,
  "remaining": 3
}
```

| Field | Meaning |
| :--- | :--- |
| `included` | From the plan snapshot. `null` = unlimited |
| `additional` | Bought as add-ons. Always `0` for campaigns |
| `limit` | `included + additional`. `null` = unlimited |
| `used` | Counted live, right now |
| `remaining` | `max(limit - used, 0)`. `null` = unlimited |
| `recurring_charge` | What the add-ons cost per cycle |

Every resource comes back in the same array, so one call drives a whole usage panel:

```
resource              included  extra  limit  used  left
--------------------------------------------------------
agent                        2      0      2     1     1
user                         3      0      3     1     2
contact                    100      0    100     0   100
knowledge_base               2      0      2     0     2
crawl_page                  50      0     50     0    50
campaign_monthly             5      0      5     2     3
storage_gb                   5      0      5     0     5
```

### In code

```python
from onboard.services import entitlements as entitlement_service

# every resource, with live usage
entitlement_service.summary(tenant_id, subscription, with_usage=True)

# one number
entitlement_service.current_usage(tenant_id, resource, subscription=subscription)
```

### Why `with_usage` is opt-in

It costs **one query per resource** — seven queries. Fine for a settings page, wrong for a
hot path, so `summary()` defaults to `with_usage=False` and returns limits alone.

The upside of computing it live is that there is **no counter column, nothing to reset,
and nothing to reconcile**. The count *is* the state rather than a projection of it, so it
cannot drift the way `Wallet.balance` can against its ledger — which is exactly why
`reconcile_wallets` has to exist for credits and has no counterpart here.

`audit_billing_readiness` uses the same call, so a CLI check and the API can never
disagree.

---

## 12. A naming wart, left alone deliberately

The enum value is literally `campaign_monthly`, which reads as a lie on a plan configured
`yearly`. Renaming it means a data migration across every `Subscription.plan_snapshot`
JSON blob and every `TenantEntitlement` row — real risk for a cosmetic gain. The
`EntitlementResource` docstring calls the name historical and explains what the resource
actually does.

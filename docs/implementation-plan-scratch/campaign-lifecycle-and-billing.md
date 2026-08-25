# The campaign lifecycle, and how it is counted

How a campaign moves through its states, where billing observes it, and what
commercial can set per plan.

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

Two values on the campaign entitlement:

| Setting | Effect |
| :--- | :--- |
| `included_quantity` | How many campaigns in a window. Blank = unlimited |
| `reset_period` | What that number *means*: `none`, `daily`, `weekly`, `monthly`, `quarterly`, `half_yearly`, `yearly` |

`none` would make it a standing total — three campaigns ever, and deleting one gives the
slot back. That is wrong for campaigns, and the migration sets every existing row to
`monthly`, but the option exists because the same machinery serves all seven resources.

Both values are **frozen onto `Subscription.plan_snapshot`** at subscribe and at each
renewal, as `{resource: {"included": n, "reset_period": p}}`. Editing the plan changes
what new and renewing customers get; nobody's allowance changes under them mid-cycle.

Windows are anchored to `subscription.created_at`, not the calendar, and the anchor is
stable across renewals — so a customer who signed up on the 9th has windows that turn
over on the 9th, and an allowance never straddles a billing cycle.

**Campaigns cannot be bought as an add-on** (`is_addon_available` is false on every plan).
Add-ons are prorated by time remaining, which is right for a quantity held over time and
wrong for a count inside a window: a customer buying on day 20 of 30 would pay a third of
the price and receive the whole allowance. Selling campaigns needs flat pricing — a small
change, but it has to be asked for.

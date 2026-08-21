# Module 03 — `cta`

**Call-to-action definition and tracking.** The BRD's measurement point.

| | |
|---|---|
| **BRD sections** | §9 (Dynamic CTA Dashboard), §6 (primary CTA per industry) |
| **Depends on** | [01 — industry](01-industry.md), [02 — lead](02-lead.md) |
| **Blocks** | 04 (revenue linkage), 06 |
| **Status** | 🔴 New |
| **Estimate** | ~4–5 dev-weeks (part of Phase 2) |

---

## 1. What a CTA is here

Not a button. **The commercial ask that moves a lead toward the revenue moment** — "book a site
visit", "start a trial", "check eligibility". BRD §6 assigns each industry a primary CTA, and
§9 measures the funnel through it: offered → accepted → completed, with failures explained.

This is where the platform learns whether a conversation *did* anything.

---

## 2. Reuse `Booking` — do not build a parallel appointment system

Three of the nine industries have appointment-shaped CTAs, and this codebase already has a
mature booking engine behind them — availability profiles and rules, exception handling,
calendar sync, reschedule tokens, and a double-booking `ExclusionConstraint`.

| Industry | Primary CTA | Fulfilled by |
|---|---|---|
| Real Estate | Site Visit | `Booking` |
| Healthcare | Book Appointment | `Booking` |
| Solar | Survey / Site Assessment | `Booking` |

`Booking.status` already distinguishes `COMPLETED` / `NO_SHOW` / `CANCELED` — which *is* §15's
Healthcare show rate. A `CTAEvent` for these industries should **point at a `Booking`**, not
duplicate its state.

The remaining six fulfil against an `Opportunity`, a subscription, or nothing but their own
completion.

---

## 3. Data model

### 3.1 `CTADefinition` — Tier 2

What CTAs exist for this tenant, and what each is worth.

| Field | Type | Notes |
|---|---|---|
| `key` | `SlugField(48)` | Stable — `site_visit`. Referenced by the voice runtime |
| `label` | `CharField(80)` | "Book a site visit" |
| `target_stage` | FK → `JourneyStage`, PROTECT | Where acceptance moves the lead |
| `fulfilment_type` | `CharField(20)`, choices | `BOOKING` \| `OPPORTUNITY` \| `EXTERNAL` \| `NONE` |
| `position` | `PositiveSmallIntegerField` | Escalation order — see §4 |
| `min_lead_score` | `PositiveSmallIntegerField` | Entry condition |
| `eligible_from_stage` | FK → `JourneyStage`, SET_NULL | Entry condition |
| `expected_value` | `Decimal(14,2)`, null | §9 *CTA Value* — expected |
| `currency` | `CharField(3)` | Callable default |
| `is_primary` | `BooleanField` | The §6 primary CTA for this industry |
| `is_active` | `BooleanField` | |

Seeded per industry from BRD §6, same mechanism as `JourneyStage`.

`db_table = "core_cta_definition"`

### 3.2 `CTAEvent` — Tier 1 Core

One row per offer. **The state machine, not a log line.**

| Field | Type | Notes |
|---|---|---|
| `lead` | FK → `Lead`, CASCADE | |
| `definition` | FK → `CTADefinition`, PROTECT | |
| `state` | `CharField(12)`, choices | `OFFERED` \| `ACCEPTED` \| `COMPLETED` \| `FAILED` \| `EXPIRED` |
| `offered_at` | `DateTimeField` | |
| `accepted_at` / `completed_at` / `failed_at` | `DateTimeField`, null | |
| `failure_reason` | `CharField(48)`, choices, blank | §9 requires the reason, not just the count |
| `call` | FK → `AgentCallLog`, SET_NULL | The conversation that offered it |
| `agent` | FK → `Agent`, SET_NULL | §9 *By Agent* |
| `campaign` | FK → `Campaign`, SET_NULL | §9 *By Campaign* |
| `booking` | FK → `Booking`, SET_NULL | When `fulfilment_type=BOOKING` |
| `opportunity` | FK → `Opportunity`, SET_NULL | When `OPPORTUNITY` — added in module 04 |
| `expected_value` | `Decimal(14,2)`, null | Snapshotted from the definition at offer time |
| `realized_value` | `Decimal(14,2)`, null | Filled on completion, from the revenue event |

**Failure reasons** — a closed taxonomy, because "other" tells nobody anything:

```python
class FailureReason(models.TextChoices):
    NO_SLOT_AVAILABLE   = "NO_SLOT_AVAILABLE"     # §21 availability alert
    INTEGRATION_ERROR   = "INTEGRATION_ERROR"     # §21 CTA failure alert
    DECLINED            = "DECLINED"
    NO_RESPONSE         = "NO_RESPONSE"
    INELIGIBLE          = "INELIGIBLE"
    CANCELLED_BY_LEAD   = "CANCELLED_BY_LEAD"
    EXPIRED             = "EXPIRED"
```

> **`expected_value` is snapshotted onto the event, not read through the FK.** A tenant editing
> a definition's value must not silently restate every historical CTA's worth — the same reason
> [05 — cost](05-cost.md) uses effective-dated rates.

**Indexes:** `(tenant, definition, state)` for §9 funnel counts; `(tenant, lead, offered_at)`
for the timeline; `(tenant, agent, state)` and `(tenant, campaign, state)` for the breakdowns.

`db_table = "core_cta_event"`

---

## 4. The dynamic behaviour in §9

> CTA is not static. SaaS Plan Compare → Demo → Subscribe as intent rises; Real Estate Property
> Compare → Site Visit → Booking; Jewellery Compare → Showroom → Purchase.

**Model this as ordered definitions with entry conditions, not as a hard-coded ladder.**
`next_cta_for(lead)` returns the highest-`position` active definition whose `min_lead_score` and
`eligible_from_stage` the lead satisfies. Escalation is then a consequence of the lead's own
score and stage — which is what "as intent rises" means.

Two things this buys:

1. **§19.5 pattern discovery can propose changes as configuration** — the BRD is explicit that
   discovered patterns become "configuration suggestions, not silent automatic changes". If the
   ladder were code, a suggestion would be a ticket rather than a toggle.
2. Adding a tenth industry is data, not a deployment.

---

## 5. Services

`core/services/cta.py`

| Function | Purpose |
|---|---|
| `next_cta_for(lead)` | The escalation resolver above |
| `offer(lead, definition, *, call, agent)` | Creates `OFFERED` + `LeadEvent` |
| `accept(event)` | → `ACCEPTED`, transitions the lead to `target_stage` via `leads.transition()` |
| `complete(event, *, fulfilment)` | → `COMPLETED`, links `Booking`/`Opportunity`, sets `realized_value` |
| `fail(event, reason)` | → `FAILED` with a taxonomy reason |
| `expire_stale_offers()` | Beat task; → `EXPIRED` |
| `funnel(tenant, **filters)` | §9 aggregate |

**State transitions are one-way and validated.** `COMPLETED` → `OFFERED` must be impossible; a
CTA that can move backwards makes §9's completion rate uncountable.

**Acceptance must go through `leads.transition()`**, never a direct stage write — see
[02 §4](02-lead.md).

---

## 6. API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `cta-definitions/` | The tenant's CTA ladder |
| PATCH | `cta-definitions/{id}/` | Enterprise only — value, thresholds, order |
| GET | `cta-events/` | Filterable by state, definition, agent, campaign, date |
| GET | `cta-events/funnel/` | **§9 dashboard** — offered/accepted/completed/failed + rates + value |
| GET | `cta-events/by-agent/` | §9 *By Agent* |
| GET | `cta-events/by-campaign/` | §9 *By Campaign* |
| POST | `cta-events/{id}/complete/` | Manual completion |
| POST | `cta-events/{id}/fail/` | Manual failure with reason |

**Machine surface.** The voice runtime offers and accepts CTAs mid-conversation, so `offer` and
`accept` need agent-token access via `vb_agent_allowed_actions` — see
[VB_AGENT scoping](../../apiDocs/core/vb_agent_tenant_scoping.md). Every row returned carries
`lead_id`, per the DASH-11 contract.

---

## 7. Background jobs

| Task | Cadence | Purpose |
|---|---|---|
| `expire_stale_cta_offers` | Hourly | `OFFERED` beyond its window → `EXPIRED`. Without it, acceptance rate is permanently understated by an ever-growing tail |
| `reconcile_cta_bookings` | Daily | `Booking` cancelled after CTA completion ⇒ correct the CTA and any revenue linkage |

---

## 8. Settings

| Setting | Default | Effect |
|---|---|---|
| `CTA_OFFER_TTL_HOURS` | `72` | How long an offer stays live |
| `CTA_MAX_ACTIVE_PER_LEAD` | `2` | Prevents stacking offers on one lead |

---

## 9. Testing

| Area | Must prove |
|---|---|
| Escalation | `next_cta_for` returns the right rung at each score/stage boundary |
| State machine | Backward transitions rejected; every illegal pair enumerated |
| Acceptance | Moves the lead **through `leads.transition()`** — assert a transition row exists |
| Value snapshot | Editing a definition does not change historical `expected_value` |
| Booking link | A cancelled booking corrects its CTA on reconciliation |
| Expiry | Stale offers expire and stop counting as pending |
| Failure taxonomy | Every reason is reachable from a real code path |
| Seeding | All nine industries seed a primary CTA matching BRD §6 |
| **Structural guard** | Every `Industry` value has at least one `is_primary` definition |

---

## 10. Open decisions

| # | Decision | Default if unanswered |
|---|---|---|
| 1 | Can two CTAs be live for one lead simultaneously? | Yes, capped at `CTA_MAX_ACTIVE_PER_LEAD` |
| 2 | Does a re-offer after failure create a new event or reopen the old one? | New event — preserves the failure history |
| 3 | Who may complete a CTA — runtime, human, or both? | Both |
| 4 | Does acceptance without completion still count in §9's acceptance rate? | Yes; they are separate rates by design |

---

## Related

- [Implementation plan §4.3](../implementation-plan.md)
- [02 — lead](02-lead.md) · [04 — revenue](04-revenue.md)
- [Bookings API](../../apiDocs/core/booking.md) — the fulfilment engine to reuse

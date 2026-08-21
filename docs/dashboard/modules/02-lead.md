# Module 02 — `lead`

**Lead lifecycle, stage transitions and the unified timeline.** The module BRD v3.1 was written
to add.

| | |
|---|---|
| **BRD sections** | §7 (Individual Lead Journey View), §8 (Lead Journey Analytics) |
| **Depends on** | [01 — industry](01-industry.md) |
| **Blocks** | 03, 04, 06 |
| **Status** | 🔴 New |
| **Estimate** | ~5–6 dev-weeks (part of Phase 1) |

---

## 1. Why this module exists

v3.1's own summary says it plainly:

> …an SME or CEO could see aggregate journey analytics but never follow one real lead's actual
> path end to end.

`Contact` is an address-book entry: name, phone, consent, `last_contacted_at`. It has **no
stage, no score, no owner, no journey**. There is nothing in the system that represents "this
person's progress toward buying".

---

## 2. Keep `Lead` separate from `Contact`

The cheap move is to add `stage` and `score` to `Contact`. Do not.

**A person can be a lead more than once.** A real-estate enquiry that goes cold and returns
fourteen months later is a *new journey*, not a mutated old one. Collapsing them:

- destroys the first journey's history the moment the second begins;
- makes §7's *days in journey* meaningless on a contact with three journeys;
- makes §8's conversion rates wrong, because the denominator counts people rather than
  opportunities to convert.

One `Contact` (the person) → many `Lead`s (attempts to convert them). `Contact` keeps identity
and consent; `Lead` keeps commercial progress.

---

## 3. Data model

### 3.1 `Lead` — Tier 2

| Field | Type | Notes |
|---|---|---|
| `contact` | FK → `Contact`, PROTECT | The person. PROTECT — never orphan a journey |
| `stage` | FK → `JourneyStage`, PROTECT | Current position |
| `status` | `CharField(16)`, choices | `OPEN` \| `WON` \| `LOST` \| `DORMANT` |
| `score` | `PositiveSmallIntegerField` | 0–100. §7 identity header |
| `score_updated_at` | `DateTimeField`, null | Freshness, per §25 |
| `owner` | FK → user, SET_NULL | §5 Action Center ownership |
| `campaign` | FK → `Campaign`, SET_NULL | §17 source economics |
| `source` | `CharField(32)`, choices | Mirrors `Contact.Source` + campaign/inbound |
| `entered_stage_at` | `DateTimeField` | Stage aging (§16), velocity (§8) |
| `first_touch_at` / `last_touch_at` | `DateTimeField` | Journey span |
| `closed_at` | `DateTimeField`, null | Set on WON/LOST |
| `lost_reason` | `CharField(64)`, blank | §18 lost reasons |

**Indexes:** `(tenant, stage, status)` for funnel counts; `(tenant, owner, status)` for the
Action Center; `(tenant, entered_stage_at)` for aging sweeps.

`db_table = "core_lead"`

### 3.2 `LeadStageTransition` — Tier 1 Core

**Append-only. Never updated, never deleted.** This table *is* BRD §8.

| Field | Type | Notes |
|---|---|---|
| `lead` | FK → `Lead`, CASCADE | |
| `from_stage` / `to_stage` | FK → `JourneyStage`, PROTECT | `from_stage` null on entry |
| `actor_type` | `CharField(12)`, choices | `AI` \| `HUMAN` \| `SYSTEM` — §8 AI influence |
| `actor_user` | FK → user, SET_NULL | When HUMAN |
| `actor_agent` | FK → `Agent`, SET_NULL | When AI |
| `call` | FK → `AgentCallLog`, SET_NULL | The conversation that caused it |
| `reason` | `CharField(120)`, blank | |
| `occurred_at` | `DateTimeField` | |
| `dwell_seconds` | `PositiveIntegerField`, null | Time in `from_stage`. Denormalised at write |

> **`dwell_seconds` is written once, at transition time, and never recomputed.** §8 velocity
> across millions of rows cannot afford a window function per query, and the value can never
> change after the fact.

Every §8 metric is a query over this one table:

| §8 metric | Query |
|---|---|
| Stage conversion | count(to_stage=X) / count(from_stage=X) |
| Leakage | leads entering X with no onward transition |
| Velocity | avg(`dwell_seconds`) by stage |
| AI influence | share where `actor_type = AI` |
| Human handoff | transitions where `actor_type` flips AI → HUMAN |

`db_table = "core_lead_stage_transition"`

### 3.3 `LeadEvent` — Tier 1 Core

The §7 Timeline: *"Every touchpoint in order — calls, messages, CTA offered/accepted/
completed/failed, stage change, human handoff — each timestamped."*

| Field | Type | Notes |
|---|---|---|
| `lead` | FK → `Lead`, CASCADE | |
| `event_type` | `CharField(32)`, choices | `CALL` \| `MESSAGE` \| `CTA_OFFERED` \| `CTA_ACCEPTED` \| `CTA_COMPLETED` \| `CTA_FAILED` \| `STAGE_CHANGE` \| `HANDOFF` \| `SCORE_CHANGE` \| `NOTE` \| `REVENUE` |
| `occurred_at` | `DateTimeField`, indexed | |
| `summary` | `CharField(255)` | One human-readable line |
| `payload` | `JSONField` | Type-specific detail |
| `object_type` / `object_id` | `CharField` / `UUIDField` | Loose reference to the source row |
| `confidence` | `FloatField`, null | §7 AI reasoning trail |
| `model_version` | `CharField(40)`, blank | §7 requires the model version behind each AI moment |

**Index:** `(tenant, lead, occurred_at)` — the timeline query, and the only one that matters.

> **Why a denormalised event table rather than a union at read time.** §7 requires calls,
> messages, CTAs, stage changes and handoffs on one ordered axis. A UNION across five tables
> with different shapes, sorted and paginated, is slow and brittle, and it breaks the moment a
> sixth source is added. Writing an event alongside each source record costs one insert and
> makes the timeline a single indexed scan.
>
> The cost is that events can drift from their sources. Mitigate with a reconciliation check
> in the nightly rollup, not by abandoning the design.

`db_table = "core_lead_event"`

---

## 4. Services

`core/services/leads.py`

| Function | Purpose |
|---|---|
| `create_from_contact(contact, *, source, campaign=None)` | Opens a journey at the first stage. Refuses if an OPEN lead exists for that contact |
| `transition(lead, to_stage, *, actor, call=None, reason="")` | **The only way stage changes.** Atomic: writes the transition, updates `Lead`, computes `dwell_seconds`, emits a `LeadEvent` |
| `record_event(lead, event_type, ...)` | Timeline append |
| `rescore(lead, score, *, confidence, model_version)` | Score + provenance + event |
| `close(lead, status, *, reason)` | WON/LOST, sets `closed_at` |
| `timeline(lead, *, limit, cursor)` | Paginated §7 timeline |
| `peer_context(lead)` | §7 "ahead of pace / typical / stalled" vs same stage + industry |

**`transition()` must be the single entry point.** If anything writes `Lead.stage` directly,
the transition history develops holes and §8 silently under-reports — the kind of bug that
looks like a business trend rather than a defect. Enforce with a structural test (§8 below).

---

## 5. API surface

`/api/v1/core/leads/` — `TenantBaseViewSet`

| Method | Path | Purpose | BRD |
|---|---|---|---|
| GET | `leads/` | List + filter by stage, owner, campaign, score, status | §20 |
| GET | `leads/{id}/` | §7 identity header + current state | §7 |
| GET | `leads/{id}/journey/` | **The §7 view** — timeline, revenue linkage, reasoning trail, peer context | §7 |
| GET | `leads/{id}/timeline/` | Paginated timeline alone | §7 |
| POST | `leads/{id}/transition/` | Manual stage move | §7 |
| PATCH | `leads/{id}/` | Owner, score override, notes | |
| GET | `leads/journey-analytics/` | §8 aggregate — volume, conversion, leakage, velocity | §8 |

### The DASH-11 contract

> **DASH-11:** *Any lead can be opened to its full individual journey in one click from any KPI,
> chart or alert.*

This is a cross-cutting API rule, not a feature of this module:

**Every list or aggregate endpoint the dashboard consumes must return a `lead_id` on each row
it represents.** Cheap to honour from the start; expensive to retrofit across a dozen
endpoints in Phase 4. Add it to the definition of done for every module from here on, and to
the schema regression test.

---

## 6. Background jobs

| Task | Cadence | Purpose |
|---|---|---|
| `mark_dormant_leads` | Daily | OPEN leads with no touch beyond a threshold → `DORMANT`. Feeds §16 stage aging |
| `recompute_lead_scores` | Hourly | Rescore where signals changed |
| `reconcile_lead_events` | Daily | Detect timeline events whose source row vanished — the known cost of §3.3 |

---

## 7. Settings

| Setting | Default | Effect |
|---|---|---|
| `LEAD_DORMANT_AFTER_DAYS` | `30` | No touch for this long ⇒ DORMANT |
| `LEAD_REOPEN_WINDOW_DAYS` | `90` | Inside this window a returning contact resumes the old lead; beyond it, a new journey |
| `LEAD_DEFAULT_SCORE` | `0` | Score before first evaluation |

`LEAD_REOPEN_WINDOW_DAYS` is the knob that decides §2's "is this a new journey?" — the
behaviour is a business rule, so it belongs in configuration rather than in code.

---

## 8. Testing

| Area | Must prove |
|---|---|
| One journey per contact | A second OPEN lead for the same contact is refused |
| Reopen window | Inside ⇒ resumes; outside ⇒ new lead |
| Transition atomicity | Failure mid-transition leaves no partial state |
| `dwell_seconds` | Correct across transitions, including same-second moves |
| Timeline order | Strictly by `occurred_at`; ties broken deterministically |
| Peer context | Correct bucketing at boundaries |
| §8 metrics | Conversion, leakage, velocity and AI influence against a hand-built fixture with known answers |
| Isolation | Tenant A cannot read tenant B's lead or timeline |
| **Structural guard 1** | Scan the codebase: nothing outside `leads.transition()` assigns `Lead.stage` |
| **Structural guard 2** | Every dashboard list serializer exposes `lead_id` — enforcing DASH-11 |

Mutation-test the §8 aggregates specifically. A conversion formula that is wrong by one stage
still returns a plausible percentage, and no human reviewing a dashboard will catch it.

---

## 9. Open decisions

| # | Decision | Default if unanswered |
|---|---|---|
| 1 | Is a lead created for every inbound call, or only on qualification? | Every call with a contact |
| 2 | Who owns a lead by default — round robin, campaign owner, unassigned? | Unassigned |
| 3 | Is scoring rule-based or model-based in v1? | Rule-based; §7 needs the reasoning trail either way |
| 4 | Can a lead move backwards? | Yes — record it; the funnel is not one-way in reality |
| 5 | Does a WON lead's contact become eligible for a new lead immediately? | Yes (upsell is a new journey) |

---

## Related

- [Implementation plan §4.2](../implementation-plan.md)
- [01 — industry](01-industry.md) — supplies `JourneyStage`
- [03 — cta](03-cta.md) — writes `LeadEvent` rows
- [04 — revenue](04-revenue.md) — §7 revenue linkage

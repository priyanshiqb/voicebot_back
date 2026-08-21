# Module 01 — `industry`

**Tenant industry and journey configuration.** The spine of the whole dashboard.

| | |
|---|---|
| **BRD sections** | §6 (Industry-Native Lead Journey), §15 (Industry KPI Cards), §24 (SME vs Enterprise) |
| **Depends on** | Nothing — build first |
| **Blocks** | Every other module |
| **Status** | 🔴 New |
| **Estimate** | ~3–4 dev-weeks (part of Phase 1) |

---

## 1. Why this is first

BRD §6 states the core rule:

> The dashboard must never force the same funnel across industries. Nine verticals, nine
> different revenue moments — the funnel and CTA shown adapt automatically to the tenant's
> configured industry.

Nothing downstream can be built until *"what industry is this tenant, and what are its
stages"* is answerable. A `Lead` has no stage to be in; a CTA has no target; a revenue moment
cannot be identified. Every 🔴 in the coverage map traces back here.

---

## 2. ⚠️ Do not reuse `AgentTemplate.industry`

It exists. It is the wrong thing.

| | `AgentTemplate.industry` (existing) | What this module needs |
|---|---|---|
| Scope | An agent template | The tenant |
| Purpose | Conversational persona | Dashboard funnel & revenue moment |
| Values | `CLINIC_RECEPTIONIST`, `AGRICULTURAL_SUPPLIER`, `REAL_ESTATE`, `E_COMMERCE`, `RESTAURANT`, `GENERAL_ASSISTANT`, `OTHER` (7) | SaaS, Real Estate, Education, Solar, Jewellery, Financial, Insurance, Healthcare, Travel (9) |
| Overlap | ~2 of 9 | — |

Reusing it produces a tenant whose dashboard funnel is decided by whichever agent template was
created most recently — a bug that will look like flaky data, not like a design error. **Add a
new, separate field and leave the existing one untouched.**

---

## 3. Data model

### 3.1 `TenantIndustryProfile` — Tier 2

One row per tenant. A model rather than a field on `TenantSettings` because journey
configuration is versioned, audited business configuration, and because §24 requires SME and
Enterprise tenants to differ in how much of it they may edit.

| Field | Type | Notes |
|---|---|---|
| `tenant` | FK (inherited) | Unique — one profile per tenant |
| `industry` | `CharField(32)`, choices | The nine BRD values |
| `sub_industry` | `CharField(64)`, blank | §20 filter dimension |
| `currency` | `CharField(3)` | Callable default, as `default_catalog_currency()` |
| `fiscal_year_start_month` | `PositiveSmallIntegerField` | Default 4 (April, Indian FY) — §13 target periods |
| `dashboard_mode` | `CharField(16)`, choices | `SME` \| `ENTERPRISE` — §24 |
| `is_journey_customised` | `BooleanField` | False ⇒ still on seeded defaults; drives "reset to default" |

```python
class Industry(models.TextChoices):
    SAAS        = "SAAS", "SaaS"
    REAL_ESTATE = "REAL_ESTATE", "Real Estate"
    EDUCATION   = "EDUCATION", "Education"
    SOLAR       = "SOLAR", "Solar"
    JEWELLERY   = "JEWELLERY", "Jewellery"
    FINANCIAL   = "FINANCIAL", "Financial Services"
    INSURANCE   = "INSURANCE", "Insurance"
    HEALTHCARE  = "HEALTHCARE", "Healthcare"
    TRAVEL      = "TRAVEL", "Travel"
```

`db_table = "core_tenant_industry_profile"`

### 3.2 `JourneyStage` — Tier 2

The tenant's funnel, ordered. Seeded per industry, editable in Enterprise mode.

| Field | Type | Notes |
|---|---|---|
| `tenant` | FK (inherited) | |
| `key` | `SlugField(48)` | Stable identifier — `site_visit`. **Never** rename; the dashboard and CTA definitions reference it |
| `label` | `CharField(80)` | Display name |
| `position` | `PositiveSmallIntegerField` | Funnel order |
| `is_revenue_moment` | `BooleanField` | **Exactly one per tenant** |
| `is_terminal` | `BooleanField` | Won/lost states that end the journey |
| `expected_days` | `PositiveSmallIntegerField`, null | Feeds §8 velocity and §16 stage aging |

**Constraints:**

```python
constraints = [
    models.UniqueConstraint(fields=["tenant", "key"],
        condition=Q(is_deleted=False), name="unique_journey_stage_key"),
    models.UniqueConstraint(fields=["tenant", "position"],
        condition=Q(is_deleted=False), name="unique_journey_stage_position"),
    models.UniqueConstraint(fields=["tenant"],
        condition=Q(is_revenue_moment=True, is_deleted=False),
        name="one_revenue_moment_per_tenant"),
]
```

> **That third constraint is the important one.** BRD §13's *Revenue Moment Rate* is
> meaningless if a tenant has two revenue moments or none, and a tenant editing their funnel in
> Enterprise mode will eventually try both. Enforce it in the database, not in a serializer —
> the seeder, the admin and the API all write this table.

`db_table = "core_journey_stage"`

---

## 4. Seeded journeys

Straight from BRD §6. The revenue moment is **bold**.

| Industry | Stages (in order) |
|---|---|
| SaaS | Inquiry → Qualified → Demo/Direct Buy → Trial → **Paid** |
| Real Estate | Inquiry → Qualified → Site Visit → **Booking** → Sale |
| Education | Inquiry → Counselling → Application → **Admission** |
| Solar | Inquiry → Qualified → Site Assessment → Quote → **Contract** |
| Jewellery | Inquiry → Compare → Showroom → **Purchase** |
| Financial | Inquiry → Eligibility → Genuine Customer → Documents → **Disbursal** |
| Insurance | Inquiry → Compare → Application → **Policy** |
| Healthcare | Inquiry → Triage → Appointment → **Visit** |
| Travel | Inquiry → Qualified → Package → **Booking** |

> **Real Estate has both "Booking" and "Sale".** The BRD names *Booking / sale* as the revenue
> moment and Sale as a later stage. Seed the revenue moment on **Booking** — that is where the
> commercial commitment happens and where money first moves. Flag this for confirmation.

**Delivered as a data migration plus an idempotent `seed_journeys` management command**, so a
tenant created before this module ships is backfilled, and re-running is safe.

**DASH-08** ("SME receives useful dashboard without RevOps configuration") is satisfied here or
nowhere: a tenant that picks an industry at onboarding must get all nine stages, one revenue
moment and a working funnel with zero further input.

---

## 5. Services

`core/services/journeys.py`

| Function | Purpose |
|---|---|
| `seed_for_tenant(tenant, industry, *, force=False)` | Idempotent. Refuses to overwrite a customised journey unless `force` |
| `stages_for(tenant)` | Ordered stages, cached — read on nearly every dashboard request |
| `revenue_moment(tenant)` | The single flagged stage; raises if misconfigured |
| `next_stage(stage)` / `previous_stage(stage)` | Funnel navigation for transitions |
| `reset_to_default(tenant)` | Back to the seeded journey; clears `is_journey_customised` |

**Cache `stages_for`** with the same asymmetric-TTL discipline used for sessions — this is read
constantly and written almost never. Invalidate on any `JourneyStage` write.

---

## 6. API surface

`/api/v1/core/journey-stages/` — `TenantBaseViewSet`

| Method | Path | Mode | Purpose |
|---|---|---|---|
| GET | `journey-stages/` | Both | The tenant's funnel, ordered |
| GET | `journey-stages/dropdown/` | Both | Stage picker |
| PATCH | `journey-stages/{id}/` | Enterprise only | Rename, reorder, set `expected_days` |
| POST | `journey-stages/reorder/` | Enterprise only | Bulk reposition in one transaction |
| POST | `journey-stages/reset/` | Enterprise only | Back to seeded defaults |
| GET/PATCH | `industry-profile/` | Enterprise for PATCH | Industry, currency, fiscal year, mode |

**SME mode is read-only on this resource** (§24: "One business funnel" vs "Segmented funnels").
Enforce server-side via permission, not by hiding the button — §25 requires tenant permissions
enforced server-side.

> **Changing `industry` on a tenant with existing leads is destructive**: every `Lead.stage`
> points at a stage that is about to disappear. Either refuse it once leads exist, or require
> an explicit stage-remapping payload. Do not let it silently orphan the funnel.

---

## 7. Migration & rollout

1. Create both tables.
2. Data migration: create a `TenantIndustryProfile` for every existing tenant, defaulting
   `industry` to a configured fallback and `dashboard_mode` to `SME`.
3. Seed `JourneyStage` rows for every tenant from its industry.
4. Ship the `seed_journeys` command for re-runs and new industries.

No downtime; both tables are new and nothing reads them yet.

---

## 8. Testing

| Area | Must prove |
|---|---|
| Seeding | All nine industries seed the correct stages, in order, with exactly one revenue moment |
| Idempotency | Re-running `seed_journeys` changes nothing and does not duplicate |
| Constraint | Two revenue moments is rejected **by the database**, not just by a serializer |
| Customisation | Editing sets `is_journey_customised`; reseeding refuses without `force` |
| Mode enforcement | SME tenant gets `403` on PATCH — asserted through the API, not the permission class |
| Isolation | Tenant A cannot read or reorder tenant B's stages |
| Cache | Stage edits invalidate; a stale funnel is never served |
| **Structural guard** | A test walking `Industry.choices` and asserting every value has seeded stages — so adding a tenth industry without seeds fails the suite rather than shipping a blank funnel |

That last one matters: the failure mode of a missing seed is an empty dashboard for one
industry's customers, which no other test would catch.

---

## 9. Open decisions

| # | Decision | Default if unanswered |
|---|---|---|
| 1 | Real Estate revenue moment — Booking or Sale? | Booking |
| 2 | Where is industry captured at onboarding? | New signup step |
| 3 | Can a tenant change industry after leads exist? | Refuse; require explicit remap |
| 4 | Do sub-industries need their own stage sets? | No — filter dimension only |
| 5 | Multi-industry tenants (a group with two businesses)? | Out of scope for v1 |

---

## Related

- [Implementation plan §4.1](../implementation-plan.md)
- [02 — lead](02-lead.md) — the first consumer of `JourneyStage`
- [09 — existing changes](09-existing-changes.md) — `TenantSettings`, `AgentTemplate.industry`

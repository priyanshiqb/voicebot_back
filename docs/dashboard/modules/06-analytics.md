# Module 06 — `analytics`

**Rollups, the metric registry, and every dashboard read endpoint.** The module that turns four
domains into a dashboard.

| | |
|---|---|
| **BRD sections** | §4, §8, §10, §11, §12, §13, §14, §15, §16, §17, §20, §25, §27 |
| **Depends on** | 01–05 |
| **Blocks** | 07, 08 |
| **Status** | 🔴 New |
| **Estimate** | ~14–18 dev-weeks (Phase 4 — the largest) |

---

## 1. Scope

**There are currently zero analytics endpoints in this codebase.** Everything the dashboard
reads is built here.

Two responsibilities that must not be conflated:

1. **The metric registry** — one definition per metric, with source, freshness and description.
   Satisfies §25 and DASH-09 structurally, and makes §27's Metric Catalog a live artefact.
2. **The aggregation layer** — pre-computed rollups so §26's 30-second promise survives a busy
   quarter.

---

## 2. The metric registry

### 2.1 Why it exists

§27 lists ~50 metrics. §25 requires every financial KPI to expose *definition, source and
freshness*. DASH-09 tests it. DASH-10 forbids any primary KPI depending only on call volume.

As prose, that drifts from the code within one release — the document says one thing, the API
returns another, and nobody notices until a customer asks how a number is computed.

**Make the registry the single definition, and generate §27 from it.**

### 2.2 `MetricDefinition` — code, not a table

A declarative registry in `core/analytics/registry.py`, in code so it is reviewable, diffable
and testable:

```python
@register
class RealizedRevenue(Metric):
    key          = "realized_revenue"
    label        = "Realized Revenue"
    category     = Category.REVENUE
    definition   = ("Confirmed revenue recognised in the period, from the tenant's "
                    "configured revenue source. Excludes open pipeline.")
    unit         = Unit.CURRENCY
    source       = "core_revenue_event.amount"
    freshness    = Freshness.ROLLUP_DAILY
    is_volume_only = False          # DASH-10
    supports     = [Dim.PERIOD, Dim.TEAM, Dim.CAMPAIGN, Dim.AGENT, Dim.PRODUCT]
    drilldown    = Drilldown.LEAD   # DASH-11
```

Every metric endpoint returns the value **and** this metadata. One definition, one place, and
the API cannot serve a metric that has not declared what it means.

### 2.3 What the registry enforces

| Rule | BRD | Enforcement |
|---|---|---|
| Definition, source, freshness on every metric | §25, DASH-09 | Required fields — a metric without them fails to register |
| No primary KPI is volume-only | DASH-10 | `is_volume_only` metrics rejected from the executive KPI set |
| Every ₹ metric drills to a lead | DASH-11 | `drilldown` required when `unit == CURRENCY` |
| Metric catalog matches reality | §27 | §27 generated from the registry by a management command |
| Pipeline never presented as revenue | §25 | Separate categories; a test asserts no metric mixes both sources |

---

## 3. Rollups

### 3.1 Why pre-aggregate

§26 promises a CEO answers "revenue, target, forecast, risk and ROI" in 30 seconds. Computing
eight KPIs live means joining call logs, leads, transitions, CTAs, opportunities, revenue events
and costs, per request, per filter combination.

That works in a demo and fails in the first quarter that matters — and it fails at exactly the
moment the CEO most wants to look.

### 3.2 `MetricRollup` — Tier 1 Core

| Field | Type | Notes |
|---|---|---|
| `metric_key` | `CharField(64)`, indexed | From the registry |
| `period_type` | `CharField(8)`, choices | `DAY` \| `WEEK` \| `MONTH` \| `QUARTER` |
| `period_start` | `DateField`, indexed | |
| `dimension_type` | `CharField(16)`, null | `AGENT` \| `CAMPAIGN` \| `TEAM` \| `PRODUCT` \| `STAGE` \| `SOURCE` |
| `dimension_id` | `UUIDField`, null | Null ⇒ tenant-wide |
| `value` | `Decimal(18,4)` | |
| `numerator` / `denominator` | `Decimal(18,4)`, null | **Kept for rates** — see below |
| `sample_size` | `PositiveIntegerField` | §19.6 insight confidence |
| `computed_at` | `DateTimeField` | The freshness §25 requires |

**Unique:** `(tenant, metric_key, period_type, period_start, dimension_type, dimension_id)`

> **Store numerator and denominator, not just the rate.** A conversion rate of 40% for Monday
> and 60% for Tuesday does not average to 50% for the two days — it depends on the volumes. A
> rollup that stores only the rate cannot be re-aggregated to a week without being wrong, and
> the error is invisible.

`db_table = "core_metric_rollup"`

### 3.3 Rollup strategy

| Age | Source |
|---|---|
| Today | Live query, capped by filter complexity |
| Yesterday–90 days | `DAY` rollups |
| Beyond 90 days | `MONTH` rollups |

Nightly `rollup_metrics` computes the previous day for every registered metric × dimension.
`recompute_rollups(since)` handles late-arriving data — cost telemetry (module 05) and CRM
revenue sync both land after the fact, so **rollups must be idempotent and re-runnable**, never
append-only.

---

## 4. API surface

One shape for all metric reads, rather than fifty bespoke endpoints:

```
GET /api/v1/core/analytics/metrics/?keys=realized_revenue,ai_roi
      &period=month&dimension=campaign&from=2026-08-01&to=2026-08-31
```

```json
{
  "data": [{
    "key": "realized_revenue",
    "label": "Realized Revenue",
    "value": "2840000.00",
    "unit": "CURRENCY", "currency": "INR",
    "comparison": { "previous": "2510000.00", "change_pct": 13.1 },
    "meta": {
      "definition": "Confirmed revenue recognised in the period…",
      "source": "core_revenue_event.amount",
      "computed_at": "2026-08-14T02:00:00Z",
      "freshness": "ROLLUP_DAILY",
      "sample_size": 94,
      "attribution_version": "last-touch-v1",
      "is_inferred": false
    },
    "drilldown": "/api/v1/core/leads/?revenue_period=2026-08"
  }]
}
```

Purpose-built endpoints on top of the same registry:

| Path | BRD |
|---|---|
| `analytics/executive-summary/` | §4 — the eight above-the-fold KPIs in one call |
| `analytics/journey/` | §8 |
| `analytics/workforce/` | §10 |
| `analytics/agents/` | §11, §12 |
| `analytics/industry-kpis/` | §15 — resolves from the tenant's industry |
| `analytics/pipeline-risk/` | §16 |
| `analytics/campaigns/` | §17 |
| `analytics/conversation/` | §18 |

**`executive-summary/` is one request, not eight.** §26 gives the CEO 30 seconds; eight parallel
round trips on a mobile connection spends most of it.

### 4.1 Filters (§20)

Period · industry · team · AI agent · human agent · campaign · product · CTA · stage.

Implemented once, as a shared filter backend over the rollup and live paths, so a filter added
later appears everywhere rather than in whichever endpoints someone remembered.

### 4.2 Drill-down (§20 + DASH-11)

> Executive KPI → segment → lead/opportunity → conversation → CTA → commercial event → realized
> revenue — terminating at the Individual Lead Journey View.

Every metric declares its `drilldown` in the registry, and **every row of every list response
carries `lead_id`**. This is the contract from [02 §5](02-lead.md); the registry is where it is
enforced for aggregates.

---

## 5. Background jobs

| Task | Cadence | Purpose |
|---|---|---|
| `rollup_metrics` | Nightly, after `snapshot_forecast` | Previous day, all metrics × dimensions |
| `rollup_period_aggregates` | Nightly | WEEK / MONTH / QUARTER from DAY rows |
| `recompute_rollups` | On demand | Late-arriving cost and revenue |
| `prune_rollups` | Monthly | DAY rows beyond retention → MONTH |

**Order matters.** `snapshot_forecast` (module 04) must run before `rollup_metrics`, or the
forecast rollup reads a snapshot that does not exist yet and silently records nulls.

---

## 6. Settings

| Setting | Default | Effect |
|---|---|---|
| `ANALYTICS_LIVE_WINDOW_DAYS` | `1` | Below this, query live |
| `ANALYTICS_DAY_ROLLUP_RETENTION_DAYS` | `400` | Daily granularity kept for 13 months — enough for YoY |
| `ANALYTICS_MAX_DIMENSION_CARDINALITY` | `500` | Guard against a rollup explosion on high-cardinality dimensions |
| `ANALYTICS_EXECUTIVE_CACHE_SECONDS` | `300` | Executive summary cache |

---

## 7. Testing

| Area | Must prove |
|---|---|
| Registry completeness | Every metric the API can serve has a registry entry — **and the reverse** |
| §27 generation | The published catalog regenerates identically from the registry |
| Rate re-aggregation | Week rollups from day rows match a direct computation (the numerator/denominator trap) |
| Idempotency | Re-running a rollup produces identical values |
| Late data | `recompute_rollups` corrects a period after cost telemetry arrives |
| Freshness | `computed_at` always reflects the underlying rollup, never request time |
| DASH-10 | No volume-only metric appears in the executive KPI set |
| DASH-11 | Every list row carries `lead_id`; every currency metric declares a drilldown |
| §25 separation | No metric mixes pipeline and realized sources |
| Isolation | Rollups are tenant-scoped; no cross-tenant read |
| Performance | `executive-summary/` within budget against a seeded large-tenant fixture |

Mutation-test the rate aggregation specifically. Averaging rates instead of dividing summed
numerators is the single most likely defect here, it produces plausible numbers, and it is
invisible to review.

---

## 8. Open decisions

| # | Decision | Default if unanswered |
|---|---|---|
| 1 | Where do revenue **targets** come from? §4 needs one for Target Achievement | New `RevenueTarget` model, per tenant/period/team |
| 2 | Is "today" live or last-night's rollup? | Live, capped |
| 3 | Do rollups store one currency or many? | Per currency; convert at read |
| 4 | Timezone for period boundaries — tenant or UTC? | Tenant's, from `TenantIndustryProfile` |
| 5 | Is §12's agent comparison table paginated or top-N? | Top-N with a drill |

Decision 1 is a real gap: **§4's *Target Achievement* is a headline KPI and there is no target
anywhere in this system.** It needs a model, an API and a UI for setting it — roughly a week of
work that no section of the BRD asks for explicitly.

Decision 4 matters more than it looks: a tenant in IST reading UTC-bounded days sees revenue
land on the wrong date near midnight, and "yesterday's number changed" is a trust problem.

---

## Related

- [Implementation plan §4.6 and §6.4](../implementation-plan.md)
- [04 — revenue](04-revenue.md) · [05 — cost](05-cost.md) — the two money sources
- [07 — insights](07-insights.md) — consumes rollups
- [08 — alerts](08-alerts.md) — consumes rollups

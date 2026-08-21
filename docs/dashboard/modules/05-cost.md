# Module 05 — `cost`

**Unit economics.** The module whose feasibility is decided outside this codebase.

| | |
|---|---|
| **BRD sections** | §14 (Cost & Unit Economics), §10–§11 (economic rows), §4 (Voice Cost, AI ROI) |
| **Depends on** | Nothing internally — **blocked on an external system** |
| **Blocks** | §4 AI ROI, DASH-07 |
| **Status** | 🔴 New |
| **Estimate** | ~4–6 dev-weeks **if** telemetry exists; otherwise unbounded |

---

## 1. ⚠️ Read this before estimating

§14 wants telephony minutes × rate, LLM tokens × rate, STT/TTS usage, messaging and enrichment
cost. **None of it is captured anywhere in this repository.**

`AgentCallLog` stores `duration_seconds` and nothing about spend, models, or providers. Those
facts exist in the voice runtime and the model providers' billing APIs — different systems.

**This module cannot invent them.** `duration_seconds × a flat rate` is an *estimate*, and
presenting an estimate as a unit economic breaches §25 ("every financial KPI has definition,
source and freshness").

### The decision that must precede the build

| Answer | Consequence |
|---|---|
| **Runtime can emit per-call cost** | Build as specified. ~4–6 weeks. DASH-07 achievable |
| **Runtime can emit usage but not cost** | Build as specified; this module applies the rates. Same cost, slightly more of it here |
| **Runtime can emit neither** | §14 must be re-scoped to **estimated** cost, labelled as such everywhere, and AI ROI becomes an estimate too. Say so in the BRD rather than shipping a confident wrong number |

Resolve in Phase 0. Everything below assumes one of the first two.

---

## 2. Data model

### 2.1 `CostRate` — Tier 2, effective-dated

| Field | Type | Notes |
|---|---|---|
| `component` | `CharField(20)`, choices | `TELEPHONY` \| `LLM` \| `STT` \| `TTS` \| `MESSAGING` \| `ENRICHMENT` |
| `provider` | `CharField(48)` | `twilio`, `openai`, `deepgram`, … |
| `model_or_plan` | `CharField(64)`, blank | `gpt-4o`, `nova-2` |
| `unit` | `CharField(16)`, choices | `MINUTE` \| `SECOND` \| `TOKEN` \| `CHARACTER` \| `MESSAGE` \| `REQUEST` |
| `rate` | `Decimal(14,6)` | **Six decimals** — per-token rates are ₹0.000018 |
| `currency` | `CharField(3)` | |
| `effective_from` | `DateTimeField` | |
| `effective_to` | `DateTimeField`, null | Null ⇒ current |
| `is_platform_default` | `BooleanField` | Tenant-null fallback rate |

> **Effective dating is not optional.** Provider prices change. A single mutable `rate` column
> means every historical cost restates the moment someone updates it — last quarter's cost per
> sale changes overnight, and the dashboard's own trend line becomes fiction. §25's "definition,
> source and freshness" is unachievable without it.
>
> **Six decimal places**, not two: token rates are small enough that two decimals round every
> LLM cost to zero.

**Constraint:** no overlapping `[effective_from, effective_to)` for the same
`(tenant, component, provider, model_or_plan, unit)`. Postgres `ExclusionConstraint` with a
`tstzrange` — the same mechanism the booking engine already uses against double-booking.

`db_table = "core_cost_rate"`

### 2.2 `CallCost` — Tier 1 Core

One row per call, itemised.

| Field | Type | Notes |
|---|---|---|
| `call` | FK → `AgentCallLog`, CASCADE, **unique** | |
| `telephony_cost` / `llm_cost` / `stt_cost` / `tts_cost` / `messaging_cost` / `other_cost` | `Decimal(12,6)` | Default 0 |
| `total_cost` | `Decimal(12,6)` | Denormalised — every §14 aggregate sums this |
| `currency` | `CharField(3)` | |
| `is_estimated` | `BooleanField` | **True ⇒ the UI must label it.** §1's third outcome |
| `usage` | `JSONField` | Raw counters — minutes, tokens in/out, characters. Auditability per §25 |
| `rate_version` | `CharField(40)` | Which rates produced this |
| `computed_at` | `DateTimeField` | |

**Index:** `(tenant, call)` unique; `(tenant, computed_at)` for period aggregates.

`db_table = "core_call_cost"`

---

## 3. Ingestion

The runtime reports usage; this module prices it.

```
POST /api/v1/core/call-costs/ingest/     (agent token)
{
  "provider_call_id": "...",
  "usage": {
    "telephony": {"provider": "twilio", "unit": "SECOND", "quantity": 184},
    "llm":       {"provider": "openai", "model_or_plan": "gpt-4o",
                  "unit": "TOKEN", "quantity": 8421},
    "stt":       {"provider": "deepgram", "unit": "SECOND", "quantity": 184}
  },
  "reported_costs": { "telephony": "1.84" }
}
```

**Design points:**

- **Idempotent on `provider_call_id`.** The runtime will retry; a duplicate must not double the
  cost. Same discipline as `NotificationLog`'s dedup key.
- **`reported_costs` wins over computed.** If the provider states the real charge, use it and
  set `is_estimated=False`. Rates are the fallback, not the truth.
- **Arrives late.** Cost telemetry follows call completion by seconds or hours. Aggregates must
  tolerate a call with no `CallCost` yet — count it as unpriced, never as zero-cost. A call
  silently costing ₹0 makes cost-per-sale look better than it is.
- Reuse the existing agent-token scoping (`vb_agent_allowed_actions`).

---

## 4. Services

`core/services/costs.py`

| Function | Purpose |
|---|---|
| `rate_for(component, provider, model, unit, *, at)` | Resolves the rate effective at a point in time — tenant first, platform default second |
| `price_usage(usage, *, at)` | Usage dict → itemised costs |
| `ingest(provider_call_id, usage, reported_costs)` | Idempotent upsert |
| `backfill_estimates(since)` | Prices unpriced calls from duration × rate, `is_estimated=True` |
| `totals(tenant, **filters)` | §14 aggregates |
| `unit_economics(tenant, **filters)` | Cost per lead / connected / CTA / sale |
| `ai_roi(tenant, **filters)` | AI-attributed realized revenue ÷ voice cost |

**AI ROI spans two modules.** The numerator comes from [04 — revenue](04-revenue.md)'s
attribution, the denominator from here. It belongs in [06 — analytics](06-analytics.md) as a
composed metric, defined once — not computed independently in three places that drift.

---

## 5. API surface

| Method | Path | Purpose |
|---|---|---|
| POST | `call-costs/ingest/` | Runtime ingestion (agent token) |
| GET | `costs/summary/` | §14 breakdown by component |
| GET | `costs/unit-economics/` | Cost per lead / connected / CTA / sale |
| GET | `costs/by-agent/` | §11 cost per sale by agent |
| GET | `costs/by-campaign/` | §17 |
| GET/POST | `cost-rates/` | Rate management (Enterprise) |

Every cost response carries `is_estimated` and `unpriced_call_count`. **Unpriced calls must be
visible**, not hidden — a cost-per-sale computed over 60% of calls is not a cost per sale, and
the consumer needs to know which one they are looking at.

---

## 6. Background jobs

| Task | Cadence | Purpose |
|---|---|---|
| `price_unpriced_calls` | Every 15 min | Catches late telemetry |
| `backfill_cost_estimates` | Daily | Estimates calls the runtime never reported, flagged |
| `alert_cost_spike` | Hourly | §21 cost-spike alert (module 08) |

---

## 7. Settings

| Setting | Default | Effect |
|---|---|---|
| `COST_ALLOW_ESTIMATES` | `True` | Whether duration-based estimates may be written |
| `COST_ESTIMATE_TELEPHONY_RATE` | `0` | Fallback per-minute rate. Zero ⇒ no estimate rather than a wrong one |
| `COST_SPIKE_THRESHOLD_PCT` | `30` | §21 trigger |
| `COST_UNPRICED_ALERT_PCT` | `20` | Unpriced share that raises a data-quality alert |

`COST_ESTIMATE_TELEPHONY_RATE` defaults to **0 deliberately**: with no rate configured, produce
no estimate. Silence is better than a fabricated unit economic.

---

## 8. Testing

| Area | Must prove |
|---|---|
| Effective dating | A rate change does not restate historical `CallCost` |
| Overlap constraint | Overlapping ranges rejected **by the database** |
| Idempotency | Re-ingesting the same `provider_call_id` does not double cost |
| Reported over computed | Provider-stated cost wins and clears `is_estimated` |
| Precision | Token-scale rates do not round to zero |
| Late arrival | Unpriced calls counted as unpriced, **never as ₹0** |
| Estimate labelling | `is_estimated` survives to the API response |
| Zero rate | No estimate written when the fallback rate is 0 |
| Currency | Mixed-currency costs never silently sum |
| **Structural guard** | Every `CostComponent` value has a resolvable rate path, so a new component cannot ship silently unpriced |

---

## 9. Open decisions

| # | Decision | Blocking? |
|---|---|---|
| 1 | Can the voice runtime emit per-call usage or cost? | **Yes — Phase 0, decides the module** |
| 2 | Are provider rates per tenant, or platform-wide with overrides? | Yes |
| 3 | Are estimates allowed at all, or is unpriced shown as unknown? | Yes — affects DASH-07 |
| 4 | Do human-agent labour costs belong in cost/sale? | §11 implies yes; the BRD never says |
| 5 | Currency of record when providers bill in USD and the tenant reports in INR? | Yes |

Decision 4 is worth surfacing early: §11 ranks agents by *cost per sale* and compares AI
personas against a human team. Without labour cost, that comparison flatters the humans — they
appear to cost nothing.

---

## Related

- [Implementation plan §4.5 and §6.2](../implementation-plan.md)
- [06 — analytics](06-analytics.md) — where AI ROI is composed
- [Agent call logs](../../apiDocs/core/agent_call_log.md) — the row `CallCost` attaches to
- [VB_AGENT scoping](../../apiDocs/core/vb_agent_tenant_scoping.md) — ingestion auth

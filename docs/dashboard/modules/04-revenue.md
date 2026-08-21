# Module 04 — `revenue`

**Opportunity, realized-revenue ledger, attribution and forecast.** The largest new domain, and
the one where a design mistake is most expensive to unwind.

| | |
|---|---|
| **BRD sections** | §13 (Revenue & Forecast), §16 (Pipeline & Revenue Risk), §4 (headline KPIs) |
| **Depends on** | [02 — lead](02-lead.md), [03 — cta](03-cta.md) |
| **Blocks** | 06, 08 |
| **Status** | 🔴 New |
| **Estimate** | ~7–9 dev-weeks (part of Phase 2) |
| **Gated on** | Phase 0 decisions — attribution rule, realized-revenue source |

---

## 1. Do not start before Phase 0

Two decisions in [the implementation plan §6](../implementation-plan.md) must be signed off
first. Building without them means rebuilding.

**§6.1 — the attribution rule.** The BRD requires an attribution *version* on every value
(§25), correctly implying the rule will change. It never states the rule. A sale closed by a
human three weeks after an AI qualification call is 100% AI-attributed, 0%, or something
between — and *AI Revenue*, one of the eight headline KPIs, moves by a multiple depending on
which.

**§6.3 — where realized revenue comes from.** §13 defines it as "confirmed recognized/closed
revenue under tenant revenue source". No such source exists here. `Transaction` is SalesBot's
*own* subscription billing — tenants paying us, not their customers paying them.

---

## 2. Pipeline and realized revenue are separate tables

BRD §25, rule 1: *"Pipeline is never displayed as realized revenue."*

Implement that as **two tables**, not one table with a status column.

A status column means every future query is one forgotten `WHERE` clause away from summing
open pipeline into a revenue headline, and that bug reports as a great quarter. Separate
tables make the mistake a join error — loud, immediate, caught in review.

| | `Opportunity` | `RevenueEvent` |
|---|---|---|
| Represents | Money that *might* arrive | Money that *did* |
| Mutable | Yes — value, probability, close date all move | **No** — append-only ledger |
| Feeds | Pipeline, Weighted Forecast, Coverage, Revenue at Risk | Realized Revenue, AI Revenue, ROI |

---

## 3. Data model

### 3.1 `Opportunity` — Tier 2

| Field | Type | Notes |
|---|---|---|
| `lead` | FK → `Lead`, PROTECT | |
| `name` | `CharField(160)` | |
| `value` | `Decimal(14,2)` | |
| `currency` | `CharField(3)` | Callable default |
| `probability` | `PositiveSmallIntegerField` | 0–100 → §13 weighted forecast |
| `expected_close_date` | `DateField` | |
| `original_close_date` | `DateField` | **Never updated.** §16 slippage = current − original |
| `status` | `CharField(16)`, choices | `OPEN` \| `WON` \| `LOST` \| `ABANDONED` |
| `stage` | FK → `JourneyStage`, PROTECT | |
| `owner` | FK → user, SET_NULL | |
| `closed_at` | `DateTimeField`, null | |
| `lost_reason` | `CharField(64)`, blank | §18 |

### 3.2 `OpportunityLine` — Tier 2

Line items referencing the catalog, so a forecast value has provenance rather than being typed
into a box.

| Field | Type | Notes |
|---|---|---|
| `opportunity` | FK, CASCADE | |
| `variant` | FK → `CatalogVariant`, PROTECT | |
| `quantity` | `Decimal(10,2)` | |
| `unit_price` | `Decimal(14,2)` | **Snapshotted** at add time |
| `line_total` | `Decimal(14,2)` | Denormalised |

> The catalog already models `FIXED` / `RECURRING` / `ON_REQUEST`, `base_price` composites and
> currency, carefully. Reuse it. `unit_price` is snapshotted for the same reason CTA values are:
> a price change must never restate history.
>
> `ON_REQUEST` variants have a null price by design — an opportunity line on one requires a
> manually entered `unit_price`, and that path needs a test.

### 3.3 `RevenueEvent` — Tier 1 Core, append-only

| Field | Type | Notes |
|---|---|---|
| `lead` | FK → `Lead`, PROTECT | |
| `opportunity` | FK, SET_NULL | Null when revenue is inferred without one |
| `amount` | `Decimal(14,2)` | |
| `currency` | `CharField(3)` | |
| `recognized_at` | `DateTimeField` | When the money counts, not when the row was written |
| `source` | `CharField(20)`, choices | `MANUAL` \| `CRM_SYNC` \| `CTA_INFERRED` \| `IMPORT` |
| `is_inferred` | `BooleanField` | **True ⇒ the UI must label it.** §6.3 option (c) |
| `external_ref` | `CharField(120)`, blank | Invoice / payment id |
| `reversal_of` | FK → self, SET_NULL | Corrections append; they never update |

**Never update or delete a `RevenueEvent`.** Corrections write a negative `reversal_of` row.
That is what makes realized revenue auditable per §25, and reconcilable with an external system
that also never rewrites history.

`db_table = "core_revenue_event"`

### 3.4 `Attribution` — Tier 1 Core

| Field | Type | Notes |
|---|---|---|
| `revenue_event` | FK, CASCADE | |
| `lead` / `call` / `campaign` / `agent` | FKs, SET_NULL | The credited entities |
| `channel` | `CharField(16)`, choices | `AI` \| `HUMAN` \| `MIXED` — drives §4 *AI Revenue* |
| `weight` | `Decimal(5,4)` | 0–1. Must sum to 1.0 per revenue event |
| `attribution_version` | `CharField(24)` | **Required by §25.** e.g. `last-touch-v1` |
| `computed_at` | `DateTimeField` | |

**Constraint:** weights per `revenue_event` sum to 1.0. Enforce in the service and assert in
tests — a drifting sum silently inflates or deflates AI Revenue.

### 3.5 `ForecastSnapshot` — Tier 1 Core

| Field | Type | Notes |
|---|---|---|
| `snapshot_date` | `DateField`, indexed | |
| `segment_type` / `segment_id` | `CharField` / `UUIDField`, null | Tenant-wide, team, campaign, product |
| `pipeline_value` | `Decimal(14,2)` | |
| `weighted_value` | `Decimal(14,2)` | |
| `open_count` | `PositiveIntegerField` | |
| `confidence` | `Decimal(5,2)` | §13 forecast confidence |

> ⚠️ **Write this from day one.** §13's *Forecast Movement* — created / advanced / slipped /
> lost / won — is a **diff between two snapshots** and cannot be derived from current state. If
> the table starts late, the KPI is permanently unavailable for every period before it existed,
> and no backfill can invent it. **DASH-02 depends entirely on this.**

---

## 4. Attribution as a swappable strategy

```
core/services/attribution/
    __init__.py       # resolve_strategy() reads settings
    base.py           # AttributionStrategy: attribute(revenue_event) -> [Attribution]
    last_touch.py     # last-touch-v1
    first_touch.py    # first-touch-v1
    linear.py         # linear-v1
    ai_assisted.py    # ai-assisted-v1 — any AI touch inside a window credits AI
```

Every `Attribution` row stores the version that produced it. Recomputation writes new rows
under a new version; it never mutates old ones. That makes restating history a deliberate,
auditable act — and lets you show the old and new numbers side by side when the rule changes,
which is the conversation that will actually happen.

**Ship `last-touch-v1` as the default** and make the setting explicit, so the choice is visible
rather than accidental.

---

## 5. Services

`core/services/revenue.py`

| Function | Purpose |
|---|---|
| `create_opportunity(lead, lines, ...)` | Computes value from catalog lines |
| `update_probability(opp, probability, *, reason)` | Logs to the lead timeline |
| `reschedule_close(opp, new_date)` | Preserves `original_close_date` — §16 slippage |
| `win(opp, *, amount, recognized_at, source)` | → WON, writes `RevenueEvent`, runs attribution, closes the lead |
| `lose(opp, reason)` | → LOST |
| `record_revenue(lead, amount, *, source, is_inferred)` | Revenue without an opportunity (SME path) |
| `reverse(event, reason)` | Appends a negative correction |
| `weighted_forecast(tenant, **filters)` | Σ value × probability |
| `revenue_at_risk(tenant)` | §16 leakage rules |
| `forecast_movement(tenant, since)` | Diff of two snapshots |

---

## 6. API surface

| Method | Path | Purpose | BRD |
|---|---|---|---|
| GET/POST | `opportunities/` | List, create | §13 |
| GET/PATCH | `opportunities/{id}/` | | |
| POST | `opportunities/{id}/win/` | Win + revenue event | §13 |
| POST | `opportunities/{id}/lose/` | | §13 |
| GET | `revenue/summary/` | Realized, pipeline, weighted, AI revenue, coverage | §4, §13 |
| GET | `revenue/forecast/` | Forecast + confidence | §13 |
| GET | `revenue/movement/` | Created / advanced / slipped / lost / won | §13 |
| GET | `revenue/at-risk/` | ₹ + count + causes + owner | §4, §16 |
| GET | `revenue/events/` | The ledger, filterable | §25 audit |

**Every money response carries metadata** — `definition`, `source`, `as_of`,
`attribution_version` where applicable, and `is_inferred`. §25 and DASH-09 require it, and
adding it later means changing every consumer.

---

## 7. Background jobs

| Task | Cadence | Purpose |
|---|---|---|
| `snapshot_forecast` | **Nightly, before rollups** | §3.5. The one job that cannot be added late |
| `evaluate_revenue_risk` | Hourly | Feeds §16 and module 08 |
| `age_stale_opportunities` | Daily | Past expected close and untouched → risk |
| `recompute_attribution` | On demand | New strategy version across a date range |

---

## 8. Settings

| Setting | Default | Effect |
|---|---|---|
| `REVENUE_ATTRIBUTION_STRATEGY` | `last-touch-v1` | Active strategy |
| `REVENUE_AI_TOUCH_WINDOW_DAYS` | `30` | AI-assisted credit window |
| `REVENUE_STALE_OPPORTUNITY_DAYS` | `14` | Untouched ⇒ at risk |
| `REVENUE_SLIPPAGE_ALERT_DAYS` | `7` | Close-date movement that raises an alert |
| `REVENUE_ALLOW_INFERRED` | `True` | Whether CTA completion may write revenue (§6.3 option c) |

---

## 9. Testing

| Area | Must prove |
|---|---|
| Pipeline ≠ revenue | No query path sums `Opportunity.value` into a realized figure |
| Ledger immutability | `RevenueEvent` cannot be updated or deleted; corrections append |
| Attribution weights | Sum to exactly 1.0 for every event, every strategy |
| Version stamping | Recomputation writes new rows and leaves old versions intact |
| Slippage | `original_close_date` survives every reschedule |
| Forecast movement | Correct against two hand-built snapshots with known deltas |
| Catalog lines | `unit_price` snapshot survives a later catalog price change |
| `ON_REQUEST` variants | Null catalog price handled without a crash |
| Inferred revenue | Always carries `is_inferred=True` through to the API |
| Currency | Mixed-currency opportunities never silently sum |
| **Structural guard** | Every money field in every dashboard serializer emits source + freshness metadata |

Mutation-test the weighted forecast and attribution weights. Both produce plausible-looking
numbers when wrong, which is exactly the class of defect no reviewer catches.

---

## 10. Open decisions

| # | Decision | Blocking? |
|---|---|---|
| 1 | Attribution rule for v1 | **Yes — Phase 0** |
| 2 | Realized-revenue source per tenant | **Yes — Phase 0** |
| 3 | Multi-currency: convert at recognition, or report per currency? | Yes, for §4 |
| 4 | Recurring revenue — MRR/ARR recognised monthly or once? (§15 SaaS) | Yes, for SaaS tenants |
| 5 | Can revenue exist without an opportunity? | Recommended yes — it is the SME zero-config path |
| 6 | Who may mark revenue realized? | Role decision |

Decision 4 is easy to miss: §15 lists MRR/ARR as a SaaS KPI, and a recurring subscription
recognised as a single lump is wrong in a way that compounds every month.

---

## Related

- [Implementation plan §4.4 and §6.1/§6.3](../implementation-plan.md)
- [03 — cta](03-cta.md) · [06 — analytics](06-analytics.md) · [08 — alerts](08-alerts.md)
- [Catalog variants — pricing](../../apiDocs/core/catalog_variant.md)

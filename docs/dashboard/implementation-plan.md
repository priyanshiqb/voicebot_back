# SalesBot Executive Dashboard — Implementation Plan

**Source:** SalesBot Executive Sales Dashboard BRD v3.1 (09 Aug 2026)
**Assessed against:** this repository, at the commit this document was written
**Purpose:** what to build, what to extend, what already works, and in what order

---

## 1. The headline finding

The BRD defines its own product hierarchy in §28:

> Revenue → Revenue Moment → Lead Journey → CTA → Agent Effectiveness → Cost → ROI → Action → Insight

**This platform models the input side of that chain and none of the output side.** It is a
conversation-execution product; the BRD describes a revenue-intelligence product built on top
of one. Those are different systems that happen to share a database.

Verified by inspecting the model layer for the BRD's core nouns — not assumed:

| Concept the BRD treats as foundational | Models in this codebase |
|---|---|
| Opportunity / deal | **0** |
| Revenue (realized, pipeline, weighted) | **0** |
| Cost (telephony, LLM, STT/TTS) | **0** |
| Lead stage / journey | **0** |
| CTA (offered / accepted / completed) | **0** |
| Attribution | **0** |
| Forecast | **0** |
| Tenant industry | **0** (a differently-scoped `industry` exists — see §4.1) |
| Analytics or dashboard endpoint | **0** |

Nothing in the repository writes, stores or reads a monetary amount tied to an outcome. The
only money anywhere is `CatalogVariant.price` — a *catalogue* price, never connected to a sale.

**What that means for planning.** Roughly **70% of this BRD is greenfield**, not enhancement.
Any estimate assuming "we have the data, we just need to visualise it" will be wrong by an
order of magnitude. Visualisation is the last 20%; the first 80% is creating the facts the
dashboard claims to display.

> This is not a criticism of either document. The platform was built to run voice conversations
> and does that well. The BRD is coherent and well specified. The gap is simply real, and
> pretending otherwise produces a plan that fails in month two.

---

## 2. Coverage map

Every BRD section, honestly classified.

| § | BRD section | Status | Notes |
|---|---|---|---|
| 4 | Executive Home KPIs | 🔴 **Build** | 8 KPIs; 7 need data that does not exist |
| 5 | CEO Action Center | 🔴 **Build** | Risk rules + ₹ impact + ownership + outcome state |
| 6 | Industry-Native Lead Journey | 🔴 **Build** | No stage model, no tenant industry |
| 7 | Individual Lead Journey View | 🟠 **Extend** | Call history exists; stages, CTA, revenue do not |
| 8 | Lead Journey Analytics | 🔴 **Build** | Depends on §6 |
| 9 | Dynamic CTA Dashboard | 🔴 **Build** | CTA is an entirely new domain |
| 10 | Voice Bot Workforce | 🟠 **Extend** | Activity metrics derivable today; money metrics are not |
| 11–12 | Agent Effectiveness | 🟠 **Extend** | Same split — volume yes, revenue/cost no |
| 13 | Revenue & Forecast | 🔴 **Build** | Entire domain |
| 14 | Cost & Unit Economics | 🔴 **Build** | Entire domain + external dependency (§6.2) |
| 15 | Industry KPI Cards | 🔴 **Build** | Depends on §6 |
| 16 | Pipeline & Revenue Risk | 🔴 **Build** | Depends on §13 |
| 17 | Campaign & Source Economics | 🟠 **Extend** | Campaign structure exists; spend and revenue do not |
| 18 | Conversation Intelligence | 🟢 **Strongest** | `AgentCallLog` already carries most inputs |
| 19 | Intelligent Data Insights Engine | 🔴 **Build** | Needs everything above to exist first |
| 20 | Filters & Drill-Down | 🟠 **Extend** | Filter/pagination infrastructure is mature |
| 21 | Alerts | 🟢 **Reuse** | Notification infrastructure is production-ready |
| 22–24 | Visual design, layout, modes | ⚪ **Frontend** | Out of backend scope; §24 implies a tenant mode setting |
| 25 | Trust rules | 🟠 **Extend** | Isolation and audit are solid; freshness/definition metadata is new |
| 26 | Acceptance criteria | — | Traceability in §8 below |
| 27 | Metric catalog | 🔴 **Build** | Should be an enforced registry, not prose — see §6.4 |

🟢 exists / reusable · 🟠 partial, needs extension · 🔴 does not exist · ⚪ not backend

---

## 3. What already exists and is genuinely reusable

Do not rebuild these. Three are stronger than the BRD assumes.

### 3.1 `AgentCallLog` — the best asset in the repository

Already captures, per call: `outcome`, `mood` + `mood_confidence`, full `transcript`,
`ai_summary`, `probing_questions` (+ count), `duration_seconds`, `follow_up_detail`,
`human_transfer_detail`, `direction`, `contact` FK, `campaign_member` FK, `pii_redacted`.

**This substantially covers BRD §18 (Conversation Intelligence) already.** Intent, objections,
buying signals and lost reasons are *extraction* problems over data already being stored — not
collection problems. §18 is the cheapest section in the BRD to deliver, which is why §7
sequences it out of dependency order.

Missing for §18: a structured objection/signal taxonomy, and per-call cost.

### 3.2 Catalog — real money modelling, already built

`CatalogVariant` supports `FIXED` / `RECURRING` / `ON_REQUEST` pricing, `base_price` for
composite rates (₹50 base + ₹18/km), currency, SKU and unit of measure. It is carefully done.

**This is the deal-value source.** `Opportunity` should reference catalog variants rather than
storing a free-typed amount — that gives every forecast value a defensible provenance, which
§25 ("every financial KPI has definition, source and freshness") demands anyway.

### 3.3 Scheduling & Booking — a working revenue-moment engine

`Booking`, `EventTemplate`, availability profiles/rules/windows, exception handling, calendar
sync, reschedule tokens, and a double-booking `ExclusionConstraint`.

**Three of the BRD's nine revenue moments are appointment-shaped** and already have a
production engine behind them:

| BRD industry | BRD stage | Existing model |
|---|---|---|
| Real Estate | Site Visit | `Booking` |
| Healthcare | Appointment → Visit | `Booking` (+ `status` for show / no-show) |
| Solar | Site Assessment | `Booking` |

`Booking.status` already distinguishes `COMPLETED` / `NO_SHOW` / `CANCELED` — exactly the
Healthcare "show rate" KPI in §15. Wire the CTA model to `Booking` rather than inventing a
parallel appointment concept.

### 3.4 Campaign & Contact — the attribution skeleton

`Campaign` (audience source, schedule, status, per-status counts) and `CampaignMember`
(attempts, outcome, suppression reason) give the source/channel dimension §17 needs. `Contact`
carries consent, do-not-call and `last_contacted_at`.

Missing: campaign **spend** — without it §17's CAC and ROAS are undefined — and a lead
lifecycle on `Contact`.

### 3.5 Infrastructure that is production-ready

| Capability | Reuse for |
|---|---|
| Multi-tenancy (`TenantBaseModel` / `Core`), row-level isolation, server-side enforcement | §25 trust rules |
| Audit fields + soft delete | §25 auditability |
| `NotificationLog` dedup ledger, FCM push, tenant SMTP | §21 Alerts, §19.7 Insights Digest |
| Celery + Beat, retries, `ReportedTask` | Nightly rollups, forecast snapshots, insight generation |
| DRF filtering, ordering, pagination, `dropdown` / `bulk_delete` conventions | §20 Filters |
| drf-spectacular schema + generated-doc regression tests | API contract for every new endpoint |
| Agent tokens with per-viewset action scoping | Voice-runtime writes (§6.2) |

---

## 4. New modules to build

Eight new domains, following the existing `models/ · serializers/ · views/ · services/ · urls/`
layout and the Tier 1 / Tier 2 base-model convention.

### 4.1 `industry` — tenant industry & journey configuration 🔴

**Build first.** BRD §6 is the spine: nine industries, nine funnels, nine revenue moments.
Nothing downstream can exist until "what industry is this tenant, and what are its stages" is
answerable.

- Tenant-level `industry` (the BRD's nine values) on `TenantSettings` or a new
  `TenantIndustryProfile`.
- `JourneyStage` — ordered, per industry, seeded from the BRD's nine funnels, with exactly one
  stage flagged `is_revenue_moment`.
- Seeded defaults so a new tenant is useful with zero configuration (**DASH-08**), overridable
  per tenant (§24 Enterprise mode).

> ⚠️ **Do not reuse `AgentTemplate.industry`.** It exists, but it is a *persona* taxonomy
> (`CLINIC_RECEPTIONIST`, `AGRICULTURAL_SUPPLIER`, `RESTAURANT`, …) with 7 values, only ~2 of
> which overlap the BRD's 9 — and it is scoped to a template, not a tenant. Conflating them
> gives you a tenant whose dashboard funnel is decided by whichever agent template was created
> last. Add a new, separate field.

### 4.2 `lead` — lead lifecycle 🔴

`Contact` is an address-book entry, not a lead: no stage, no score, no owner, no journey.

- `Lead` referencing `Contact`, with `stage` (FK → `JourneyStage`), `score`, `owner`,
  `source` / `campaign`, `entered_stage_at`, `status`.
- `LeadStageTransition` — append-only, one row per movement, with actor (AI / human / system)
  and reason. **This is what makes §7 and §8 possible**: velocity, leakage and AI-influence are
  all queries over this table.
- `LeadEvent` — the unified timeline (call, message, CTA, stage change, handoff), which is
  literally §7's Timeline requirement.

> **Keep `Lead` separate from `Contact`** rather than adding fields to it. One person can be a
> lead more than once — a lost real-estate enquiry returning a year later is a new journey, not
> a mutated old one — and §7 asks for "days in journey", which is meaningless on a contact that
> has had three.

### 4.3 `cta` — call-to-action tracking 🔴

The BRD's measurement point. §9 needs offered / accepted / completed / failed **with reasons**,
per agent and per campaign.

- `CTADefinition` — per industry / tenant, with `target_stage` and expected value.
- `CTAEvent` — per lead, per offer: state, timestamps, failure reason, and a **polymorphic link
  to the fulfilling object** (`Booking` for a site visit, `Opportunity` for a quote).

> The dynamic escalation in §9 (Compare → Site Visit → Booking as intent rises) is a
> *sequencing rule over definitions*, not a hard-coded ladder. Model it as ordered definitions
> with entry conditions, so §19.5 can propose changes to it as configuration rather than
> silently rewriting behaviour — which the BRD explicitly requires.

### 4.4 `revenue` — opportunity, deal, attribution 🔴

The core of §13 and the largest new domain.

- `Opportunity` — value (from catalog variants), currency, probability, expected close, stage,
  status.
- `RevenueEvent` — the realized-money ledger. Immutable, one row per recognised amount.
- `Attribution` — links a `RevenueEvent` to lead / call / campaign / agent, **with an
  `attribution_version`** (§25 requires it explicitly).
- `ForecastSnapshot` — nightly frozen forecast, so §13's *Forecast Movement*
  (created / advanced / slipped / lost / won) is computable.

> **Two structural points that are expensive to retrofit.**
>
> Forecast Movement is a **diff between snapshots** and cannot be derived from current state.
> If `ForecastSnapshot` is not written from day one, this KPI is permanently unavailable for
> all history before it is added.
>
> Pipeline must be **physically separable** from realized revenue (§25, rule 1). Enforce it as
> separate tables, not a status column on one, so the two can never be summed by accident in a
> query someone writes eighteen months from now.

### 4.5 `cost` — unit economics 🔴

§14 wants telephony, AI/LLM, STT/TTS, messaging and enrichment cost, per call and aggregated.

- `CostRate` — per tenant / provider / model, **effective-dated**. Rates change; historical
  cost must not silently restate when someone updates one.
- `CallCost` — per `AgentCallLog`, itemised by component.
- Derived: cost per lead / connected / CTA / sale, and AI ROI.

⚠️ **Hard external dependency — see §6.2.**

### 4.6 `analytics` — aggregation & KPI service 🔴

There are **no analytics endpoints at all** today. This is the query layer behind §4, §8, §10,
§11, §15, §16 and §17.

- Pre-aggregated daily rollups per tenant × dimension. §26 promises a 30-second CEO answer;
  computing eight KPIs live across joins of call logs, leads, CTAs, opportunities and costs
  will not hold at scale, and the first busy quarter is exactly when it matters most.
- A **metric registry**: one definition per metric, with its source, freshness and description,
  from which the API serves both the value *and* its metadata. This is how §25's
  "definition, source and freshness" becomes structural rather than a promise — and how §27's
  Metric Catalog becomes a live artefact instead of a document that drifts.

### 4.7 `insights` — the intelligence engine 🔴

BRD §19, and the section most likely to be underestimated.

| § | Capability | Approach |
|---|---|---|
| 19.1 | Auto-narrative | LLM over the tenant's own aggregates, grounded in retrieved metric rows — never templated, per the BRD |
| 19.2 | Anomaly detection | Statistical, against the tenant's **own** rolling baseline (the BRD is explicit: not a platform-wide threshold) |
| 19.3 | What-if | Deterministic recompute of forecast with recovery assumptions applied — **not** a model |
| 19.4 | Peer benchmarking | Cross-tenant cohort aggregates, opt-in, k-anonymity floor |
| 19.5 | Pattern discovery | Correlation mining, surfaced as CTA config *suggestions* |
| 19.6 | Confidence framework | `Insight` model with type, confidence, evidence refs, expiry |

> §19.4 is the **only feature in the BRD that reads across tenant boundaries**, while every
> rule in §25 says isolation is absolute. Treat it as a separate, explicitly consented
> subsystem with its own aggregate store and a minimum-cohort-size guard — never as a query
> that reaches into other tenants' live tables.

### 4.8 `alerts` — risk detection & CEO Action Center 🔴

§5 and §21. A rules engine producing prioritised, owned, SLA-bound actions.

`ActionItem` needs a lifecycle (`recovered` / `progressed` / `lost` / `dismissed`) — §5 lists
Outcome as a field, which makes the Action Center **stateful**, not a rendered query. That
distinction drives the whole design.

**Delivery is already solved:** reuse `NotificationLog` for dedup and the existing push/email
paths for §21 and the §19.7 digest.

---

## 5. Existing modules to update

| Module | Change | Why |
|---|---|---|
| `TenantSettings` | Add `industry`, `dashboard_mode` (SME / Enterprise), currency, fiscal period | §6, §24 |
| `Contact` | Add `owner`; leave lifecycle to `Lead` | §7 identity header |
| `AgentCallLog` | Add FK to `Lead`; add structured `intent`, `objections`, `buying_signals`; link `CallCost` | §18, §7 timeline |
| `AgentCallLog.campaign_id` | Migrate `CharField` → FK | Attribution integrity — free-text campaign ids cannot be joined reliably (§17) |
| `Campaign` | Add `spend` / budget | Without it §17's CAC and ROAS are undefined |
| `Booking` | Link to `CTAEvent` / `Lead` | Turns the existing booking engine into a revenue moment (§3.3) |
| `CatalogVariant` | **No change** | Referenced by `Opportunity`; already correct |
| `AgentTemplate.industry` | **Leave alone** | Different concept — see the §4.1 warning |
| Beat schedule | Add rollup, forecast-snapshot, insight-generation, alert-evaluation jobs | §13, §19, §21 |
| `docs/` | New API docs + a dashboard flow doc | Existing convention |

---

## 6. The four hard problems

Everything above is ordinary engineering. These four are not, and each can quietly sink the
programme if treated as a detail.

### 6.1 Attribution is a policy decision, not a schema

The BRD says "AI Revenue = revenue attributed to Voice Bot" and requires an attribution
*version* on every value (§25) — correctly implying the rule will change. **But it never states
the rule.**

First touch? Last touch? Any AI touch inside a window? Split with human agents? A real-estate
sale closed by a human three weeks after an AI qualification call is either 100% AI-attributed,
0%, or something between — and the AI ROI headline KPI moves by a multiple depending on which.

**Action:** get this decided and signed off *before* building §13. Implement it as a versioned,
swappable strategy, and stamp every attribution record with the version that produced it, so
restating history is a deliberate and auditable act.

### 6.2 Cost data does not originate in this system

§14 wants telephony minutes × rate, LLM tokens × rate, STT/TTS usage. **None of it is captured
here.** `AgentCallLog` stores `duration_seconds` and nothing about spend, models or providers.
Those facts live in the voice runtime and the model providers.

The dashboard cannot invent them. Duration × a flat rate is an *estimate*, not a unit economic,
and presenting it as the latter would breach §25.

**Action:** confirm the voice runtime can emit per-call cost telemetry, and treat that
integration as a **prerequisite** for §14, §10's economic rows and the AI ROI headline KPI. If
it cannot, re-scope §14 to estimated cost, explicitly labelled as estimated.

### 6.3 "Realized revenue" has no source of truth

§13 defines it as "confirmed recognized/closed revenue under tenant revenue source". No such
source exists here: no invoicing, no payments, no CRM sync. `Transaction` exists but is
**SalesBot's own subscription billing** — the tenant paying us, not the tenant's customers
paying them.

**Action:** decide, per tenant, whether realized revenue is (a) manually confirmed in-product,
(b) synced from an external CRM/ERP, or (c) inferred from a completed CTA. Option (c) is the
only zero-setup path and is what makes **DASH-08** achievable for SMEs — but it is an inference
and must be labelled as one wherever it appears.

### 6.4 The metric catalog must be executable

§27 lists ~50 metrics across seven categories, and §25 requires each to expose definition,
source and freshness. As a prose table it will drift from the code within one release.

**Action:** build §4.6's registry as the single definition, generate §27 from it, and add a
test asserting that every metric the API can serve has a registry entry — the same structural-
guard approach already used elsewhere in this codebase.

---

## 7. Phased delivery

Sequenced by dependency, not by BRD order. Each phase ends with something demonstrable.

### Phase 0 — Decisions (blocking, no code)
Attribution rule · realized-revenue source · voice-runtime cost telemetry · peer-benchmark
consent policy. **Phases 2+ cannot start without these** — §6 explains why.

### Phase 1 — Foundation
`industry` + `lead` + stage transitions + timeline. Seed the nine BRD funnels.
**Ships:** §6, and §7 partially (timeline without revenue).

### Phase 2 — Measurement
`cta` + `revenue` (Opportunity, RevenueEvent, Attribution, ForecastSnapshot).
**Ships:** §9, §13 pipeline/forecast, revenue linkage in §7, §8 leakage.

### Phase 3 — Economics
`cost` + campaign spend. **Gated on Phase 0's cost-telemetry answer.**
**Ships:** §14, §10 and §11 economic rows, §17 CAC/ROAS, AI ROI.

### Phase 4 — Surface
`analytics` rollups + metric registry + all read APIs + §20 drill-down.
**Ships:** §4, §8, §10, §11, §15, §16, §17, §20 — the first genuinely usable dashboard.

### Phase 5 — Intelligence
`alerts` (§5, §21), then `insights` (§19).
**Ships:** the CEO Action Center, and the difference between a report and a product.

### Phase 6 — Polish
SME / Enterprise modes, digest delivery, peer benchmarking, export.

> **Conversation Intelligence (§18) is the exception to this ordering.** It depends on almost
> nothing new — `AgentCallLog` already holds the inputs — so it can run in parallel from Phase 1
> and is the fastest visible win in the programme. Sequence it early for exactly that reason.

---

## 7.1 Effort estimate

Estimated two ways, from this repository rather than from intuition. Both land in the same
place, which is the only reason I'd quote a number at all.

### Method A — calibrate against what this codebase already cost

Measured, per domain, counting production + test + documentation lines:

| Existing domain | Models | Prod | Tests | Docs | Total |
|---|---:|---:|---:|---:|---:|
| Scheduling & booking | 12 | 4,345 | 7,351 | 5,282 | 16,978 |
| Catalog | 4 | 4,143 | 5,773 | 2,190 | 12,106 |
| Contacts | 3 | 3,310 | 1,720 | 1,860 | 6,890 |
| Campaigns | 3 | 2,226 | 1,968 | 2,324 | 6,518 |
| Device tokens | 1 | 1,079 | 1,501 | 656 | 3,236 |

Whole repository: **52 models · 46,717 prod · 29,954 test · 31,400 docs = 108,071 lines**, or
**~2,078 lines per model** at this project's quality bar — a 0.64 test-to-production ratio,
plus documentation for every endpoint.

The eight new modules in §4 are ~20 models:

> industry 2 · lead 3 · cta 2 · revenue 4 · cost 2 · analytics 3 · insights 2 · alerts 2

20 × 2,078 ≈ **41,500 lines**, or **+38% on top of the entire existing codebase**.

That is a **floor**, because two of the eight are not line-proportional to model count:
`analytics` is a query and rollup layer with ~20 endpoints behind three models, and `insights`
is six sub-capabilities behind two. Adjusted: **45,000–55,000 lines**.

### Method B — bottom-up by phase

| Phase | Scope | Dev-weeks | Confidence |
|---|---|---:|---|
| 0 | Decisions (no code) | 0 | Calendar only — 1–2 weeks elapsed |
| 1 | `industry` + `lead` + 9 seeded funnels | 8–10 | High |
| 2 | `cta` + `revenue` + attribution + snapshots | 12–14 | Medium-high |
| 3 | `cost` + campaign spend | 4–6 | **Low — gated on external telemetry** |
| 4 | `analytics` rollups + registry + read APIs | 14–18 | Medium |
| 5 | `alerts` (5–6) + `insights` (12–16) | 16–20 | **Lowest — see below** |
| 6 | Modes, digest, benchmarking, export | 4–6 | Medium |
| — | §18 Conversation Intelligence (parallel) | 3–4 | High |
| | **Total** | **~62–78** | |

Method A at a sustainable 130–150 finished lines per developer-day gives 64–78 dev-weeks. The
two methods agree, which is the main reason to trust the range.

### Headline

**≈ 65–80 developer-weeks of backend engineering.**

| Backend developers | Calendar | Note |
|---|---|---|
| 1 | 15–18 months | Not advisable — single point of failure over that span |
| 2 | 8–10 months | Realistic |
| **3** | **6–7.5 months** | **Best value** — phases 4 and 5 parallelise, §18 runs alongside |
| 4+ | 5–6 months | Diminishing: phases 1→2→4 are serially dependent |

**Adding people does not compress this much below six months.** You cannot build revenue
analytics before the revenue model exists, or the revenue model before the lead journey does.

### Explicitly excluded

| Not counted | Why |
|---|---|
| Frontend (BRD §22–24) | Separate discipline; plausibly comparable effort, runs in parallel |
| Voice-runtime cost telemetry | Different codebase — see §6.2 |
| Phase 0 decision-making | Calendar time, not developer time, but **blocking** |
| External CRM/ERP integration | Only if §6.3 resolves to option (b) |
| Data migration / backfill | Unknowable until the realized-revenue source is decided |

### Where the estimate is weakest

**§19 Insights (12–16 weeks) is the least reliable figure in this table.** It is six distinct
capabilities, and three of them — auto-narrative, pattern discovery, peer benchmarking — are
iterative quality problems rather than build problems. "Working" is a judgement call, not a
passing test. If the programme runs late, it will run late here.

**Phase 3 could be near-zero or a whole extra project**, depending entirely on whether the
voice runtime already emits per-call cost. That single answer moves the total by several weeks
and decides whether DASH-07 is deliverable at all.

---

## 8. Acceptance-criteria traceability

| ID | Depends on | Blocked by |
|---|---|---|
| DASH-01 | §4 KPIs | revenue + cost domains |
| DASH-02 | forecast movement | `ForecastSnapshot` written from day one |
| DASH-03 | agent scorecard | attribution + cost |
| DASH-04 | industry funnel | Phase 1 |
| DASH-05 | journey analytics | Phases 1 + 2 |
| DASH-06 | CTA → revenue trace | Phase 2 |
| DASH-07 | cost visibility | **external cost telemetry (§6.2)** |
| DASH-08 | zero-config SME value | seeded journeys + inferred revenue (§6.3) |
| DASH-09 | definition / source / freshness | metric registry (§6.4) |
| DASH-10 | no volume-only KPI | design rule — enforce in the registry |
| DASH-11 | one-click lead journey | Phase 1 + a drill-down contract in every list API |
| DASH-12 | insight confidence | Phase 5 |

**DASH-11 has a design consequence worth acting on immediately:** *every* list endpoint the
dashboard consumes must return a lead identifier, or the one-click guarantee fails in exactly
the places it matters. Cheap to honour from the start; expensive to retrofit across a dozen
endpoints later.

---

## 9. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Cost telemetry unavailable from the voice runtime | §14, AI ROI and DASH-07 undeliverable | Resolve in Phase 0; fall back to explicitly-labelled estimates |
| Attribution rule unsigned | §13 rebuilt late; all historical AI-revenue restated | Phase 0 gate |
| No realized-revenue source | The headline KPI is an inference | Per-tenant strategy, labelled as such |
| Live aggregation at scale | The 30-second promise (§26) fails in the first busy quarter | Rollups from Phase 4, not later |
| Peer benchmarking vs tenant isolation | Data-governance incident | Opt-in, separate store, k-anonymity floor |
| Nine industries × configurable journeys | Combinatorial test surface | Seeded defaults + per-industry fixtures |
| Insight quality | §19 erodes trust in the whole dashboard | Confidence + evidence enforced in the model, per §19.6 |

---

## 10. What this document is not

It does not cover frontend implementation (BRD §22–24), and its effort estimate (§7.1) is a
range derived from this codebase, not a commitment — Phase 0's answers can move it materially. It assumes the BRD is correct about *what* the
business wants, and assesses only *how* this codebase gets there.

---

## Related

- [Command Runbook](../runbook.md)
- [Push Notifications](../flows/push-notifications.md) — reusable alert delivery
- [Campaigns](../campaigns/README.md) — existing source/campaign structure
- [Booking flow internals](../flows/booking-flow-internals.md) — the revenue-moment engine

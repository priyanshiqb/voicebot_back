# Module 09 — Changes to existing modules

**Every change to code that already ships.** Including §18 Conversation Intelligence, which is
not a new module — it is extraction over data `AgentCallLog` already stores.

| | |
|---|---|
| **BRD sections** | §18, §17, §24, §25, plus support for 01–08 |
| **Status** | 🟠 Extend |
| **Estimate** | ~4–5 dev-weeks, spread across phases |

---

## 1. Summary

| Module | Change | Phase | Risk |
|---|---|---|---|
| `TenantSettings` | Industry, mode, currency, fiscal year | 1 | Low |
| `Contact` | `owner` | 1 | Low |
| `AgentCallLog` | `lead` FK, conversation-intelligence fields, `CallCost` reverse | 1–3 | **Medium — large table** |
| `AgentCallLog.campaign_id` | `CharField` → FK | 2 | **Medium — data migration** |
| `Campaign` | `spend`, `budget`, `spend_currency` | 3 | Low |
| `Booking` | `cta_event` reverse link | 2 | Low |
| `AgentTemplate` | **No change** | — | — |
| `CatalogVariant` | **No change** | — | — |
| Beat schedule | ~12 new tasks | 1–5 | Low |
| Docs | New API + flow docs | All | Low |

---

## 2. §18 Conversation Intelligence — the fast win

### 2.1 What already exists

`AgentCallLog` captures, per call: `outcome`, `mood` + `mood_confidence`, full `transcript`,
`ai_summary`, `probing_questions` (+ count), `duration_seconds`, `follow_up_detail`,
`human_transfer_detail`, `direction`, `contact` FK, `campaign_member` FK, `pii_redacted`.

Mapped against §18's requirements:

| §18 signal | Status |
|---|---|
| Intent (hot/warm/researching + confidence) | 🟠 `mood` + `mood_confidence` exist; intent is a different axis and needs its own field |
| Objections + ₹ at risk | 🔴 In the transcript, unstructured |
| Buying signals | 🔴 In the transcript, unstructured |
| CTA recommendation | 🔴 Module 03 |
| Quality / compliance | 🔴 New |
| Lost reasons | 🟠 `Lead.lost_reason` (module 02) + extraction |
| Revenue evidence chain | 🔴 Modules 02–04 |

**Nothing here requires new data collection.** That is why §18 can run from day one, in
parallel with Phase 1, and is the fastest visible progress in the programme.

### 2.2 New fields on `AgentCallLog`

| Field | Type | Notes |
|---|---|---|
| `lead` | FK → `Lead`, SET_NULL | Ties the call to a journey |
| `intent` | `CharField(16)`, choices | `HOT` \| `WARM` \| `RESEARCHING` \| `UNQUALIFIED` \| `UNKNOWN` |
| `intent_confidence` | `FloatField`, null | |
| `objections` | `JSONField` | `[{code, quote, at_second, confidence}]` — **ordered**, per §7 |
| `buying_signals` | `JSONField` | Same shape |
| `quality_score` | `PositiveSmallIntegerField`, null | 0–100, §11 quality/compliance |
| `compliance_flags` | `JSONField` | Disclosure, consent, script adherence |
| `analysis_version` | `CharField(24)` | Which extractor produced this — reprocessing must be traceable |
| `analyzed_at` | `DateTimeField`, null | Freshness, per §25 |

> **BRD §7 requires "every objection and buying signal detected, in the order they occurred, not
> just the latest one."** Store arrays with `at_second`, not a single latest value. A schema
> that keeps only the most recent objection cannot satisfy §7, and discovering that after the
> extractor ships means re-analysing every call.

### 2.3 Objection taxonomy

A closed set, or §18's "top clusters + ₹ at risk" cannot aggregate:

```python
class ObjectionCode(models.TextChoices):
    PRICE_TOO_HIGH      = "PRICE_TOO_HIGH"
    NEEDS_APPROVAL      = "NEEDS_APPROVAL"
    TIMING_NOT_RIGHT    = "TIMING_NOT_RIGHT"
    COMPETITOR_PREFERRED= "COMPETITOR_PREFERRED"
    TRUST_CONCERN       = "TRUST_CONCERN"
    FEATURE_GAP         = "FEATURE_GAP"
    LOCATION_UNSUITABLE = "LOCATION_UNSUITABLE"
    ELIGIBILITY_DOUBT   = "ELIGIBILITY_DOUBT"
    NO_BUDGET           = "NO_BUDGET"
    OTHER               = "OTHER"
```

Monitor the `OTHER` share. A taxonomy where 40% lands in `OTHER` is not a taxonomy, and §18's
clusters will be meaningless — treat a rising `OTHER` rate as a signal to extend the set.

### 2.4 Extraction

`core/services/conversation_intelligence.py`, run as a task after call completion:

1. Transcript → LLM with a structured-output schema.
2. Persist with `analysis_version` and `analyzed_at`.
3. Emit a `LeadEvent`.
4. Reprocessing writes a new version; it never overwrites blind.

**Respect `pii_redacted`.** The field exists; extraction must not undo redaction by copying raw
quotes into `objections`. Store the quote only when redaction permits it, and store the code
and timestamp always.

---

## 3. `AgentCallLog.campaign_id` — CharField to FK

Today `campaign_id` is a `CharField` sitting beside a `campaign_member` FK.

**Why it must change:** §17 attributes revenue by campaign. A free-text id cannot be joined
reliably — typos, deleted campaigns and runtime format drift all produce silent
under-attribution, which reads as a campaign performing badly rather than as a data defect.

**Migration, in four deploys:**

1. Add `campaign` FK, nullable. Deploy.
2. Backfill: match `campaign_id` to `Campaign.id`, and via `campaign_member.campaign` where
   present. **Log every unmatched row** — do not discard them silently.
3. Dual-write from the runtime; verify agreement over a full cycle.
4. Read from the FK; keep the CharField for one release, then drop it.

Expect unmatchable rows from before campaigns existed. Decide up front whether those are null
or a synthetic "unknown campaign" — the choice changes every §17 denominator.

---

## 4. Field-level changes

### 4.1 `TenantSettings`

Currently holds a single field (`booking_email_configuration`). Add, or delegate to
`TenantIndustryProfile` from [module 01](01-industry.md) — recommended, since journey config is
audited business configuration:

| Field | Purpose |
|---|---|
| `industry` | §6 |
| `dashboard_mode` | §24 SME vs Enterprise |
| `currency` | §4 |
| `fiscal_year_start_month` | §13 target periods |
| `timezone` | §20 period boundaries — see [06 §8](06-analytics.md) decision 4 |

### 4.2 `Contact`

| Field | Purpose |
|---|---|
| `owner` | FK → user, SET_NULL. §5 ownership before a lead exists |

Lifecycle stays on `Lead` — see [02 §2](02-lead.md).

### 4.3 `Campaign`

| Field | Purpose |
|---|---|
| `spend` | `Decimal(14,2)`. **Without it §17's CAC and ROAS are undefined** |
| `budget` | `Decimal(14,2)`, null |
| `spend_currency` | `CharField(3)` |
| `spend_source` | `CharField(16)` — `MANUAL` \| `IMPORT` \| `AD_PLATFORM` |

Spend is external (ad platforms, media buys). Manual entry is the v1 path; say so in the UI
rather than leaving a zero that looks like free acquisition.

### 4.4 `Booking`

No new fields — `CTAEvent.booking` is the forward FK. Add `related_name="cta_events"` so a
booking can report which CTA produced it, for §9's completion trace.

---

## 5. `AgentCallLog` migration risk

This is the highest-volume table receiving changes. Guidance:

- Add columns **nullable with no default** — a non-null default rewrites the table.
- Add indexes `CONCURRENTLY`; Django's `AddIndexConcurrently` exists for this.
- Backfill `lead` in batches via a management command, not a data migration — a migration
  holding a transaction over millions of rows will not finish cleanly.
- Split across releases: columns first, backfill second, index third, read fourth.

The existing codebase already batches this way in `purge_expired_sessions` and
`purge_device_tokens`; follow that shape.

---

## 6. Beat schedule additions

| Task | Cadence | Module |
|---|---|---|
| `mark_dormant_leads` | Daily | 02 |
| `recompute_lead_scores` | Hourly | 02 |
| `reconcile_lead_events` | Daily | 02 |
| `expire_stale_cta_offers` | Hourly | 03 |
| `reconcile_cta_bookings` | Daily | 03 |
| `snapshot_forecast` | **Nightly, first** | 04 |
| `evaluate_revenue_risk` | Hourly | 04 |
| `age_stale_opportunities` | Daily | 04 |
| `price_unpriced_calls` | 15 min | 05 |
| `rollup_metrics` | **Nightly, after snapshot** | 06 |
| `rollup_period_aggregates` | Nightly | 06 |
| `detect_anomalies` | Hourly | 07 |
| `generate_narratives` | Nightly, after rollups | 07 |
| `discover_patterns` | Weekly | 07 |
| `refresh_peer_aggregates` | Weekly | 07 |
| `send_insights_digest` | Daily/weekly | 07 |
| `evaluate_alert_rules` | Hourly | 08 |
| `sweep_sla_breaches` | 15 min | 08 |
| `analyze_call_transcripts` | On completion | 09 |

> **The nightly chain is ordered:** `snapshot_forecast` → `rollup_metrics` →
> `rollup_period_aggregates` → `generate_narratives`. Celery Beat does not express
> dependencies, so either chain them explicitly or space them and assert ordering in a test.
> Getting this wrong produces a dashboard that is silently one day stale — the hardest kind of
> bug to notice, because every number looks reasonable.

---

## 7. Documentation

Following the existing convention:

| Doc | Covers |
|---|---|
| `apiDocs/core/leads.md` | §7, §8 |
| `apiDocs/core/cta.md` | §9 |
| `apiDocs/core/revenue.md` | §13, §16 |
| `apiDocs/core/costs.md` | §14 |
| `apiDocs/core/analytics.md` | All read endpoints |
| `apiDocs/core/insights.md` | §19 |
| `apiDocs/core/action-center.md` | §5, §21 |
| `flows/executive-dashboard.md` | End-to-end with diagrams |
| `dashboard/metric-catalog.md` | **Generated from the registry** — §27 |
| `commands/` | One page per new maintenance command |

Update `_sidebar.md` and `.env.example` as each module lands, not at the end.

---

## 8. Testing

| Area | Must prove |
|---|---|
| `campaign_id` migration | Backfill matches known rows; unmatched are logged, not dropped |
| Dual-write | FK and CharField agree during transition |
| Objection ordering | Multiple objections preserved in occurrence order (§7) |
| PII | Extraction never writes raw quotes for `pii_redacted` calls |
| Analysis versioning | Reprocessing writes a new version, keeps the old |
| `AgentCallLog` migration | Applies against a large fixture within budget |
| Beat ordering | Snapshot precedes rollup precedes narrative |
| **Structural guard** | Every `ObjectionCode` is reachable from the extractor's schema |

---

## 9. Open decisions

| # | Decision | Default if unanswered |
|---|---|---|
| 1 | Are historical calls back-analysed for §18, or only new ones? | New only; backfill on request — cost scales with transcript volume |
| 2 | Unmatched `campaign_id` rows — null or synthetic campaign? | Null, counted and reported |
| 3 | Does extraction run on every call or a sample? | Every call; revisit if LLM cost is material |
| 4 | Is `Contact.owner` or `Lead.owner` authoritative for §5? | `Lead.owner`, falling back to `Contact.owner` |

Decision 3 interacts with [module 05](05-cost.md): per-call transcript analysis is itself an LLM
cost, and it should appear in `CallCost` rather than being invisible platform overhead.

---

## Related

- [Module index](../README.md) · [Implementation plan §5](../implementation-plan.md)
- [Agent call logs API](../../apiDocs/core/agent_call_log.md)
- [Campaigns](../../campaigns/README.md)
- [Command runbook](../../runbook.md) — migration and test invocation

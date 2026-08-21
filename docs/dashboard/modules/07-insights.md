# Module 07 — `insights`

**The intelligence engine.** BRD §19 — the difference between a reporting tool and a product,
and the least estimable module in the programme.

| | |
|---|---|
| **BRD sections** | §19 (all sub-sections), §22 (visual distinction), §25 (trust) |
| **Depends on** | [06 — analytics](06-analytics.md) — needs rollups to exist |
| **Blocks** | Nothing |
| **Status** | 🔴 New |
| **Estimate** | ~12–16 dev-weeks — **lowest confidence in the plan** |

---

## 1. Why the estimate is soft

Six capabilities, and three of them — auto-narrative, pattern discovery, peer benchmarking —
are **iterative quality problems, not build problems**. "Working" is a judgement call, not a
passing test. You can build the pipeline in three weeks and spend nine more making the output
worth reading.

Plan for that explicitly. If the programme runs late, it runs late here.

---

## 2. The rule that governs the whole module

BRD §19.7:

> An insight is not a decoration. Every insight must answer 'why do you believe this' with the
> same evidence standard as a CEO Action Center alert. A prediction is never displayed as a
> certainty.

And §22: generated insights must be **visually distinct from raw metrics — never presented as
if they were a directly measured KPI**.

**Structural consequence:** confidence, evidence and type are required fields on the model, not
optional decoration. An insight that cannot cite its evidence must not be storable. Enforce it
in the model, and DASH-12 follows.

---

## 3. Data model

### 3.1 `Insight` — Tier 1 Core

| Field | Type | Notes |
|---|---|---|
| `insight_type` | `CharField(12)`, choices | `DESCRIPTIVE` \| `DIAGNOSTIC` \| `PREDICTIVE` \| `PRESCRIPTIVE` — §19.6 requires the label |
| `capability` | `CharField(20)`, choices | Which §19 sub-engine produced it |
| `headline` | `CharField(255)` | The sentence a RevOps analyst would have written |
| `body` | `TextField`, blank | Optional detail |
| `confidence` | `CharField(8)`, choices | `HIGH` \| `MEDIUM` \| `LOW` — **required** |
| `confidence_score` | `Decimal(4,3)`, null | The underlying number, when there is one |
| `sample_size` | `PositiveIntegerField` | §19.6 evidence |
| `evidence` | `JSONField` | Metric keys, periods, record ids, thresholds |
| `metric_keys` | `ArrayField(CharField)` | Registry keys this concerns |
| `segment_type` / `segment_id` | `CharField` / `UUIDField`, null | Campaign, agent, stage |
| `period_start` / `period_end` | `DateField` | |
| `expires_at` | `DateTimeField` | §19.6 — insights are retired, not left stale |
| `status` | `CharField(12)`, choices | `ACTIVE` \| `EXPIRED` \| `DISMISSED` \| `ACTIONED` |
| `generator_version` | `CharField(24)` | Which engine version — same discipline as attribution |
| `model_name` | `CharField(64)`, blank | LLM used, where one was |
| `dedup_key` | `CharField(160)` | Stops the same insight regenerating nightly |

**Unique:** `(tenant, dedup_key)` among non-expired rows — the same pattern `NotificationLog`
already uses. Without it a nightly generator re-tells the same story every morning and the
digest becomes noise within a week.

`db_table = "core_insight"`

### 3.2 `PeerAggregate` — Tier 1 Core, **not tenant-scoped**

The only cross-tenant table in the system. §19.4.

| Field | Type | Notes |
|---|---|---|
| `industry` | `CharField(32)` | Cohort key |
| `metric_key` | `CharField(64)` | |
| `period_type` / `period_start` | | |
| `tenant_count` | `PositiveIntegerField` | **Enforce a floor** — see §5 |
| `p25` / `p50` / `p75` / `p90` | `Decimal(18,4)` | Percentiles only — never raw values |

`db_table = "core_peer_aggregate"`

---

## 4. The six capabilities

### 4.1 Auto-narrative (§19.1)

> "Revenue fell 12% this week — driven by a 40% drop in real-estate site-visit bookings,
> concentrated in the Whitefield campaign."

**Approach:** a deterministic *finder* identifies the largest contributing movement across
rollup dimensions; an LLM renders only the sentence, grounded in the retrieved rows.

**The finder does the analysis, not the model.** The LLM must never be asked "why did revenue
fall" against raw data — it will confabulate a plausible cause. Give it the computed
attribution and let it write English. Evidence then cites real rollup rows, which is what §19.7
demands.

The BRD is explicit that this is "never a templated fill-in-the-blank", so a template engine
does not satisfy it — but nor does an ungrounded model.

### 4.2 Anomaly detection (§19.2)

> "CTA completion rate is 2.1 standard deviations below your own 90-day average."

**Against the tenant's own rolling baseline**, per the BRD — not a platform-wide threshold.
What is normal for one real-estate tenant is an anomaly for another.

Rolling mean and standard deviation over the trailing window per `(metric_key, dimension)`.
Requires a minimum history before it may fire; a two-week-old tenant has no baseline, and
firing anyway produces nonsense on day three of a customer relationship.

Seasonality (weekday/weekend) matters — a Sunday dip is not an anomaly. Start with
day-of-week-aware baselines rather than a naive rolling window.

### 4.3 Predictive / what-if (§19.3)

> "If the 12 stalled real-estate leads in the Action Center are recovered this week, forecast
> increases by ₹14L."

**Deterministic recomputation, not a model.** Take the current forecast, apply the recovery
assumption to a named set of leads, recompute. It is arithmetic over module 04's weighted
forecast, and it is fully explainable — which is why it should never be an ML problem.

Inputs come from module 08's Action Center, which makes the two modules a pair: the alert
proposes the action, this quantifies it.

### 4.4 Peer benchmarking (§19.4)

**The only feature in the BRD that crosses tenant boundaries**, against §25's absolute
isolation rule. Treat as a separate subsystem — see §5.

### 4.5 Pattern discovery (§19.5)

> "Leads that ask about financing convert 2.4× more often when contacted within 10 minutes."

Correlation mining over lead attributes, CTA outcomes, timing and conversation signals.

**Output is a suggestion, never an automatic change.** The BRD says so directly, and module 03
was designed for it: CTA escalation is ordered configuration precisely so a discovered pattern
can be proposed as a config diff a human approves.

Guard against spurious correlation — mining fifty attributes will find something at p<0.05 by
chance. Require a minimum effect size and sample, and label confidence honestly.

### 4.6 Insights digest (§19.7)

Daily or weekly plain-language summary, dashboard card and email. **The primary way an SME
owner who never opens §8's analytics gets the value of the entire document.**

Reuse what exists: `NotificationLog` for dedup, tenant SMTP for email, FCM for push. No new
delivery infrastructure.

---

## 5. Peer benchmarking — the isolation problem

Every rule in §25 says tenant isolation is absolute and enforced server-side. §19.4 asks for
cross-tenant comparison. Both are correct; the resolution is architectural.

| Guard | Requirement |
|---|---|
| **Opt-in** | Explicit tenant consent, stored, revocable, default off |
| **Minimum cohort** | Never publish a percentile computed from fewer than **k tenants** (k ≥ 5). With three tenants in an industry, "ahead of 70%" identifies a competitor |
| **Aggregates only** | The peer table stores percentiles. Never raw values, never tenant ids |
| **Separate store** | Computed by a job that writes `PeerAggregate`. **No API path joins live tenant tables across tenants** |
| **Revocation** | Withdrawing consent removes that tenant from the next recomputation |

> Build the cohort-size floor as a database-level or service-level refusal, not a UI condition.
> The failure mode is a data-governance incident, and it will be discovered by a customer.

---

## 6. Services & jobs

`core/services/insights/` — one module per capability, plus a shared `Generator` base declaring
`dedup_key`, `expires_at`, evidence and confidence.

| Task | Cadence |
|---|---|
| `detect_anomalies` | Hourly |
| `generate_narratives` | Nightly, after rollups |
| `discover_patterns` | Weekly — expensive, and patterns do not change daily |
| `refresh_peer_aggregates` | Weekly, consented tenants only |
| `expire_insights` | Hourly — §19.6 freshness |
| `send_insights_digest` | Daily / weekly per tenant preference |

---

## 7. API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `insights/` | Active insights, filterable by type, capability, confidence |
| GET | `insights/{id}/evidence/` | §19.6 — evidence one click away |
| POST | `insights/{id}/dismiss/` | |
| POST | `insights/{id}/action/` | Links to an Action Center item |
| GET | `insights/digest/` | §19.7 |
| POST | `insights/what-if/` | §19.3 scenario |
| GET | `insights/peer-benchmark/` | §19.4 — **403 unless consented** |

Every response carries `insight_type`, `confidence`, `sample_size` and `generator_version`. §22
requires the frontend to render these distinctly from measured KPIs; the API must make that
possible without inference.

---

## 8. Settings

| Setting | Default | Effect |
|---|---|---|
| `INSIGHTS_ANOMALY_SIGMA` | `2.0` | Deviation that fires |
| `INSIGHTS_MIN_BASELINE_DAYS` | `21` | History before anomalies may fire |
| `INSIGHTS_MIN_SAMPLE_SIZE` | `30` | Below this, confidence is capped at LOW |
| `INSIGHTS_PEER_MIN_COHORT` | `5` | k-anonymity floor |
| `INSIGHTS_DEFAULT_TTL_HOURS` | `168` | Freshness window |
| `INSIGHTS_LLM_MODEL` | — | Narrative model |
| `INSIGHTS_ENABLE_PEER_BENCHMARK` | `False` | Platform kill switch |

---

## 9. Testing

| Area | Must prove |
|---|---|
| Confidence required | An insight without confidence or evidence cannot be saved |
| Dedup | The same finding does not regenerate nightly |
| Expiry | Stale insights leave the active set |
| Baseline guard | No anomaly fires below `INSIGHTS_MIN_BASELINE_DAYS` |
| Seasonality | A normal weekend dip does not fire |
| What-if | Deterministic — the same scenario returns the same delta |
| **Peer cohort floor** | A cohort below k **returns nothing**, asserted directly |
| **Peer isolation** | No code path reads another tenant's live rows — structural scan |
| Consent | Revocation removes the tenant from the next recomputation |
| Narrative grounding | Every generated narrative cites evidence resolving to real rollup rows |
| DASH-12 | Every insight discloses confidence and evidence via the API |

**Narrative quality cannot be unit-tested.** Build a labelled fixture set of periods with known
causes and score generated narratives against them by hand each iteration. Budget for that
review loop — it is the work, not overhead on the work.

---

## 10. Open decisions

| # | Decision | Default if unanswered |
|---|---|---|
| 1 | Which LLM, and does tenant data leave the platform? | Must be answered before §19.1 — a data-governance question, not a technical one |
| 2 | Is peer benchmarking opt-in or opt-out? | **Opt-in** — do not vary this |
| 3 | Digest cadence per tenant or platform-wide? | Per tenant, defaulting weekly |
| 4 | May insights trigger automatic actions? | **No** — §19.5 forbids silent changes |
| 5 | Is there a human review step before insights reach SME tenants? | Not at first; revisit after the first quality review |

Decision 1 is the one to raise early. §19.1 sends tenant business metrics to a model provider,
and some customers will have contractual views about that.

---

## Related

- [Implementation plan §4.7](../implementation-plan.md)
- [06 — analytics](06-analytics.md) — supplies rollups and sample sizes
- [08 — alerts](08-alerts.md) — the what-if input, and the evidence standard §19.7 references
- [Push notifications](../../flows/push-notifications.md) — digest delivery

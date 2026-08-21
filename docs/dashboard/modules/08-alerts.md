# Module 08 — `alerts`

**Risk detection and the CEO Action Center.** Where the dashboard stops reporting and starts
telling someone what to do.

| | |
|---|---|
| **BRD sections** | §5 (CEO Action Center), §21 (Alerts), §16 (recovery opportunity) |
| **Depends on** | [04 — revenue](04-revenue.md), [06 — analytics](06-analytics.md) |
| **Blocks** | §19.3 what-if (needs the action list) |
| **Status** | 🔴 New |
| **Estimate** | ~5–6 dev-weeks (Phase 5) |

---

## 1. The Action Center is stateful

§5 lists **Outcome** as a field: *recovered, progressed, lost, dismissed*.

That single row changes the architecture. An Action Center with outcomes is not a rendered
query over current risk — it is a **worklist with a lifecycle**. Items are created, owned,
worked, and resolved, and yesterday's item must still be findable tomorrow to know whether the
intervention worked.

Build it as records, not as a view. A rendered query cannot answer "did we recover the ₹18.4L
we flagged last Tuesday", which is the question that justifies the feature.

---

## 2. Data model

### 2.1 `AlertRule` — Tier 2

| Field | Type | Notes |
|---|---|---|
| `key` | `SlugField(48)` | `revenue_risk_stalled`, `cost_spike` |
| `label` | `CharField(120)` | |
| `alert_type` | `CharField(24)`, choices | The §21 taxonomy below |
| `metric_key` | `CharField(64)`, blank | Registry key, when threshold-based |
| `condition` | `JSONField` | Operator, threshold, window, comparison basis |
| `severity` | `CharField(8)`, choices | `CRITICAL` \| `HIGH` \| `MEDIUM` \| `LOW` |
| `default_owner_role` | `CharField(32)` | §5 ownership |
| `sla_hours` | `PositiveSmallIntegerField` | §5 SLA |
| `is_active` | `BooleanField` | |
| `notify_channels` | `ArrayField` | `PUSH` \| `EMAIL` \| `IN_APP` |

Seeded from §21, editable in Enterprise mode (§24: "Configurable anomaly rules").

```python
class AlertType(models.TextChoices):
    REVENUE_RISK           = "REVENUE_RISK"            # ₹12L high-intent pipeline stalled
    CTA_FAILURE            = "CTA_FAILURE"             # site-visit integration failed
    COST_SPIKE             = "COST_SPIKE"              # cost/sale +31%
    AGENT_DETERIORATION    = "AGENT_DETERIORATION"     # win rate -18%
    CAMPAIGN_DETERIORATION = "CAMPAIGN_DETERIORATION"  # revenue-moment rate -24%
    FORECAST_RISK          = "FORECAST_RISK"           # forecast -₹22L from slippage
    AVAILABILITY           = "AVAILABILITY"            # no valid appointment/inventory slot
    DATA_QUALITY           = "DATA_QUALITY"            # stale integration (§25)
```

`db_table = "core_alert_rule"`

### 2.2 `ActionItem` — Tier 2

One row per §5 field. The BRD wrote the schema; this transcribes it.

| Field | BRD §5 field | Type |
|---|---|---|
| `rule` | — | FK → `AlertRule`, PROTECT |
| `priority_score` | Priority | `Decimal(8,2)` — see §3 |
| `title` | Issue | `CharField(255)` — plain language |
| `description` | Issue | `TextField` |
| `impact_amount` | ₹ Impact | `Decimal(14,2)` |
| `currency` | | `CharField(3)` |
| `evidence` | Evidence | `JSONField` — lead / deal / conversation / CTA ids |
| `lead` | Evidence | FK → `Lead`, SET_NULL — **DASH-11 one-click** |
| `opportunity` | Evidence | FK, SET_NULL |
| `owner_user` | Owner | FK → user, SET_NULL |
| `owner_type` | Owner | `CharField(12)` — `AI` \| `SALESPERSON` \| `MANAGER` |
| `next_action` | Next action | `CharField(160)` |
| `next_action_url` | Next action | `CharField(255)` — the one-click target |
| `due_at` | SLA | `DateTimeField` |
| `breached_at` | SLA breach | `DateTimeField`, null |
| `outcome` | Outcome | `CharField(12)` — `OPEN` \| `RECOVERED` \| `PROGRESSED` \| `LOST` \| `DISMISSED` |
| `resolved_at` / `resolved_by` | | |
| `recovered_amount` | | `Decimal(14,2)`, null — what was *actually* saved |
| `dedup_key` | | `CharField(160)` |

**Unique:** `(tenant, dedup_key)` among `OPEN` items. Without it, an hourly evaluator recreates
the same ₹18.4L item every hour and the Action Center becomes unusable inside a day — the same
failure `NotificationLog`'s dedup key already prevents for notifications.

`db_table = "core_action_item"`

---

## 3. Prioritisation

§5: *"Rank by financial impact × urgency × recovery probability."*

```
priority_score = impact_amount
               × urgency_factor(due_at, breached_at)
               × recovery_probability(rule, lead_state)
```

- **`urgency_factor`** rises as the SLA approaches and jumps on breach.
- **`recovery_probability`** starts as a per-rule constant. Once there is history, derive it
  from that rule's actual recovery rate — the `outcome` field makes this measurable, which is
  the second reason the Action Center is stateful.

Keep it a transparent formula, not a model. A CEO asking "why is this first" deserves an
arithmetic answer, and §5's Evidence field implies one.

---

## 4. Services

`core/services/alerts.py`

| Function | Purpose |
|---|---|
| `evaluate_rules(tenant)` | Runs active rules, creates or refreshes items |
| `create_or_refresh(rule, *, dedup_key, ...)` | Idempotent upsert on the dedup key |
| `assign(item, user)` | Ownership |
| `resolve(item, outcome, *, recovered_amount)` | Closes with §5's outcome |
| `breach_sweep()` | Stamps `breached_at`, re-prioritises |
| `action_center(tenant, **filters)` | The §5 ranked list |
| `recovery_stats(tenant)` | Feeds §3's probability and §16's recovery widget |

**Rule evaluation reads rollups, not raw tables.** Module 06 already computes deviations with
correct numerator/denominator handling; re-deriving them here would drift from the dashboard
the alert refers to — and an alert that disagrees with the chart beside it destroys trust in
both.

---

## 5. API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `action-center/` | §5 ranked list — the primary endpoint |
| GET | `action-center/{id}/` | Full evidence |
| POST | `action-center/{id}/assign/` | |
| POST | `action-center/{id}/resolve/` | Outcome + recovered amount |
| POST | `action-center/{id}/dismiss/` | |
| GET | `action-center/stats/` | Recovery rate, SLA adherence, open ₹ |
| GET/PATCH | `alert-rules/` | Enterprise configuration |

Every item carries `lead_id` and `next_action_url` — §5's "one-click contextual action" and
DASH-11 are the same requirement seen from two directions.

---

## 6. Delivery — reuse, do not rebuild

§21 needs alerts delivered. Everything required already exists and is production-tested:

| Need | Existing |
|---|---|
| Once-only delivery | `NotificationLog` dedup ledger |
| Push | FCM path with retries, failure classification, device lifecycle |
| Email | Tenant SMTP with per-tenant sender resolution |
| Retry + permanent-failure visibility | `ReportedTask` |

Write a `NotificationLog` row per alert delivery with `channel=FCM` or `EMAIL`, keyed
`alert:{item_id}:{channel}`. That is the pattern
[Flow 5](../../flows/05-notifications-and-push.md) already documents.

> **Respect device-token staleness.** Push goes only to deliverable devices — active, not
> deleted, and re-registered within the staleness window. A CRITICAL alert pushed to a dead
> handset is not delivered, and §21 needs the email fallback for exactly that case.

---

## 7. Background jobs

| Task | Cadence | Purpose |
|---|---|---|
| `evaluate_alert_rules` | Hourly | Create/refresh items |
| `sweep_sla_breaches` | Every 15 min | Stamp breaches, re-prioritise, escalate |
| `expire_resolved_items` | Daily | Archive beyond retention |
| `deliver_pending_alerts` | Every 5 min | Push/email via `NotificationLog` |

---

## 8. Settings

| Setting | Default | Effect |
|---|---|---|
| `ALERTS_DEFAULT_SLA_HOURS` | `24` | When a rule sets none |
| `ALERTS_MAX_OPEN_PER_TENANT` | `50` | Keeps the Action Center a worklist, not a backlog |
| `ALERTS_MIN_IMPACT_AMOUNT` | `0` | Suppress trivial items |
| `ALERTS_ITEM_RETENTION_DAYS` | `180` | Resolved-item retention |
| `ALERTS_ESCALATE_AFTER_BREACH_HOURS` | `24` | Escalation to manager |

`ALERTS_MAX_OPEN_PER_TENANT` is a product decision disguised as a setting: an Action Center
with 400 open items is a report, and nobody acts on a report.

---

## 9. Testing

| Area | Must prove |
|---|---|
| Dedup | Re-evaluation refreshes rather than duplicating |
| Prioritisation | Ordering matches the formula at boundaries |
| SLA | Breach stamped once; escalation fires once |
| Outcome lifecycle | Every transition valid; resolved items leave the open list |
| Recovery stats | Correct against a fixture with known outcomes |
| Delivery dedup | One `NotificationLog` row per item per channel |
| Push fallback | A dead device falls back to email |
| Rollup consistency | Alert thresholds agree with the dashboard metric they cite |
| DASH-11 | Every item exposes `lead_id` |
| Isolation | Tenant A never sees tenant B's items |
| **Structural guard** | Every `AlertType` has a seeded rule — so a new type cannot ship silently inert |

---

## 10. Open decisions

| # | Decision | Default if unanswered |
|---|---|---|
| 1 | Who owns an item when no salesperson is assigned to the lead? | Tenant admin |
| 2 | Can AI own an item, or only be the subject of one? | Subject only in v1 |
| 3 | Does dismissing suppress that dedup key permanently or for a window? | Window (7 days) |
| 4 | Is `recovered_amount` entered by a human or inferred from revenue? | Inferred where an opportunity closes; overridable |
| 5 | Do SME tenants get configurable rules? | No — §24 puts rule configuration in Enterprise |

Decision 3 has teeth: permanent suppression means a recurring risk goes quiet forever after one
dismissal, and the second occurrence is the one that costs money.

---

## Related

- [Implementation plan §4.8](../implementation-plan.md)
- [04 — revenue](04-revenue.md) — risk rules and recovery amounts
- [06 — analytics](06-analytics.md) — threshold source
- [07 — insights](07-insights.md) — §19.3 what-if consumes this list
- [Flow 5 — Notifications & Push](../../flows/05-notifications-and-push.md) — the delivery pattern

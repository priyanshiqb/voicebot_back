# Executive Dashboard — Module Specifications

Per-module build specifications for [SalesBot Executive Dashboard BRD v3.1](implementation-plan.md).

Read [the implementation plan](implementation-plan.md) first — it establishes *why* these
modules exist, what already works, and the four decisions that gate the whole programme. These
documents answer *how to build each one*.

---

## Reading order

The numbering is dependency order, not importance. Building out of order means building on
facts that do not exist yet.

| # | Module | BRD sections | Depends on | Status |
|---|---|---|---|---|
| [01](modules/01-industry.md) | **industry** — tenant industry & journey config | §6, §15, §24 | — | 🔴 New |
| [02](modules/02-lead.md) | **lead** — lifecycle, transitions, timeline | §7, §8 | 01 | 🔴 New |
| [03](modules/03-cta.md) | **cta** — call-to-action tracking | §9 | 01, 02 | 🔴 New |
| [04](modules/04-revenue.md) | **revenue** — opportunity, ledger, attribution, forecast | §13, §16 | 02, 03 | 🔴 New |
| [05](modules/05-cost.md) | **cost** — unit economics | §14 | — (external dep) | 🔴 New |
| [06](modules/06-analytics.md) | **analytics** — rollups, metric registry, read APIs | §4, §8, §10–17, §20, §27 | 01–05 | 🔴 New |
| [07](modules/07-insights.md) | **insights** — the intelligence engine | §19 | 06 | 🔴 New |
| [08](modules/08-alerts.md) | **alerts** — risk rules & CEO Action Center | §5, §21 | 04, 06 | 🔴 New |
| [09](modules/09-existing-changes.md) | **Changes to existing modules** | various | — | 🟠 Extend |

**§18 Conversation Intelligence is not in this list** because it is not a new module — it is
extraction over data `AgentCallLog` already stores. Its changes live in
[09 — existing changes](modules/09-existing-changes.md), and it can be built from day one.

---

## What each specification contains

| Section | Answers |
|---|---|
| Purpose & BRD coverage | Which BRD requirements this module satisfies, exactly |
| What already exists | What to reuse instead of rebuilding |
| Data model | Concrete fields, types, constraints, indexes |
| Services | Where the business logic lives |
| API surface | Endpoints, with the drill-down contract where it applies |
| Background jobs | Beat tasks and their cadence |
| Settings | New configuration, with defaults |
| Migration & rollout | Order of operations against live data |
| Testing | What must be proven, including the structural guards |
| Open decisions | What a human must decide before or during the build |

---

## Conventions these specifications follow

Taken from the existing codebase, not invented. New code should be indistinguishable from what
is already here.

| Concern | Convention |
|---|---|
| Base model | `TenantBaseModel` (audited: `created_by` / `updated_by` / `deleted_by`) or `TenantBaseModelCore` (no user audit). See [the guide](../../tenant/common/models/guide.md) |
| Primary key | UUIDv4, inherited |
| Soft delete | `is_deleted` / `deleted_at`, inherited. `objects` excludes deleted; `all_objects` does not |
| Table naming | `core_<snake_case>` — e.g. `core_agent_call_log`, `core_device_tokens` |
| Enums | `models.TextChoices` nested in the model |
| Money | `DecimalField(max_digits=14, decimal_places=2)` — 14 digits because a property in INR and a car in IDR both have to fit |
| Currency | `CharField(max_length=3)` with a **callable** default reading settings, as `default_catalog_currency()` does |
| Layout | `core/models/ · serializers/ · views/ · services/ · urls/ · admin/`, one file per concern |
| Views | `TenantBaseViewSet` — gives filtering, ordering, pagination, `dropdown`, `bulk_delete`, tenant scoping |
| Schema | `@extend_schema` on every action; the generated document is regression-tested |
| Tasks | `core/tasks.py`, `@shared_task(base=ReportedTask)`, retries on anything that can fail transiently |
| Tests | Mirror the domain; assert behaviour, not implementation |

### Tier 1 vs Tier 2

The existing codebase marks each model. Follow it:

- **Tier 1 Core** — high-volume, machine-written, no user attribution needed (`AgentCallLog`,
  `DeviceToken`, `NotificationLog`). Use `TenantBaseModelCore`.
- **Tier 2** — user-authored, needs "who changed this" (`CatalogItem`, `Campaign`, `Contact`).
  Use `TenantBaseModel`.

Money and configuration are almost always Tier 2. Event streams are almost always Tier 1.

---

## The rule that applies to every module

> **BRD §25 is not decoration.** Every financial value must carry its definition, source and
> freshness; pipeline must never be displayed as realized revenue; every attributed value must
> carry its attribution version; and no prediction may be shown as a certainty.
>
> These are structural requirements. Each specification states how its module satisfies them —
> if a design cannot, it is the design that is wrong.

---

## Related

- [Implementation plan](implementation-plan.md) — coverage map, hard problems, phasing, effort
- [Command runbook](../runbook.md) — how to run migrations, tests and the stack
- [Base model guide](../../tenant/common/models/guide.md)

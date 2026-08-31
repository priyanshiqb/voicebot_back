# Admin Console — Plan

A custom operator console (React) for running the platform: tenants, analytics, manual
onboarding, support, catalogue. This document covers **what the backend must provide**,
since that is what lives in this repo, plus the screen inventory the frontend builds
against.

---

## 1. Scope and the one rule that shapes everything

The console is for **platform staff**, not tenants. That single fact decides the whole
architecture, and getting it wrong is the most expensive mistake available here.

> ⚠️ **Never reuse the tenant-scoped viewsets.** Everything under `/api/v1/core/`,
> `/api/v1/billing/` and `/api/v1/tenant/` resolves the tenant from
> `request.user.tenant_mapping`. An operator endpoint takes the tenant as a
> *parameter*. Mount an operator screen on a tenant-scoped viewset and it will silently
> scope to the operator's own tenant — returning plausible, wrong data with no error.

Everything the console needs is a **new `/api/v1/ops/` namespace**, built on the service
layer that already exists. Almost nothing here is new business logic: `wallet_service`,
`subscription_service`, `payment_application`, `entitlements` and `pricing` already do
the work. This is routing, aggregation and permissions.

---

## 2. What already exists, so nobody rebuilds it

| | Status |
| :--- | :--- |
| Service layer for every billing action | ✅ `grant`, `activate`, `renew`, `apply_plan_change`, `apply`, `summary` |
| Tenant creation without payment | ✅ `create_tenant_and_mapping` + `activate()` — the pieces exist, no endpoint does |
| Per-tenant usage with live counts | ✅ `entitlements.summary(tenant_id, sub, with_usage=True)` |
| Frozen audit trail on every charge | ✅ `Transaction.amount_breakdown`, `plan_snapshot`, `rate_snapshot` |
| Call data for analytics | ✅ `AgentCallLog` — duration, tier, outcome, mood, campaign, agent |
| Credit movements | ✅ `CreditLedger`, append-only, with the rating that produced each |
| Django admin | ✅ 66 models — **keep it** as the engineering escape hatch |
| **Operator API** | ✅ `/api/v1/ops/` — plans (module 1) and tenants (milestone 2) |
| **Analytics endpoints** | ❌ none |
| **Manual onboarding endpoint** | ❌ none |
| **Platform-staff roles** | ✅ `PlatformRole`, `console_permissions`, `IsPlatformStaff`, `HasConsolePermission` |
| **Operator audit log** | ✅ `OperatorAuditLog`, append-only |

**Keep the Django admin.** Not as the product, but as the thing engineering uses when
the console has a bug. Deleting it removes the only tool that can fix the tool.

---

## 3. Authentication and roles

`user_type` has `ADMIN`, `USER`, `ETL`, `VB_AGENT`.

**`ADMIN` is free, and now reserved for exactly this.** It was never assigned by any
code path — the default is `USER` and nothing overrides it — while two booking
notification paths filtered on it and therefore delivered to an empty audience for as
long as they existed. Those now filter on tenant membership, which is correct: **a
tenant's users are all one type**, and permissions inside a tenant come from the tenant
mapping rather than from this field.

That leaves `ADMIN` meaning what the console needs: **platform staff**. Deliberately not
a tenant role, so a tenant's own administrator can never reach `/ops/`.

### `PlatformRole` — two roles ✅ built

| Role | Can |
| :--- | :--- |
| `MANAGER` | **Everything.** Runs the panel; no per-action grants are consulted |
| `SUB_MANAGER` | **Reads, and nothing else** until an action is granted individually |

Two rather than a tier ladder, because two is what the work currently divides into — and
a permission matrix nobody can hold in their head is one that gets bypassed with a
superuser account. If a third level is genuinely needed later, it is a choice added to
an enum, not a redesign.

**A sub-manager starts closed.** Each action they may take is listed on
`console_permissions`, keyed on codes from `OperatorAuditLog.Action` — `credits.grant`,
`payment.replay` and so on. Using one vocabulary for both means **what somebody may do
and what is recorded when they do it cannot drift apart**, and a capability added next
year is unreachable until somebody decides who should hold it, rather than silently
appearing for everyone who already had a role.

### How it is gated

`IsPlatformStaff` at the router — one check for the whole namespace. `HasConsolePermission`
per view, reading `required_permission`. A view that names no permission is treated as a
**read**: forgetting the attribute is "readable by staff", never "open to the world",
because the namespace gate has already run.

A database constraint ties `platform_role` to `user_type = ADMIN`, so a half-configured
operator cannot exist — existing and being denied access for a reason nobody can find is
worse than failing at creation.

Platform staff have **no `tenant_mapping`**, which is exactly why the tenant-scoped
viewsets would break for them, and a useful safety property: an operator account
physically cannot be a tenant user.

---

## 4. Backend work — the `/api/v1/ops/` namespace

### Layout — package-based, one sub-package per module

Matching `onboard/views/billing/` and `onboard/serializers/onboarding/`: every layer is
a package whose `__init__.py` re-exports, one file per concern.

```
ops/
  __init__.py   apps.py
  urls/          __init__.py assembles the single /api/v1/ops/ router
  permissions/   re-exports IsPlatformStaff, HasConsolePermission
  serializers/   one sub-package per module -- plan/, tenant/, payment/, ...
  services/      one sub-package per module
  views/         one file per module
  tests/
```

A new console module adds a sub-package under `serializers/` and `services/` plus a file
under `views/` — rather than four shared modules that every module edits at once, which
is how merge conflicts and accidental cross-module coupling both start.

[Module 1 — Plan Configuration](./plan-configuration-module-plan.md) shows the shape
filled in.


### 4.1 Tenants

```
GET    /ops/tenants/                    list, search, filter by plan/status/enforcement
GET    /ops/tenants/{id}/               the 360 payload  (§4.2)
PATCH  /ops/tenants/{id}/               name, timezone, language
POST   /ops/tenants/{id}/enforcement/   {credits: bool, entitlements: bool}
POST   /ops/tenants/{id}/suspend/       is_active = False
```

The list needs columns that do not exist on `Tenant` — plan, status, balance, last call.
**Annotate, do not loop.** A naive implementation is 5 queries per row.

### 4.2 The 360 payload

One request, everything about one tenant:

```jsonc
{
  "tenant":       { "name", "subdomain", "created_at", "timezone", "is_active" },
  "enforcement":  { "credits_enforced", "entitlements_enforced" },
  "owner":        { "email", "name", "last_login" },
  "subscription": { "plan", "status", "start_date", "end_date", "days_remaining",
                    "bonus_days_applied", "scheduled_change" },
  "wallet":       { "balance", "allocated", "purchased", "is_frozen", "rate_snapshot" },
  "entitlements": [ /* summary(with_usage=True) — every resource, limit and live usage */ ],
  "payments":     [ /* last 10 Transactions with status and summary */ ],
  "ledger":       [ /* last 20 CreditLedger rows with their rating */ ],
  "usage":        { "calls_30d", "minutes_30d", "credits_burned_30d", "campaigns_active" },
  "health":       { "wallet_reconciles", "unrated_calls", "failed_payments" }
}
```

`health` is the part worth building carefully. It answers *"is anything wrong with this
tenant"* before support has to ask, and every check already exists as a query the audit
commands run.

### 4.3 Manual onboarding

The signup flow is `TempUser → OTP → plan → pay → tenant`. Sales-led deals, pilots and
comps need the same outcome **without a payment**.

```
POST /ops/onboarding/          create tenant + owner + subscription, no payment
POST /ops/onboarding/preview/  what this would create, before committing
```

Body: business name, owner email and name, plan, optional trial end date, optional
starting credits, reason.

All the pieces exist — `create_tenant_and_mapping` takes a `TempUser`-shaped object, and
`subscription_service.activate(tenant, plan)` funds the wallet with no transaction. The
work is a service function that assembles them and sends the owner a set-password
invitation rather than expecting a password.

> **Record why.** A tenant created this way has no payment behind it, and six months
> later somebody will ask why they are on Enterprise for free. Store the operator, the
> reason and any agreed end date on the subscription's `notes`.

### 4.4 Support actions

```
POST /ops/tenants/{id}/credits/grant/      {amount, reason}      -> wallet_service.grant
POST /ops/tenants/{id}/credits/deduct/     {amount, reason}
POST /ops/tenants/{id}/wallet/freeze/      {reason}
POST /ops/tenants/{id}/subscription/extend/{days, reason}
POST /ops/transactions/{order_id}/replay/  re-run payment_application.apply()
POST /ops/transactions/{order_id}/cancel/
POST /ops/webhooks/{event_id}/reprocess/
```

Every one is a thin wrapper over an existing service call. **Every one takes a reason,
and every reason is stored** — a credit grant with no explanation is indistinguishable
from a bug six weeks later.

`replay` is the highest-value action here: today a payment whose webhook failed needs a
shell. `payment_application.apply()` is idempotent, so the button is safe to press twice.

### 4.5 Catalogue

```
GET/POST/PATCH  /ops/plans/                  and nested entitlements + rates
POST            /ops/plans/{id}/preview/     what changing this would do
```

The 84 settings per plan are already documented for commercial. The console's job is to
present them in the three groups they naturally form — plan, capacity per resource, rate
card per tier — rather than as one 84-field form.

**Editing a plan only affects new and renewing tenants**, which is a property worth
showing on screen: *"changes apply to new subscriptions; 47 existing tenants keep their
current terms."*

### 4.6 Analytics

No analytics exist today. The data does. Build **two** endpoints, not twenty:

```
GET /ops/analytics/overview/    ?from=&to=
GET /ops/analytics/timeseries/  ?metric=&from=&to=&interval=day|week|month
```

| Metric | Source |
| :--- | :--- |
| MRR, ARR | `Subscription` × `plan_snapshot.net_base_price`, normalised by cycle |
| Active / lapsed / scheduled-to-downgrade | `Subscription.status` |
| New subscriptions, churn | `Subscription.start_date`, `_close()` history |
| Revenue collected | `Transaction` where `status = successful` |
| Failed & underpaid payments | `Transaction.status in (failed, underpaid)` |
| Calls, minutes, by tier | `AgentCallLog.duration_seconds`, `service_tier` |
| Credits granted / burned / forfeited | `CreditLedger.entry_type` |
| Tenants near a limit | `entitlements.summary` — **expensive, see below** |

> ⚠️ **"Tenants near a limit" is 7 queries per tenant.** `summary(with_usage=True)` is
> built for one tenant on a settings page. Running it across every tenant for a dashboard
> is the one place in this plan that needs a different implementation — a nightly
> materialised snapshot table, not a live loop.

---

## 5. Screen inventory for the React app

| # | Screen | Backend | Priority |
| :--- | :--- | :--- | :--- |
| 1 | Login + role-aware nav | JWT (exists) + `IsPlatformStaff` | P0 |
| 2 | Tenant list — search, filter, sort | `/ops/tenants/` | P0 |
| 3 | **Tenant 360** | `/ops/tenants/{id}/` | P0 |
| 4 | Support actions (modals off 360) | §4.4 | P0 |
| 5 | Payments — list, filter, replay | `/ops/transactions/` | P0 |
| 6 | Manual onboarding wizard | `/ops/onboarding/` | P1 |
| 7 | Plan catalogue editor | `/ops/plans/` | P1 |
| 8 | Analytics dashboard | `/ops/analytics/` | P1 |
| 9 | Webhook log + reprocess | `/ops/webhooks/` | P2 |
| 10 | Audit runner (reconcile, readiness) | `/ops/audits/` | P2 |
| 11 | Operator audit log — who did what | new model | P2 |

**Screens 2–5 are the console.** Everything else is a nice-to-have that can follow real
usage, which is a better guide than any inventory written in advance.

---

## 6. What has to be built that does not exist as a service

Most of this plan is routing over existing code. These are the genuinely new pieces:

| New | Why | Size |
| :--- | :--- | :--- |
| ~~`IsPlatformStaff` + `PlatformRole`~~ | ✅ built | — |
| ~~`OperatorAuditLog`~~ | ✅ built | — |
| `ops` serializers + router | New namespace | medium |
| 360 aggregation | Assembling 8 sources without N+1 | medium |
| Manual onboarding service | Assembles existing pieces + invitation email | medium |
| **`OperatorAuditLog` model** | Nothing records who granted credits or why | small |
| Analytics aggregation | All new, but plain ORM | medium |
| Usage snapshot table | The "near a limit" query cannot run live | medium |

---

## 7. Order of work

| | Milestone | Unblocks |
| :--- | :--- | :--- |
| ~~1~~ | ✅ `IsPlatformStaff`, `PlatformRole`, `OperatorAuditLog`, and the `/ops/` router | Everything. The audit log was built **first**, not last — retrofitting who-did-what is impossible |
| ~~2~~ | ✅ [Tenant list + 360 read endpoint](./admin-panel/milestone-2-tenant-360.md) — plus `PATCH` and enforcement. **`suspend` deferred:** `Tenant.is_active` is enforced nowhere, so the button would change nothing | Support can see. Read-only is already most of the value |
| 3 | Support actions | Support can act without an engineer |
| 4 | Payments list + replay | Stuck payments stop needing a shell |
| 5 | Manual onboarding | Sales-led deals stop needing a developer |
| ~~6~~ | ✅ Plan catalogue — [built first](./plan-configuration-module-plan.md), out of order: nothing can be sold until plans exist | Commercial self-serves |
| 7 | Analytics overview + timeseries | "How are we doing" has an answer |
| 8 | Usage snapshot + near-limit | Proactive support |

**Milestones 1–4 are the console that matters.** They are also, deliberately, the ones
that reuse the most existing code.

---

## 8. Things worth deciding now, because they are expensive later

**Impersonation.** *"Log in as this tenant"* is the single most requested admin feature
and the single largest security surface. If it is wanted, design it now: separate token
type, short expiry, banner in the tenant UI, every action logged as impersonated. If it
is not wanted, the 360 view has to be good enough — which is an argument for building it
well.

**Whether operators can edit a live subscription's frozen snapshot.** They should not.
The freeze is the guarantee that nobody's terms change under them. Changing terms means a
plan change, which has its own path and its own audit trail. But somebody will ask, so
decide before they do.

**Refunds.** No path in this system issues one, by design. If the console is expected to
refund, that is a genuine new capability touching Razorpay, the ledger and the tax
position — not a screen.

**Multi-currency.** The console will make it obvious that everything is INR. If a second
currency is coming, it changes analytics aggregation everywhere, and it is cheaper to
know now than to rebuild the dashboard.

---

## 9. What this plan does not build

| Not building | Why |
| :--- | :--- |
| Replacing Django admin | Keep it as engineering's escape hatch. When the console has a bug, it is the tool that fixes the tool |
| Editing frozen snapshots | The freeze is the guarantee — see §8 |
| Bulk price changes | Editing a plan already only affects new and renewing tenants. A bulk tool is mostly a way to make a mistake at scale |
| Tenant-facing analytics | Different audience, different aggregation, different permissions. A separate project |

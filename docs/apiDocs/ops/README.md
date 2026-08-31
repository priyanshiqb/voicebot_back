# Admin Console API

Everything platform staff can reach. **Base path:** `/api/v1/ops/`

This namespace is for people who run the platform, not for tenants. That one fact
decides its whole shape, and getting it wrong is the most expensive mistake available
here.

---

## Who can reach it

| | |
|---|---|
| A tenant user | ❌ — including a tenant's own administrator |
| A superuser without a console role | ❌ — `is_superuser` grants Django admin, not this |
| `MANAGER` | ✅ everything |
| `SUB_MANAGER` | ✅ reads; each write action granted individually |

Two fields on `CustomUser` carry it: `platform_role` is the gate, `console_permissions`
is the list of actions a sub-manager has been given. A database constraint requires
`user_type = ADMIN` alongside a role, so a **half-configured operator cannot exist** —
existing and being denied access for a reason nobody can find is worse than failing at
creation.

> **`is_staff` and `is_superuser` are deliberately not enough.** Those grant the Django
> admin, which is engineering's escape hatch. A support agent should hold a
> `platform_role` and neither of the others; an engineer with a superuser account
> should not silently gain the console.

### Reads are not gated per action

A view that names no `required_permission` is treated as a **read**, reachable by any
platform staff. That is deliberate: the namespace gate has already run, so a route
added without naming its permission is *readable by staff*, never *open to the world*.
A console where support cannot look at a plan is not a console.

### Writes name their permission before dispatch

`required_permission` is resolved from the action, **not set inside the handler**.
`has_permission` runs during `initial()`, before the handler is ever called — assigning
it in the handler would check `None`, let any platform staff write, and set the
attribute far too late to matter.

---

## Every write is recorded

`OperatorAuditLog` is append-only, and every write endpoint under `/ops/` leaves a row
carrying **who, what, to whom, and why**.

**A `reason` is required on every write.** Endpoints refuse with `400 REASON_REQUIRED`
without one — a catalogue change or a credit grant with no explanation is
indistinguishable from a bug six weeks later.

**Failures are recorded too.** An operator repeatedly failing to activate a plan is a
signal, and it disappears entirely if only successes are kept.

**The audit write never breaks the action it records.** It is best-effort and loud: a
logging failure must not roll back a change the customer is already expecting, because
that trades a missing row for a missing effect.

Permissions and the log share one vocabulary — the codes in `OperatorAuditLog.Action`
(`plan.change`, `credits.grant`, …). **What somebody may do and what is recorded when
they do it cannot drift apart.**

---

## Modules

| Module | Path | Status |
|---|---|---|
| [Plan configuration](./plans.md) | `/ops/plans/` | ✅ built |
| [Tenants & the 360 view](./tenants.md) | `/ops/tenants/` | ✅ built |
| Payments | `/ops/transactions/` | planned |
| Manual onboarding | `/ops/onboarding/` | planned |
| Operators | `/ops/operators/` | planned |
| Audit log | `/ops/audit/` | planned |
| Analytics | `/ops/analytics/` | planned |

See [Admin Console — Plan](../../implementation-plan-scratch/admin-console-plan.md) for
the full module inventory and what each one needs.

---

## Response envelope

The same as the rest of the project:

```json
{ "status": true, "message": "…", "data": { … }, "errors": null }
```

Errors carry a machine-readable code in `errors` and a sentence in `message`:

```json
{ "status": false, "message": "A reason is required for any change to the catalogue.",
  "data": null, "errors": ["REASON_REQUIRED"] }
```

---

## One rule for anyone extending this

> ⚠️ **Never build an ops endpoint on the tenant-scoped viewsets.** Everything under
> `/api/v1/core/`, `/api/v1/billing/` and `/api/v1/tenant/` resolves the tenant from
> `request.user.tenant_mapping`. **A platform operator has none.** Mount an operator
> screen on one and it will silently scope to the operator's own tenant — returning
> plausible, wrong data with no error at all.
>
> Operator endpoints take the tenant as a *parameter*. That is why `ops` is its own
> app: the boundary is auditable by inspection rather than by discipline.

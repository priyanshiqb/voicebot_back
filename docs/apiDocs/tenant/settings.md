# Tenant Settings API Documentation

Per-tenant configuration — the knobs a tenant sets for how the product behaves for them, as opposed
to their identity or their credentials.

Today it holds exactly one setting, and it is a consequential one: **which SMTP configuration carries
booking email**.

- **Path**: `/api/v1/tenant/settings/`
- **Auth**: JWT. Machine (`VB_AGENT`) tokens are refused.
- **Shape**: a **singleton** — one row per tenant, so no id in the path and no list. The row is
  created on first read; you never have to `POST` one into existence.

> ## Booking email does not send unless this is set
>
> `booking_email_configuration` unset means **no booking email at all** — no confirmations, no
> reminders, no cancellations, no reschedule requests. Bookings are still created. There is **no
> fallback to the platform sender**.
>
> A confirmation arriving from an address the customer has never heard of hurts the tenant's brand
> more than a missing one does, and lending the platform's sending reputation to a tenant's customer
> mail is what tenant SMTP exists to stop.

---

## 1. Read settings

`GET /api/v1/tenant/settings/`

No parameters. Creates the row if this tenant has never had one — reading must never fail because
nobody has written yet, since the same code path decides whether a booking confirmation can be sent.

**200 OK**
```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "id": "c3d4e5f6-…",
    "booking_email_configuration": "b1c2d3e4-…",
    "booking_email": {
      "will_send": true,
      "reason": null,
      "configuration_id": "b1c2d3e4-…",
      "from_email": "bookings@acme.com"
    },
    "created_at": "2026-08-06T10:58:03Z",
    "updated_at": "2026-08-07T09:12:44Z"
  },
  "errors": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `booking_email_configuration` | UUID \| null | The assigned configuration's id. **A populated value does not mean mail is sending** — see below. |
| `booking_email` | object | The computed answer to *"is booking email going out right now?"* |

### `booking_email` — read this, not the id

The pointer is set once and read for months. In between, the configuration behind it can be
disconnected, or reset to `UNVERIFIED` by a credential edit. This block re-checks all of it and
returns the live answer.

| Field | Type | Meaning |
|---|---|---|
| `will_send` | boolean | **The field to drive your UI from.** True only when the assigned configuration exists, is connected, and is `VERIFIED`. |
| `reason` | string \| null | Why not, in words a tenant can act on. `null` when `will_send` is true. |
| `configuration_id` | UUID \| null | What is assigned, even when it is not working. |
| `from_email` | string \| null | The address customers will see. `null` when nothing will send. |

| `reason` when `will_send` is false | What happened |
|---|---|
| *No email configuration is assigned…* | Nothing has been set. The starting state for every tenant. |
| *The assigned configuration is disconnected.* | Someone took it out of service. |
| *The assigned configuration is UNVERIFIED — it has not passed a test send since it was last changed.* | A credential or host edit reset it. Re-test it. |
| *The assigned configuration no longer exists.* | It was deleted through a path that bypassed the guard. |

| Status | When | `message` |
|---|---|---|
| **200** | Always, for a user with a tenant. | `Retrieved successfully` |
| **400** | A superuser with no tenant mapping — there is no "all tenants" answer to give. | `No tenant context. Superusers must act within a tenant.` |
| **401** | No valid JWT. | `Authentication credentials were not provided.` |
| **403** | The user has no active tenant mapping. | `Your user account is not associated with any active tenant.` |

---

## 2. Update settings

`PATCH /api/v1/tenant/settings/`

| Field | Type | Required | Description | Validation |
|---|---|---|---|---|
| `booking_email_configuration` | UUID \| null | | The configuration to carry booking email. `null` stops booking email entirely. | Must be one of **this tenant's** configurations, and must be **connected** and **`VERIFIED`**. |

**Request**
```json
{ "booking_email_configuration": "b1c2d3e4-…" }
```

Returns the full settings object, including a recomputed `booking_email` block — check
`will_send` rather than assuming the write took effect.

### Why assignment is fussier than it looks

> **Only a verified, connected configuration can be assigned.** Assigning one that cannot send would
> leave booking email silently off, which looks identical to having configured nothing — the exact
> confusion this endpoint exists to remove. Test it first.

> **Only your own configurations can be assigned.** The pointer arrives as an id; unscoped, a tenant
> could route their customer mail through a stranger's credentials by naming a UUID. Another tenant's
> id is a validation error, not a 403 — it is simply not in the choices.

> **Clearing is always allowed.** `null` is the escape hatch: a tenant with a leaked credential can
> stop sending immediately, accepting that booking email stops with it. It is also the prerequisite
> for [disconnecting or deleting](./email_configuration.md#the-assigned-booking-sender-is-locked) the
> assigned configuration, which is otherwise blocked.

| Status | When | `message` / `errors` |
|---|---|---|
| **200** | Updated. | `Updated successfully` |
| **400** | The configuration has not passed a test send. | `That configuration has not passed a test send yet. Test it first, so booking email is not assigned to a sender that cannot deliver.` |
| **400** | The configuration is disconnected. | `That configuration is disconnected. Reconnect it first.` |
| **400** | The id is not one of this tenant's configurations, or is not a valid UUID. | `Invalid pk "…" - object does not exist.` |
| **401** | No valid JWT. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping. | `Your user account is not associated with any active tenant.` |

Every error is wrapped by the global exception handler — `{status, message, data, errors, request_id}`.

> **There is no `PUT`, `POST`, or `DELETE`.** The row is a singleton that always exists; settings are
> cleared by setting fields to `null`, not by removing the row.

---

## What happens when nothing is assigned

Bookings still work. Only the mail stops.

| Behaviour | Effect |
|---|---|
| Booking creation, confirmation, cancellation | All succeed normally |
| The four booking emails | **Not sent** |
| `NotificationLog` | A row per attempt, `status: FAILED`, `error` explaining there is no sender |
| Calendar sync, push notifications | Unaffected — they do not go through SMTP |
| Platform email (signup OTP, password reset) | Unaffected — always platform credentials |

> **The `FAILED` rows are a recovery path, not just a record.** A `FAILED` notification is
> re-enqueueable, so configuring a sender and re-triggering the notification sends the *same* logical
> message rather than a duplicate. That is why nothing is skipped silently.

---

## Setting up booking email

1. Create and verify a configuration — [Email Configuration API](./email_configuration.md).
2. `PATCH /api/v1/tenant/settings/` with its id.
3. Check `booking_email.will_send` is `true`.

Step 3 is not ceremony. Steps 1 and 2 can both succeed while a stale state elsewhere keeps mail off,
and this is the single field that tells you the truth.

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **Only booking email is configurable.** Campaign and platform mail are not covered. | A tenant cannot separate transactional from bulk sending reputation yet. |
| 2 | **No notification when the assigned sender breaks.** | A configuration that goes `FAILED` mid-flight stops booking mail, and the tenant finds out from a customer unless something is watching `booking_email.will_send`. |
| 3 | **No audit trail of assignment changes** beyond `updated_at` and a log line. | "Who turned our booking email off?" is answerable from application logs, not from the API. |
| 4 | **Any tenant user can change it.** There is no role check. | Same gap as the [users API](../user/users.md) — visibility equals edit rights. |

## Related

- [Email Configuration API](./email_configuration.md) — creating and verifying the configurations this
  assigns
- [Booking API](../core/booking.md) — the notifications this setting gates
- [Tenant SMTP integration plan](../../tenant-smtp-integration-plan.md) — design rationale

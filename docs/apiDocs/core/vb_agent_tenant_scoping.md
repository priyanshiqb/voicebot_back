# Tenant Scoping for VB_AGENT Callers (`X-Tenant-ID`)

Applies to every endpoint a `VB_AGENT` token can reach. Human (JWT) callers are unaffected.

---

## Why this exists

`VB_AGENT` is a global role. Its users have no `TenantUserMapping`, so `TenantBaseViewSet`
cannot resolve a tenant for them, and the machine-facing endpoints have historically read
**every tenant's** data.

`X-Tenant-ID` is **mandatory on every endpoint a VB_AGENT token can reach**. It declares which
tenant the caller is working on, and every VB_AGENT code path scopes to it. Omitting it is a
`400` — never a global read.

It is also the **sole source of `tenant` on anything a machine caller creates**. That is not a
scoping detail. `tenant` is what every human-facing queryset filters on, so a record written
with the wrong tenant is not merely mis-scoped — it is invisible to the people who own it. A
booking the voicebot made would never appear in the customer's list, and nothing would look
broken until someone asked where their appointment went.

## ⚠️ This is a filter, not an authorization boundary

`AgentToken` carries **no tenant**, so nothing stops a token holder from putting any tenant id
in this header.

| It does | It does not |
|---|---|
| Stop a misconfigured runner reading or writing the wrong tenant | Contain a leaked or stolen token |
| Make the intended scope explicit and auditable per request | Restrict which tenants a token may name |
| Stop the `agent` field alone from choosing a write's tenant | Replace treating runner tokens as high-value secrets |

Making it a real boundary requires the allowed tenants to live on `AgentToken`. The header then
becomes a selector among them, and `resolve_agent_tenant` in
`tenant/common/views/agent_tenant_scope.py` is the only function that changes.

Until then: store runner tokens in a secret manager, rotate them, and never expose one to a
browser.

---

## Usage

```http
GET /api/v1/core/agents/ HTTP/1.1
Authorization: Bearer vb_a1b2c3_s3cr3tstring
X-Tenant-ID: 3f7c1e28-9b40-4a51-8e2d-5c6a7b8d9e01
```

- **Required on every request.** There is no unscoped mode.
- A header rather than a `?tenant=` query parameter: several of these viewsets declare
  `filterset_fields`, where a query parameter would collide with django-filter, and a header
  applies uniformly to `GET` and `POST` without being threaded through pagination links.

### Validation

| Value | Result |
|---|---|
| Valid UUID of an active tenant | Scoped to that tenant, `200` |
| Absent, empty, or whitespace | `400` — `{"X-Tenant-ID": ["This header is required for agent tokens."]}` |
| Not a UUID | `400` — `{"X-Tenant-ID": ["Must be a valid tenant UUID."]}` |
| UUID with no matching tenant | `400` — `{"X-Tenant-ID": ["No active tenant matches this id."]}` |
| UUID of a soft-deleted tenant | `400` — same as above |
| Sent by a JWT (human) user | Silently ignored; scope comes from the tenant mapping |

An unknown tenant is a `400`, not an empty `200`, so a typo cannot be mistaken for "this tenant
has no records". Whitespace counts as absent, so an unset environment variable produces a clear
error rather than scoping to nothing.

---

## The access matrix

Machine callers are **closed by default**. `VBAgentActionPermission` denies any viewset that
does not declare `vb_agent_allowed_actions`, so a new endpoint is unreachable by tokens until
someone opts it in. Anything not listed below returns `403`.

`X-Tenant-ID` is required on **every** row below.

| Endpoint | Actions |
|---|---|
| `/core/agents/{id}/` | `retrieve` only |
| `/core/agent-call-logs/` | full — `list`, `retrieve`, `dropdown`, `create`, `request-upload-url`, `confirm-upload` |
| `/core/campaign-runner/{id}/` | `context`, `members/update` — no delete, and none should be added |
| `/core/campaigns/{id}/` | `retrieve` only |
| `/core/bookings/` | full — `list`, `retrieve`, `create`, `hold`, `confirm`, `cancel`, `complete`, `no-show`, `DELETE` |
| `/core/availability/available-slots/` | full |
| `/core/event-templates/` | `list`, `retrieve`, `dropdown` — read-only |
| `/core/event-skills/` | `list`, `retrieve`, `dropdown` — read-only |

Explicitly **denied**: `availability-profiles`, `availability-exceptions`,
`calendar-integrations`, `contacts`, `contact-imports`, `knowledge-base`, `agent-templates`,
`probing-questions`, `custom-voices`, `device-tokens`, `campaigns` write actions
(`launch`/`pause`/`resume`/`cancel`/`members`/`stats`), `agents` `list`/`dropdown`/write, all
import/export, `/auth/users/`, `/auth/sessions/`.

Working hours, time-off, event-type definitions and calendar credentials are human
configuration. A time-off window in particular fires the reschedule workflow and can mass-move
existing bookings, which is not something a call should be able to trigger.

### Reads

Every read is filtered to the named tenant. A record outside it returns `404`, not `403` — it is
simply not in the queryset, the same response as an id that does not exist.

### Writes — where `tenant` comes from

Everything a machine caller creates takes `tenant` from the header:

| Created by | Model(s) | Source of `tenant` |
|---|---|---|
| `POST /core/agent-call-logs/` | `AgentCallLog` | header |
| `POST /core/agent-call-logs/request-upload-url/` | `AgentCallRecording` | header (also the S3 object-key prefix) |
| `POST /core/agent-call-logs/confirm-upload/` | `AgentCallRecording` | header |
| `POST /core/bookings/` | `Booking`, `BookingParticipant` | header |
| `POST /core/bookings/hold/` | `Booking`, `BookingParticipant` | header |

The `agent` and `event_template` fields in these payloads are **corroborating, not deciding**.
They must belong to the named tenant — `403` for `agent`, `400` for `event_template` — but they
never set it. Deriving the tenant from a field the caller supplied would be the caller naming
its own scope, which defeats the point of the header.

The practical consequence: a booking or call log the voicebot creates appears immediately in the
customer's own list, because it carries the same `tenant` their queryset filters on.

### Writes — the header constrains the `agent` field

These endpoints derive the record's tenant from the `agent` id in the request body. With a header
present, that agent must belong to the named tenant:

| Endpoint | Mismatch result |
|---|---|
| `POST /api/v1/core/agent-call-logs/` | `403` |
| `POST /api/v1/core/agent-call-logs/request-upload-url/` | `403` |
| `POST /api/v1/core/agent-call-logs/confirm-upload/` | `403` |

```json
{
  "status": false,
  "message": "The specified agent does not belong to the requested tenant.",
  "data": null,
  "errors": null
}
```

Without the header these behave exactly as before: the `agent` field alone determines the tenant,
which means any valid token can land a call log or recording in any tenant by naming that
tenant's agent. **Sending the header closes that path** — it is the main reason to adopt it.

Reported as `403` rather than `404`. The object-key checks in `confirm-upload` use `404` to avoid
confirming what exists in another tenant, but that reasoning does not carry here: the header
grants nothing and a caller can simply omit it, so hiding the mismatch buys no secrecy and costs
the client an actionable error.

### Bulk import / export — always denied

These are **not** header-dependent. Bulk CSV import and export require an active tenant mapping
(or superuser), which a machine caller does not have:

| Endpoint | VB_AGENT result |
|---|---|
| `POST /core/agents/import/` | `403` |
| `POST /core/agent-templates/import/` | `403` |
| `POST /core/probing-questions/import/` | `403` |
| `POST /core/contacts/import/` | `405` — staged flow only, at `/core/contact-imports/` |
| `GET /core/{agents,agent-templates,probing-questions,contacts}/export/` | `403` |

This closes a real hole. `import_data` never calls the viewset's `get_queryset`, so the tenant
guard that protects every other write on these viewsets does not run on the import path. The only
remaining check was `TenantModelResource.get_queryset`, which skipped its tenant filter whenever
`tenant` was unset — reading an absent scope as "no scope needed". A VB_AGENT token hit exactly
that branch, and could overwrite **any** tenant's agents, templates, or probing questions by
putting their ids in a CSV. It returned `200 {"updated": 1}`.

Both layers now fail closed: the mixin refuses the action, and the resource matches nothing on an
unset tenant. Superusers keep their global carve-out via an explicit `allow_cross_tenant` opt-in.

---

---

## ⚠️ Breaking changes for existing runners

**1. `X-Tenant-ID` is now required on every call.** Previously optional on agents, call logs and
the campaign runner. Any request without it returns `400`. Every runner must send it.

**2. `GET /core/agents/` (list) and `GET /core/agents/dropdown/` now return `403`.** Only
`GET /core/agents/{id}/` remains. A runner that enumerated agents must be given the agent id it
is acting for instead.

### Client checklist

- Send `X-Tenant-ID: <tenant uuid>` on **every** request, reads included.
- Keep sending `agent` on call-log create and both upload steps — still required, and it must
  belong to the tenant you named.
- Stop calling `/core/agents/` and `/core/agents/dropdown/`.
- Treat `400` with an `X-Tenant-ID` key as a configuration error, not a retryable failure.

## Adding a new endpoint to the surface

1. Add `vb_agent_allowed_actions = frozenset({...})` to the viewset, using DRF action names — the
   method name for `@action` routes (`no_show`, not `no-show`).
2. If it is tenant-scoped, add `VBAgentTenantScopedMixin`, set `vb_agent_requires_tenant = True`,
   and route `get_queryset` through `agent_scoped_queryset()`.
3. If the viewset overrides `permission_classes` or `get_permissions`, restate
   `VBAgentActionPermission` — assigning either replaces what `BaseViewSet` sets and would
   silently reopen every action.

## Rollout

1. **Now** — header optional on the legacy endpoints, required on the new ones. Bulk import and
   export are closed outright.
2. **Next** — runners send it everywhere; cross-tenant writes become impossible for those callers.
3. **Later** — add tenants to `AgentToken` and enforce, turning the filter into a boundary.

## Reference

- Implementation: `tenant/common/views/agent_tenant_scope.py`
- Tests: `core/tests/views/test_agent_tenant_header.py`,
  `core/tests/views/test_vb_agent_access_matrix.py`
- Call sites: `core/views/{agent,agent_call_log,campaign_runner,campaign,booking,availability,event_template,user_event_skill}.py`

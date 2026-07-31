# Campaigns API

**Base path:** `/api/v1/core/campaigns/`
**Auth:** JWT — `Authorization: Bearer <access_token>`
**Scope:** every endpoint is filtered to the caller's tenant.

> **VB_AGENT access:** `retrieve` only (`GET /campaigns/{id}/`), and the token **must** send
> `X-Tenant-ID: <tenant uuid>` (**400** if omitted; **404** if the campaign is in another
> tenant). `list`, `launch`, `pause`, `resume`, `cancel`, `members`, `stats`,
> `preview-audience` and all writes return **403** — the runner reports outcomes through the
> [Campaign Runner API](campaign_runner.md), it does not drive campaign lifecycle.
> See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

For architecture, flow diagrams, and how the statuses fit together, see the
[Campaign Overview](../../campaigns/README.md). For the machine-facing side, see the
[Campaign Runner API](campaign_runner.md).

---

## Contents

- [Response envelope](#response-envelope)
- [Endpoint index](#endpoint-index)
- [Field reference](#field-reference)
- [Enum reference](#enum-reference)
- [`GET /` — List](#get--list)
- [`POST /` — Create](#post--create)
- [`GET /{id}/` — Retrieve](#get-id--retrieve)
- [`PATCH /{id}/` — Update](#patch-id--update)
- [`DELETE /{id}/` — Delete](#delete-id--delete)
- [`POST /bulk_delete/`](#post-bulk_delete)
- [`GET /dropdown/`](#get-dropdown)
- [`POST /{id}/launch/`](#post-idlaunch)
- [`POST /{id}/pause/`](#post-idpause)
- [`POST /{id}/resume/`](#post-idresume)
- [`POST /{id}/cancel/`](#post-idcancel)
- [`GET /{id}/members/`](#get-idmembers)
- [`GET /{id}/stats/`](#get-idstats)
- [`POST /preview-audience/`](#post-preview-audience)
- [Error reference](#error-reference)

---

## Response envelope

Every response uses the same wrapper.

**Success:**

```json
{ "status": true, "message": "success", "data": {}, "errors": null }
```

**Error:**

```json
{ "status": false, "message": "Human-readable reason", "data": null, "errors": null }
```

**Paginated** — adds `meta`:

```json
{
  "status": true,
  "message": "success",
  "data": [],
  "errors": null,
  "meta": {
    "count": 42,
    "next": "http://host/api/v1/core/campaigns/?page=2",
    "previous": null,
    "page": 1,
    "page_size": 10
  }
}
```

**DRF field-validation errors** bypass the wrapper and return the field map directly:

```json
{ "name": ["A campaign with this name already exists."] }
```

---

## Endpoint index

| Method | Path | Purpose | Success |
|---|---|---|---|
| `GET` | `/` | List campaigns | 200 |
| `POST` | `/` | Create | 201 |
| `GET` | `/{id}/` | Retrieve | 200 |
| `PUT` / `PATCH` | `/{id}/` | Update | 200 |
| `DELETE` | `/{id}/` | Soft delete | 200 |
| `POST` | `/bulk_delete/` | Soft delete many | 200 |
| `GET` | `/dropdown/` | Picker options | 200 |
| `POST` | `/{id}/launch/` | Launch or schedule | 202 |
| `POST` | `/{id}/pause/` | Hold | 200 |
| `POST` | `/{id}/resume/` | Un-hold | 200 |
| `POST` | `/{id}/cancel/` | Terminate | 200 |
| `GET` | `/{id}/members/` | Frozen audience | 200 |
| `GET` | `/{id}/stats/` | Counters | 200 |
| `POST` | `/preview-audience/` | Callable preview | 200 |

---

## Field reference

### Writable

| Field | Type | Required | Rules |
|---|---|---|---|
| `name` | string | **yes** | 3–100 chars; letters, digits, spaces, `-`, `_` only (`^[\w\s-]+$`); **unique per tenant, case-insensitive**, ignoring soft-deleted |
| `description` | string | no | `""` allowed. If non-blank, 3–500 chars |
| `agent` | uuid | **yes** | Must be an **active, non-deleted** bot **in your tenant** |
| `contacts` | uuid[] | no | Each a non-deleted contact **in your tenant**. Max `CAMPAIGN_MAX_SELECTED_CONTACTS` (5000). Replaces the whole selection |
| `audience_source` | enum | no | Only `SELECTED` accepted. `FILTER` / `UPLOAD` → 400 |
| `start_date` | string | no | `DD/MM/YYYY` or `YYYY-MM-DD`. **Write-only.** Requires `start_time` |
| `start_time` | string | no | `HH:MM`, `HH:MM:SS`, `HH:MM AM/PM`. **Write-only.** Requires `start_date` |
| `is_active` | bool | no | Defaults `true` |

### Read-only

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | |
| `tenant` | object | `{id, name, subdomain}` |
| `status` | enum | Server-derived; sending it is ignored |
| `contact_count` | int | Size of the current selection (detail only) |
| `start_local` | object \| null | `{date, time, timezone}` — the schedule as typed |
| `scheduled_start_at` | datetime \| null | UTC |
| `schedule_timezone` | string | IANA name, e.g. `Asia/Kolkata` |
| `started_at` | datetime \| null | When materialisation completed |
| `completed_at` | datetime \| null | |
| `handoff_id` | uuid \| null | Idempotency key, set at launch |
| `handoff_status` | enum | `PENDING` → `ACKED` when the runner pulls context |
| `handed_off_at` | datetime \| null | |
| `total_members` | int | |
| `pending_count` | int | Awaiting an outcome |
| `suppressed_count` | int | Excluded, never handed over |
| `completed_count` | int | |
| `failed_count` | int | |
| `error_message` | string | Populated on `FAILED` |
| `created_by` / `updated_by` / `deleted_by` | object \| null | `{id, email, first_name, last_name}` |
| `created_at` / `updated_at` / `deleted_at` | datetime | |
| `is_deleted` | bool | |

### Validation notes

- **Dates are day-first.** `01/02/2027` is 1 February 2027.
- **Start time is interpreted in your tenant's timezone**, then stored as UTC.
- The **combined** date+time must be in the future. Today's date with a time that has already
  passed is rejected.
- Date and time are all-or-nothing.

---

## Enum reference

**`status`** — `DRAFT`, `SCHEDULED`, `MATERIALIZING`, `HANDING_OFF`, `RUNNING`, `PAUSED`,
`COMPLETED`, `CANCELLED`, `FAILED`

**`audience_source`** — `SELECTED` (supported) · `FILTER`, `UPLOAD` (reserved, rejected)

**`handoff_status`** — `PENDING`, `SENT`, `ACKED`, `FAILED`

**member `status`** — `PENDING`, `SUPPRESSED`, `CANCELLED`, `IN_PROGRESS`, `COMPLETED`, `FAILED`

**`suppression_reason`** — `DO_NOT_CALL`, `CONSENT_DENIED`, `NO_PHONE`, `CONTACT_DELETED`

---

## `GET /` — List

Uses the lighter list serializer: **`contacts` is omitted** (an audience of thousands across ten
campaigns per page would be tens of thousands of rows for a field the list does not show). Use
the detail endpoint for the selection.

### Query parameters

| Param | Type | Values |
|---|---|---|
| `status` | enum | `DRAFT`, `SCHEDULED`, `MATERIALIZING`, `HANDING_OFF`, `RUNNING`, `PAUSED`, `COMPLETED`, `CANCELLED`, `FAILED` |
| `agent` | uuid | Agent id |
| `audience_source` | enum | `SELECTED`, `FILTER`, `UPLOAD` |
| `handoff_status` | enum | `PENDING`, `SENT`, `ACKED`, `FAILED` |
| `is_active` | bool | `true`, `false` |
| `search` | string | Matches `name`, `description`, `agent.bot_name` |
| `ordering` | string | `created_at`, `updated_at`, `name`, `scheduled_start_at`, `status` — prefix `-` to reverse |
| `page` | int | 1-based |
| `page_size` | int | Max 100 |

`GET /api/v1/core/campaigns/?status=RUNNING&ordering=-created_at&page_size=20`

### Response `200`

```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "id": "3f2a…",
      "created_at": "2027-02-10T11:02:31Z",
      "updated_at": "2027-02-15T04:00:03Z",
      "deleted_at": null,
      "is_deleted": false,
      "created_by": { "id": "…", "email": "alex@acme.com", "first_name": "Alex", "last_name": "P" },
      "updated_by": null,
      "deleted_by": null,
      "tenant": { "id": "…", "name": "Acme", "subdomain": "acme" },
      "name": "Q1 Lead Qualification",
      "description": "Warm leads from the January webinar",
      "agent": { "id": "…", "bot_name": "Sales Bot" },
      "status": "RUNNING",
      "audience_source": "SELECTED",
      "scheduled_start_at": "2027-02-15T04:00:00Z",
      "schedule_timezone": "Asia/Kolkata",
      "started_at": "2027-02-15T04:00:03Z",
      "completed_at": null,
      "handoff_status": "ACKED",
      "total_members": 500,
      "pending_count": 470,
      "suppressed_count": 30,
      "completed_count": 0,
      "failed_count": 0,
      "is_active": true
    }
  ],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

---

## `POST /` — Create

Creates in `DRAFT`. Only `name` and `agent` are required.

### Request

```json
{
  "name": "Q1 Lead Qualification",
  "description": "Warm leads from the January webinar",
  "agent": "b4e1…",
  "contacts": ["c1a2…", "d3f4…"],
  "start_date": "15/02/2027",
  "start_time": "09:30"
}
```

### Response `201`

Full detail object — same shape as [`GET /{id}/`](#get-id--retrieve).

### Errors

| Code | Body | When |
|---|---|---|
| 400 | `{"name": ["A campaign with this name already exists."]}` | Duplicate, including case-only differences |
| 400 | `{"name": ["Campaign Name can only contain letters, numbers, spaces, hyphens, and underscores."]}` | Bad characters |
| 400 | `{"name": ["Ensure this field has at least 3 characters."]}` | Too short |
| 400 | `{"agent": ["Invalid pk \"…\" - object does not exist."]}` | Unknown, inactive, deleted, **or another tenant's** bot |
| 400 | `{"contacts": ["Invalid pk \"…\" - object does not exist."]}` | Unknown or **another tenant's** contact |
| 400 | `{"contacts": ["A campaign cannot target more than 5000 contacts at once."]}` | Over the cap |
| 400 | `{"start_date": ["The campaign start must be in the future."]}` | Past instant |
| 400 | `{"start_time": ["Start date and start time must be supplied together."]}` | Only one supplied |
| 400 | `{"audience_source": ["'FILTER' audiences are not supported yet. Use 'SELECTED'."]}` | Reserved value |
| **400** | `{"status": false, "message": "Create an active bot before creating a campaign.", "data": null, "errors": {"code": "NO_ACTIVE_BOT"}}` | Tenant has no active bot |

> **Check `errors.code === "NO_ACTIVE_BOT"`,** not the message text — it is there so the UI can
> deep-link to bot creation.

---

## `GET /{id}/` — Retrieve

Full detail, including the contact selection. **Poll this** after launching.

### Response `200`

```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "id": "3f2a…",
    "created_at": "2027-02-10T11:02:31Z",
    "updated_at": "2027-02-15T04:00:03Z",
    "deleted_at": null,
    "is_deleted": false,
    "created_by": { "id": "…", "email": "alex@acme.com", "first_name": "Alex", "last_name": "P" },
    "updated_by": null,
    "deleted_by": null,
    "tenant": { "id": "…", "name": "Acme", "subdomain": "acme" },
    "name": "Q1 Lead Qualification",
    "description": "Warm leads from the January webinar",
    "agent": { "id": "…", "bot_name": "Sales Bot" },
    "status": "RUNNING",
    "audience_source": "SELECTED",
    "contacts": ["c1a2…", "d3f4…"],
    "contact_count": 500,
    "start_local": { "date": "2027-02-15", "time": "09:30", "timezone": "Asia/Kolkata" },
    "scheduled_start_at": "2027-02-15T04:00:00Z",
    "schedule_timezone": "Asia/Kolkata",
    "started_at": "2027-02-15T04:00:03Z",
    "completed_at": null,
    "handoff_status": "ACKED",
    "handoff_id": "9c7e…",
    "handed_off_at": "2027-02-15T04:00:11Z",
    "total_members": 500,
    "pending_count": 470,
    "suppressed_count": 30,
    "completed_count": 0,
    "failed_count": 0,
    "error_message": "",
    "is_active": true
  },
  "errors": null
}
```

`start_local` is `null` when unscheduled. `contacts` is a flat array of ids.

---

## `PATCH /{id}/` — Update

Which fields you may change depends on status. Anything else → **409**.

| Status | Editable |
|---|---|
| `DRAFT` | Everything |
| `SCHEDULED` | `name`, `description`, `start_date`, `start_time` |
| `RUNNING`, `PAUSED` | `name`, `description` |
| `MATERIALIZING`, `HANDING_OFF` | Nothing |
| `COMPLETED`, `CANCELLED`, `FAILED` | Nothing |

`contacts` **replaces** the whole selection — send the complete list, not a delta.

### Response `200`

```json
{ "status": true, "message": "Updated successfully", "data": { }, "errors": null }
```

### Errors

| Code | Body |
|---|---|
| 409 | `{"status": false, "message": "A campaign in RUNNING cannot change: agent, contacts.", "data": null, "errors": null}` |
| 400 | Same field errors as create |

---

## `DELETE /{id}/` — Delete

Soft delete. The name is freed for reuse immediately.

### Response `200`

```json
{ "status": true, "message": "Deleted successfully", "data": null, "errors": null }
```

### Errors

| Code | Body |
|---|---|
| 409 | `{"status": false, "message": "This campaign is still running. Cancel it before deleting.", "data": null, "errors": null}` |
| 404 | Not found, or another tenant's |

---

## `POST /bulk_delete/`

### Request

```json
{ "ids": ["3f2a…", "7b1c…"] }
```

### Response `200`

```json
{ "status": true, "message": "Bulk deleted successfully", "data": null, "errors": null }
```

### Errors

| Code | Message | When |
|---|---|---|
| 400 | `ids must be a non-empty list` | Missing or empty |
| 400 | `Some ids are invalid` | Any id unknown or another tenant's |
| 409 | `This campaign is still running. Cancel it before deleting.` | **Any** campaign in the batch is live — nothing is deleted |

---

## `GET /dropdown/`

Compact picker options.

| Param | Notes |
|---|---|
| `limit` | Max 1000; over that → 400 |
| plus every list filter | `status`, `search`, … |

### Response `200`

```json
{
  "status": true,
  "message": "success",
  "data": [{ "label": "Q1 Lead Qualification", "value": "3f2a…" }],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

---

## `POST /{id}/launch/`

No request body.

- Future `scheduled_start_at` → `SCHEDULED`; the beat sweep starts it within ~60s.
- No schedule → `MATERIALIZING`; the audience is frozen asynchronously. Poll `GET /{id}/`.

Generates `handoff_id`.

### Response `202`

```json
{
  "status": true,
  "message": "Campaign launched",
  "data": { "status": "MATERIALIZING", "handoff_id": "9c7e…" },
  "errors": null
}
```

Full detail object; abbreviated above.

### Errors

| Code | Message | When |
|---|---|---|
| 409 | `A campaign in RUNNING cannot be launched.` | Not `DRAFT` — including a double-click, where the second request loses |
| 409 | `The bot for this campaign is no longer active.` | Bot deleted or deactivated since create |
| 409 | `This campaign has no contacts that can be called. Everyone selected is either suppressed or has no usable phone number.` | Empty or fully suppressed audience — campaign **stays `DRAFT`** |
| 429 | Throttled | 30/min |

---

## `POST /{id}/pause/`

`RUNNING` → `PAUSED`. Records `paused_by`. No body.

### Response `200`

```json
{ "status": true, "message": "Campaign paused", "data": { "status": "PAUSED" }, "errors": null }
```

### Errors

| Code | Message |
|---|---|
| 409 | `A campaign in DRAFT cannot be paused.` |

---

## `POST /{id}/resume/`

`PAUSED` → `RUNNING`. No body.

### Response `200`

```json
{ "status": true, "message": "Campaign resumed", "data": { "status": "RUNNING" }, "errors": null }
```

### Errors

| Code | Message |
|---|---|
| 409 | `A campaign in RUNNING cannot be resumed.` |

---

## `POST /{id}/cancel/`

Terminal. Remaining `PENDING` and `IN_PROGRESS` members become `CANCELLED`; counters are
recomputed; `cancelled_by` recorded. No body.

**`SUPPRESSED` members are left untouched** — their reason is a compliance record, not a work
item.

### Response `200`

```json
{
  "status": true,
  "message": "Campaign cancelled",
  "data": { "status": "CANCELLED", "pending_count": 0, "suppressed_count": 30 },
  "errors": null
}
```

### Errors

| Code | Message |
|---|---|
| 409 | `A campaign in COMPLETED cannot be cancelled.` |
| 429 | Throttled — 30/min |

---

## `GET /{id}/members/`

The frozen audience. Empty until materialisation completes.

### Query parameters

| Param | Type | Values |
|---|---|---|
| `status` | enum | `PENDING`, `SUPPRESSED`, `CANCELLED`, `IN_PROGRESS`, `COMPLETED`, `FAILED` |
| `suppression_reason` | enum | `DO_NOT_CALL`, `CONSENT_DENIED`, `NO_PHONE`, `CONTACT_DELETED` |
| `search` | string | Substring match on `phone_e164` **only** |
| `page`, `page_size` | int | Max page size 100 |

### Response `200`

```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "id": "9f1c…",
      "contact_id": "c1a2…",
      "full_name": "Ada Lovelace",
      "phone_e164": "+14155552671",
      "status": "PENDING",
      "suppression_reason": "",
      "attempt_count": 0,
      "last_call_at": null,
      "outcome": "",
      "error_message": "",
      "created_at": "2027-02-15T04:00:03Z"
    },
    {
      "id": "2b7d…",
      "contact_id": "d3f4…",
      "full_name": "Raj Kumar",
      "phone_e164": "+919812345678",
      "status": "SUPPRESSED",
      "suppression_reason": "DO_NOT_CALL",
      "attempt_count": 0,
      "last_call_at": null,
      "outcome": "",
      "error_message": "",
      "created_at": "2027-02-15T04:00:03Z"
    }
  ],
  "errors": null,
  "meta": { "count": 500, "next": "…?page=2", "previous": null, "page": 1, "page_size": 10 }
}
```

Deliberately limited to name and number rather than the full contact, so a campaign export does
not widen the PII surface. Use `/contacts/{contact_id}/` for the rest.

---

## `GET /{id}/stats/`

Counters and the suppression breakdown, without paging members.

### Response `200`

```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "campaign_id": "3f2a…",
    "status": "RUNNING",
    "total_members": 500,
    "pending_count": 468,
    "suppressed_count": 30,
    "completed_count": 2,
    "failed_count": 0,
    "by_status": { "PENDING": 468, "SUPPRESSED": 30, "COMPLETED": 2 },
    "suppressed_by_reason": { "DO_NOT_CALL": 22, "CONSENT_DENIED": 6, "NO_PHONE": 2 }
  },
  "errors": null
}
```

`by_status` and `suppressed_by_reason` are computed live from members; the flat counters are
denormalised on the campaign. They should agree — a mismatch means counter drift.

Keys are **omitted when zero**, so treat a missing key as `0`.

---

## `POST /preview-audience/`

How many of these contacts can actually be called, and why the rest cannot. **Detail-free**, so
the wizard can call it before the campaign exists.

### Request

```json
{ "contact_ids": ["c1a2…", "d3f4…", "e5f6…"] }
```

### Response `200`

```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "requested": 500,
    "matched": 498,
    "callable": 470,
    "suppressed": 28,
    "suppressed_by_reason": { "DO_NOT_CALL": 20, "CONSENT_DENIED": 6, "NO_PHONE": 2 }
  },
  "errors": null
}
```

| Field | Meaning |
|---|---|
| `requested` | **Distinct** ids submitted — duplicates counted once |
| `matched` | Of those, how many exist in your tenant and are not deleted |
| `callable` | Of the matched, how many have no suppression reason |
| `suppressed` | `matched - callable` |
| `suppressed_by_reason` | Breakdown; keys omitted when zero |

A gap between `requested` and `matched` means ids that are unknown, deleted, **or another
tenant's** — those are silently ignored rather than erroring.

### Errors

| Code | Body |
|---|---|
| 400 | `{"contact_ids": ["A campaign cannot target more than 5000 contacts at once."]}` |
| 400 | `{"contact_ids": ["This field is required."]}` |
| 403 | `{"status": false, "message": "Your user account is not associated with any active tenant.", …}` |
| 429 | Throttled — 30/min |

---

## Error reference

| Code | Meaning | Shape |
|---|---|---|
| `400` | Validation failed | DRF field map, or envelope with `errors.code` |
| `401` | Missing or invalid token | `{"detail": "Authentication credentials were not provided."}` |
| `403` | No active tenant mapping | Envelope |
| `404` | Not found **or another tenant's** — never 403, which would confirm it exists | `{"detail": "Not found."}` |
| `409` | Wrong state for this action | Envelope |
| `429` | Throttled | `{"detail": "Request was throttled. Expected available in N seconds."}` |

### 400 vs 409

- **400** — the request is wrong. Fix the payload and resend.
- **409** — the request is fine, the campaign moved. Refresh and reconsider.

Throttled at **30/min** per user: `launch`, `cancel`, `preview-audience`.

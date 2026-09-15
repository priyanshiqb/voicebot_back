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
- [`POST /{id}/reports/`](#post-idreports)
- [`POST /preview-audience/`](#post-preview-audience)
- [Error reference](#error-reference)
- [Configuration](#configuration)
- [Related](#related)

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

**Field-validation errors use the same wrapper.** The per-field map is nested under `errors`, and
`message` is lifted from the first error so it is always a displayable string:

```json
{
  "status": false,
  "message": "A campaign with this name already exists.",
  "data": null,
  "errors": { "name": ["A campaign with this name already exists."] },
  "request_id": "5f2c9b14-…"
}
```

Two things follow, and both bite clients that assume otherwise:

- **Nothing on this API returns a bare field map.** A global exception handler wraps every error,
  so `response.name` is always undefined — read `response.errors.name`.
- **`message` is safe to display for any error**, including field errors. It is the first
  `detail`, else the first `non_field_errors` entry, else the first message of the first field.

Error responses also carry **`request_id`**, absent from success responses. Include it when
reporting a problem — it is what correlates your request with the server logs.

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
| `POST` | `/{id}/reports/` | Request an XLSX or PDF report | 202 |
| `POST` | `/preview-audience/` | Callable preview | 200 |

---

## Field reference

### Writable

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `name` * | string | What operators will call this campaign. Shown in lists, pickers, and reports. | 3–100 characters. Letters, digits, spaces, `-`, `_` only (`^[\w\s-]+$`) — no punctuation. **Unique per tenant, case-insensitively**: `Q1 Leads` and `q1 leads` collide. Soft-deleted campaigns do not reserve their name. |
| `agent` * | UUID | The bot that will place these calls. Its voice, prompt, and probing questions define what happens on the call. | Must be an **active, non-deleted** bot **in your tenant**. Another tenant's id reads as "does not exist". |
| `description` | string | Free-text note on who this audience is and why — for the humans reading the list later. | Optional; `""` is accepted. If non-blank, **3–500** characters. A 1–2 character description is rejected. |
| `contacts` | UUID array | The audience: who this campaign intends to call. Frozen into members at launch. | Optional. Each must be a non-deleted contact **in your tenant**. At most **5 000** (`CAMPAIGN_MAX_SELECTED_CONTACTS`). **Replaces the entire selection** — send the full list, never a delta. |
| `audience_source` | enum | How the audience was chosen. | Optional, default `SELECTED`. Only `SELECTED` is accepted today; `FILTER` and `UPLOAD` are reserved and return **400**. |
| `start_date` | string | Calendar date to start on, in your tenant's timezone. Omit both this and `start_time` to launch immediately instead. | Optional, **write-only**. `DD/MM/YYYY` or `YYYY-MM-DD`. Must be sent together with `start_time`. |
| `start_time` | string | Wall-clock time to start at, in your tenant's timezone. | Optional, **write-only**. `HH:MM`, `HH:MM:SS`, `HH:MM AM/PM`. Must be sent together with `start_date`. |
| `is_active` | boolean | Soft on/off switch, independent of both `status` and deletion. Does not stop a running campaign. | Optional, default `true`. |

### Read-only

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | |
| `tenant` | object | `{id, name, subdomain}` |
| `status` | enum | Server-derived; sending it is ignored |
| `contact_count` | int | Size of the **current** selection (detail only). Compare with `total_members`, which is the **frozen** count from launch — after materialisation they can legitimately differ, because editing `contacts` afterwards does not re-freeze the audience |
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

| Code | When | `message` |
|---|---|---|
| **200** | Zero or more campaigns. An empty `data` array is a normal result. | `success` |
| **400** | An unrecognised value for `status`, `audience_source`, or `handoff_status`; a non-boolean `is_active`; a malformed `agent` UUID. | filter error |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping, or a `VB_AGENT` token — listing is not available to machine callers. | as [Error reference](#error-reference) |
| **404** | `page` beyond the last page. | `Invalid page.` |

> **Unlike the members list, these filters *are* validated** — `?status=RUNNIG` is a `400` here but
> an empty `200` on `/{id}/members/`. The two use different filtering machinery.

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

### Responses

All field errors arrive as `{"status": false, "message": "<first error>", "errors": {…}}` — see
[Response envelope](#response-envelope). The `errors` column below shows only the nested map.

| Code | When | `message` | `errors` |
|---|---|---|---|
| **201** | Created, in `DRAFT`. Nothing is called until you launch it. | `Created successfully` | `null` |
| **400** | Tenant has no active bot. Checked **before** the payload, so this wins over field errors. | `Create an active bot before creating a campaign.` | `{"code": "NO_ACTIVE_BOT"}` |
| **400** | Duplicate name, including case-only differences. | `A campaign with this name already exists.` | `{"name": [...]}` |
| **400** | `name` contains disallowed characters. | `Campaign Name can only contain letters, numbers, spaces, hyphens, and underscores.` | `{"name": [...]}` |
| **400** | `name` shorter than 3 or longer than 100. | `Ensure this field has at least 3 characters.` | `{"name": [...]}` |
| **400** | `agent` unknown, inactive, deleted, **or another tenant's**. | `Invalid pk "…" - object does not exist.` | `{"agent": [...]}` |
| **400** | A contact id is unknown, deleted, **or another tenant's**. | `Invalid pk "…" - object does not exist.` | `{"contacts": [...]}` |
| **400** | More than 5 000 contacts. | `A campaign cannot target more than 5000 contacts at once.` | `{"contacts": [...]}` |
| **400** | The combined start instant is in the past. | `The campaign start must be in the future.` | `{"start_date": [...]}` |
| **400** | Only one of `start_date` / `start_time` supplied. | `Start date and start time must be supplied together.` | `{"start_time": [...]}` |
| **400** | `audience_source` is a reserved value. | `'FILTER' audiences are not supported yet. Use 'SELECTED'.` | `{"audience_source": [...]}` |
| **400** | `description` supplied but 1–2 characters. | `Ensure this field has at least 3 characters.` | `{"description": [...]}` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": …}` |
| **403** | No active tenant mapping, or a `VB_AGENT` token. | `Your user account is not associated with any active tenant.` / `This endpoint is not available to agent tokens.` | `{"detail": …}` |

> **Check `errors.code === "NO_ACTIVE_BOT"`,** not the message text — it is there so the UI can
> deep-link to bot creation.

> **The no-active-bot check is skipped for superusers**, who have no tenant mapping to check
> against. It also asks only whether *any* active bot exists — not whether the `agent` you named is
> one. A deleted or inactive `agent` id still fails separately, as a field error.

> **The duplicate-name check is a pre-flight, not a lock.** Two simultaneous creates with the same
> name can both pass validation; the database constraint then rejects the loser, which is caught
> and returned as the same clean `400` rather than a 500. No retry needed — the error is accurate.

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

| Code | When | `message` |
|---|---|---|
| **200** | Found. | `Retrieved successfully` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | No tenant mapping. For a `VB_AGENT` token, also when `X-Tenant-ID` is missing (**400**) — this is the one action agent tokens may call. | as [Error reference](#error-reference) |
| **404** | No such campaign, soft-deleted, or another tenant's. | `Not found.` |

> **This is the poll target.** After `launch` returns `202`, poll here until `status` leaves
> `MATERIALIZING`. Success looks like `RUNNING` with a non-zero `pending_count`; failure looks like
> `FAILED` with `error_message` explaining why.

> **`contacts` is expanded only here.** The list endpoint omits it entirely, and `contact_count`
> saves you counting the array yourself.

---

## `PATCH /{id}/` — Update

Change a campaign's configuration. **What you may change narrows as the campaign progresses** —
once the audience has been handed to the runner, editing it would mean the record no longer
describes what was actually called.

| Status | Editable |
|---|---|
| `DRAFT` | Everything |
| `SCHEDULED` | `name`, `description`, `start_date`, `start_time` |
| `RUNNING`, `PAUSED` | `name`, `description` |
| `MATERIALIZING`, `HANDING_OFF` | Nothing |
| `COMPLETED`, `CANCELLED`, `FAILED` | Nothing |

The check is on **which keys you send**, not on whether you changed anything. Re-sending `agent`
with its current value on a `RUNNING` campaign is still a 409 — omit the keys you are not
changing.

`contacts` **replaces** the whole selection — send the complete list, not a delta.

> **Use `PATCH`, not `PUT`.** `PUT` is routed, but it requires the full payload, and a full payload
> necessarily includes keys that a non-`DRAFT` campaign forbids — so `PUT` returns 409 for anything
> past `DRAFT`. There is no state in which `PUT` does something `PATCH` cannot.

> **An empty `PATCH {}` succeeds in any state**, including terminal ones, because no forbidden key
> was sent. It changes nothing and returns `200` with the current object. Harmless, but do not read
> that `200` as "this campaign is editable".

### Responses

| Code | When | `message` | `errors` |
|---|---|---|---|
| **200** | Updated. | `Updated successfully` | `null` |
| **400** | Any field error from [create](#post--create), including the new name colliding. | as create | `{"<field>": [...]}` |
| **409** | You sent a key this campaign's status forbids. Lists exactly which. | `A campaign in RUNNING cannot change: agent, contacts.` | `null` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": …}` |
| **403** | No tenant mapping, or a `VB_AGENT` token. | as create | `{"detail": …}` |
| **404** | No such campaign, soft-deleted, or another tenant's. | `Not found.` | `{"detail": …}` |

> **409 lists the offending fields**, sorted, so the message is directly displayable: *"A campaign
> in RUNNING cannot change: agent, contacts."*

> **Editing `contacts` after materialisation does not change who gets called.** The audience was
> frozen into members at launch; `contacts` is only the *selection*. This is why `contact_count`
> and `total_members` can diverge — and why the guard forbids it in every state where the runner is
> live.

---

## `DELETE /{id}/` — Delete

Soft delete. The name is freed for reuse immediately.

### Response `200`

```json
{ "status": true, "message": "Deleted successfully", "data": null, "errors": null }
```

| Code | When | `message` |
|---|---|---|
| **200** | Soft-deleted. The name is freed for reuse immediately. | `Deleted successfully` |
| **409** | The campaign is live (`MATERIALIZING`, `HANDING_OFF`, `RUNNING`, or `PAUSED`). Cancel it first. | `This campaign is still running. Cancel it before deleting.` |
| **409** | The campaign has **ever run** (`started_at` is set). Permanent — there is no later point at which it becomes deletable. | `This campaign has already run and forms part of your billing record, so it cannot be deleted. You can archive it instead.` |
| **401** / **403** | As elsewhere. | — |
| **404** | No such campaign, already deleted, or another tenant's. | `Not found.` |

> **`DRAFT`, `SCHEDULED`, and campaigns that never ran delete freely** — including ones that
> were cancelled before launch or failed to materialise. Deleting those gives the plan
> allowance back, because no call was ever placed.
>
> **A campaign that ran is a billing record.** `started_at` is stamped once, when the runner
> takes the campaign, and it is what stops a tenant running their month's campaigns, deleting
> them, and running the same number again. So the guard is not about the runner still being
> busy — a `COMPLETED` campaign from six months ago is equally undeletable.

> **Members are not deleted.** They cascade only if the campaign row is hard-deleted, which this
> is not — the compliance record of who was suppressed and why survives the soft delete.

> **The guard lives on the model, not the view.** That is what makes `bulk_delete` below safe:
> it calls `soft_delete()` directly on each row, so a view-only check would have left it as an
> open route to deleting a live campaign.

---

## `POST /bulk_delete/`

Soft-delete several campaigns at once. **All-or-nothing** — if any one is refused, none are
deleted.

### Request

```json
{ "ids": ["3f2a…", "7b1c…"] }
```

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Campaigns to soft-delete. | Non-empty list. Every id must exist, be undeleted, and be in your tenant. None may be live. |

### Response `200`

```json
{ "status": true, "message": "Bulk deleted successfully", "data": null, "errors": null }
```

| Code | When | `message` |
|---|---|---|
| **200** | All soft-deleted. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | Any id unknown, already deleted, or another tenant's — **nothing is deleted**. | `Some ids are invalid` |
| **409** | **Any** campaign in the batch is live — **nothing is deleted**. | `This campaign is still running. Cancel it before deleting.` |
| **409** | **Any** campaign in the batch has ever run — **nothing is deleted**. | `This campaign has already run and forms part of your billing record, so it cannot be deleted. You can archive it instead.` |
| **401** / **403** | As elsewhere. | — |

> **The 409 does not say which campaign blocked it.** The message is the model's, raised by the
> first offending row. With a mixed batch you will have to check statuses yourself — or delete
> individually, where the id is unambiguous.

> **Duplicate ids fail the whole request** with `Some ids are invalid`, because the check compares
> matched rows against the length of `ids`. De-duplicate client-side.

---

## `GET /dropdown/`

Compact picker options.

| Param | Type | Description | Validation |
|---|---|---|---|
| `limit` | int | Cap the number of options. | ≤ **1000**; over that → **400 `Limit cannot exceed 1000`**. A non-numeric value is ignored, not rejected. |
| *(list filters)* | — | Every filter and `search` from [`GET /`](#get--list) applies. | As there. |

`label` is the campaign name, `value` its id.

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

### Responses

| Code | When | `message` |
|---|---|---|
| **202** | Accepted. Now `SCHEDULED` (future start) or `MATERIALIZING` (immediate). **Not yet running** — 202, not 200, because the work happens after the response. | `Campaign launched` |
| **409** | Not in `DRAFT`. Includes the double-click case, where the second request loses the row lock race. | `A campaign in RUNNING cannot be launched.` |
| **409** | The bot was deleted or deactivated since the campaign was created. | `The bot for this campaign is no longer active.` |
| **409** | Nobody in the audience can be called. The campaign **stays `DRAFT`** so you can fix the selection and retry. | `This campaign has no contacts that can be called. Everyone selected is either suppressed or has no usable phone number.` |
| **402** | Launching now would exceed the plan's campaign allowance for the current window. Only on an **immediate** launch; a future-dated one is checked when it becomes due. Not returned while `entitlements_enforced` is off. | `You are using 10 of 10 available campaign monthly. Upgrade your plan or buy more capacity to continue.` |
| **404** | The campaign was deleted between the lookup and the lock, or is another tenant's. | `Campaign not found.` / `Not found.` |
| **401** / **403** | As elsewhere. | — |
| **429** | Throttled. Shares one 30/min budget with `cancel` and `preview-audience`. | `Request was throttled. Expected available in N seconds.` |

> **The callable check runs synchronously, before the 202.** Resolving the audience up front costs
> a scan, but it means a pointless campaign is refused immediately rather than accepted and landing
> in `FAILED` minutes later with nobody watching.

> **The allowance is re-checked here, not just at create.** A campaign created last month
> could otherwise start against this month's window without ever being counted against
> either. The check asks *are you still within your allowance*, not *may you add one more* —
> this campaign already exists and is already counted.
>
> **A `202` is not a promise it will run.** Materialisation happens in a background task and can
> still fail afterwards — most often because every remaining contact opted out in the gap between
> launch and the task running. Poll `GET /{id}/` and watch for `FAILED` plus `error_message`.

---

## `POST /{id}/pause/`

Hold a running campaign. `RUNNING` → `PAUSED`, recording `paused_by`. No request body.

### Response `200`

```json
{ "status": true, "message": "Campaign paused", "data": { "status": "PAUSED" }, "errors": null }
```

Full detail object; abbreviated above.

| Code | When | `message` |
|---|---|---|
| **200** | Paused. | `Campaign paused` |
| **409** | Any status other than `RUNNING`. | `A campaign in DRAFT cannot be paused.` |
| **404** | Deleted between lookup and lock, or another tenant's. | `Campaign not found.` / `Not found.` |
| **401** / **403** | As elsewhere. | — |

> **Pausing is recorded here, not enforced here.** This service does not hold the call queue — the
> runner does. `PAUSED` is the instruction; how quickly in-flight calls stop is the runner's
> behaviour. Members already `IN_PROGRESS` may still complete.

> **Not throttled.** Only `launch`, `cancel`, and `preview-audience` are.

---

## `POST /{id}/resume/`

Release a hold. `PAUSED` → `RUNNING`. No request body, and no actor is recorded — unlike pause and
cancel, there is no `resumed_by` field.

### Response `200`

```json
{ "status": true, "message": "Campaign resumed", "data": { "status": "RUNNING" }, "errors": null }
```

| Code | When | `message` |
|---|---|---|
| **200** | Resumed. | `Campaign resumed` |
| **409** | Any status other than `PAUSED`. | `A campaign in RUNNING cannot be resumed.` |
| **404** | Deleted between lookup and lock, or another tenant's. | `Campaign not found.` / `Not found.` |
| **401** / **403** | As elsewhere. | — |

> **`paused_by` is not cleared on resume.** It keeps the last pauser, so after a pause/resume cycle
> it reads as a stale "paused by" in the UI. Show it only when `status == PAUSED`.

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

| Code | When | `message` |
|---|---|---|
| **200** | Cancelled. Terminal — there is no un-cancel. | `Campaign cancelled` |
| **409** | Already terminal (`COMPLETED`, `CANCELLED`, or `FAILED`). | `A campaign in COMPLETED cannot be cancelled.` |
| **404** | Deleted between lookup and lock, or another tenant's. | `Campaign not found.` / `Not found.` |
| **401** / **403** | As elsewhere. | — |
| **429** | Throttled. Shares one 30/min budget with `launch` and `preview-audience`. | `Request was throttled. Expected available in N seconds.` |

> **Cancellable from any non-terminal state**, including `DRAFT` and `SCHEDULED` — not just while
> running. Cancelling a `DRAFT` campaign is how you retire one without deleting the record.

> **Members are cancelled with a single bulk update**, not row by row, because an audience can run
> to six figures. `PENDING` and `IN_PROGRESS` become `CANCELLED`; counters are recomputed
> immediately, so the response already reflects the new totals.

---

## `GET /{id}/members/`

The frozen audience. Empty until materialisation completes.

### Query parameters

| Param | Type | Description | Validation |
|---|---|---|---|
| `status` | enum | Narrow to one or more member states. Comma-separated (`?status=PENDING,IN_PROGRESS`) or the key repeated (`?status=PENDING&status=IN_PROGRESS`); a member matching any of them is returned. | `PENDING`, `SUPPRESSED`, `CANCELLED`, `IN_PROGRESS`, `COMPLETED`, `FAILED`, uppercase. Spaces around commas are ignored. **Not validated** — see below. |
| `suppression_reason` | enum | Narrow to one exclusion reason. Pair with `status=SUPPRESSED` for a compliance list. | `DO_NOT_CALL`, `CONSENT_DENIED`, `NO_PHONE`, `CONTACT_DELETED`. **Not validated.** |
| `search` | string | Substring match on `phone_e164` **only**. | Case-insensitive. Does **not** search names — the audience is keyed by number here. |
| `outcome` | string | Exact match on `outcome`. Supports multiple values (comma-separated or multiple query params). | Matches the outcome text. |
| `start_date` / `end_date` | datetime | Filter by `last_call_at` time range. | Standard ISO datetime formats. |
| `min_duration` / `max_duration` | int | Length of the member's **latest call** in seconds, both inclusive: `?min_duration=30&max_duration=300`. Either can be sent alone. | Whole numbers, `0` or more, and `min_duration` no greater than `max_duration`, else **400**. Sent empty (`?min_duration=`) counts as not sent. Setting either leaves out members with no linked call. |
| `ordering` | string | Sort. Default `created_at` (oldest first). | `full_name`, `status`, `attempt_count`, `last_call_at`, `duration_seconds`, `created_at`; prefix `-` to reverse, comma-separate for multiple keys. Unrecognised keys are dropped silently. |
| `page`, `page_size` | int | Pagination. | Default page size 10, max 100. |

> **`ordering=full_name` sorts on the contact's current name**, not a frozen copy — unlike
> `search`, which matches the number as it stood at materialisation. Sorting joins through to
> the contact; the number does not.

> **Every sort is tie-broken by `created_at`.** Members materialised in one transaction share
> a status and an attempt count, and an unordered tie lets Postgres return page 2 overlapping
> page 1.

> **`status`, `suppression_reason` and `outcome` are not validated against their choices.**
> (`min_duration` / `max_duration` are: a bad value there is a **`400`**.) Unlike the campaign list, a
> misspelled value such as `?status=PENDNIG` returns **`200` with an empty list**, not a `400`.
> An empty result therefore means either "none match" or "you typed it wrong", and the response
> cannot tell you which — check the spelling before concluding the audience is empty.

> **`search` matches the frozen number, not the contact's current one.** If a contact's phone was
> edited after materialisation, searching their new number finds nothing here. That is deliberate:
> the member row records what was actually handed over.

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
      "analytics_id": null,
      "duration_seconds": null,
      "created_at": "2027-02-15T04:00:03Z"
    },
    {
      "id": "5e8a…",
      "contact_id": "a7b9…",
      "full_name": "Grace Hopper",
      "phone_e164": "+14155552672",
      "status": "COMPLETED",
      "suppression_reason": "",
      "attempt_count": 2,
      "last_call_at": "2027-02-16T09:12:40Z",
      "outcome": "meeting_booked",
      "error_message": "",
      "analytics_id": "7a1c…",
      "duration_seconds": 312,
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
      "analytics_id": null,
      "duration_seconds": null,
      "created_at": "2027-02-15T04:00:03Z"
    }
  ],
  "errors": null,
  "meta": { "count": 500, "next": "…?page=2", "previous": null, "page": 1, "page_size": 10 }
}
```

Deliberately limited to name and number rather than the full contact, so a campaign export does
not widen the PII surface. Use `/contacts/{contact_id}/` for the rest.

| Code | When | `message` |
|---|---|---|
| **200** | Members returned. **Empty until materialisation completes** — a `DRAFT` or `SCHEDULED` campaign has no members yet, and that is not an error. | `success` |
| **400** | `min_duration` or `max_duration` is not a whole number `0` or more, or `min_duration` is greater than `max_duration`. The field is named under `errors`. | the first error |
| **401** / **403** | As elsewhere. | — |
| **404** | No such campaign, soft-deleted, or another tenant's. | `Not found.` |

> **`duration_seconds` is the length of the call `analytics_id` opens**, the member's latest
> call, not the sum of all attempts and not the longest one. A member whose first call ran ten
> minutes and whose latest ran ten seconds shows, filters and sorts as `10`. It is `null` whenever
> `analytics_id` is `null` (see below).
>
> Because it is null for members with no linked call, `min_duration` or `max_duration` leaves
> those members out, even `?min_duration=0`. Sorting by it puts them **last** ascending and
> **first** descending, the same as `ordering=last_call_at`.

> **`suppression_reason` is `""`, not `null`, for members that were not suppressed.** Test for
> truthiness rather than `!== null`.

> **`attempt_count`, `last_call_at`, `outcome`, and `error_message` are mirrored from the runner**,
> not authoritative. They stay at their defaults until the runner reports, so zeros on a freshly
> materialised campaign mean "nothing reported yet", not "tried and got nowhere".

> **`analytics_id` opens the member's call.** It is the id of the member's most recent call log:
> pass it to `GET /agent-call-logs/{id}/` for the transcript, summary and recording. If the member
> was called more than once it is the newest call, and `attempt_count` says how many there were.
> It is the same call the campaign report's "Provider call ID" column names, unless that call log
> was deleted: this skips deleted logs, because their detail endpoint returns 404.
>
> It is `null` when no call log is linked to the member. That covers members never called
> (`PENDING`, `SUPPRESSED`, `CANCELLED`), and also calls whose log was posted *after* the runner
> reported the outcome: the link is made only when the outcome arrives and that call log already
> exists, and it is not retried later.

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

| Code | When | `message` |
|---|---|---|
| **200** | Counters returned. Before materialisation everything is `0` and both maps are `{}`. | `Retrieved successfully` |
| **401** / **403** | As elsewhere. | — |
| **404** | No such campaign, soft-deleted, or another tenant's. | `Not found.` |

> **Prefer this over `/members/` for dashboards.** It answers "how is this campaign doing" with two
> aggregate queries instead of paging an audience that may run to six figures. It takes no
> filters — it is deliberately the cheap, whole-campaign view.

> **The live maps are the trustworthy ones.** `by_status` and `suppressed_by_reason` are computed
> per request; the flat counters are denormalised and only refreshed at materialisation, on cancel,
> and when the runner reports. If they disagree, believe `by_status`.

---

## `POST /{id}/reports/`

Request an **XLSX or PDF report** on this campaign. Returns **202** immediately with a report in
`PENDING`; the file is built in the background. Polling and downloading are in the
[Campaign Reports API](campaign_report.md), which documents this endpoint in full.

### Request

```json
{ "report_format": "XLSX" }
```

| Field | Type | Description | Validation |
|---|---|---|---|
| `report_format` | enum | Which file to produce. | Optional, default `XLSX`. `XLSX` or `PDF` only. |

### Responses

| Code | When | `message` |
|---|---|---|
| **202** | Queued. `data` is the report; poll `GET /api/v1/core/campaign-reports/{data.id}/`. | `Report requested` |
| **400** | Unknown `report_format`. | `"CSV" is not a valid choice.` |
| **401** / **403** | As elsewhere. `VB_AGENT` tokens are refused. | — |
| **404** | No such campaign, soft-deleted, or another tenant's. | `Not found.` |
| **429** | Throttled. Shares the 30/min budget with `launch`, `cancel` and `preview-audience`. | `Request was throttled. Expected available in N seconds.` |

> **Every request builds a new report**, in any campaign status — `DRAFT` included. Disable the
> button while the request is in flight, or a double click builds the file twice.

---

## `POST /preview-audience/`

How many of these contacts can actually be called, and why the rest cannot. **Detail-free**, so
the wizard can call it before the campaign exists.

### Request

```json
{ "contact_ids": ["c1a2…", "d3f4…", "e5f6…"] }
```

| Field | Type | Description | Validation |
|---|---|---|---|
| `contact_ids` * | UUID array | The prospective audience to evaluate. No campaign needs to exist. | Required — but **may be empty**, which returns all-zero counts rather than an error. At most **5 000** entries. Each must be a UUID; ids that are unknown, deleted, or another tenant's are silently ignored rather than rejected. |

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

### Responses

| Code | When | `message` | `errors` |
|---|---|---|---|
| **200** | Counts returned. An empty `contact_ids` gives zeros, not an error. | `Retrieved successfully` | `null` |
| **400** | More than 5 000 ids. | `A campaign cannot target more than 5000 contacts at once.` | `{"contact_ids": [...]}` |
| **400** | `contact_ids` omitted. | `This field is required.` | `{"contact_ids": [...]}` |
| **400** | An entry is not a valid UUID. | `"abc" is not a valid UUID.` | `{"contact_ids": {...}}` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": …}` |
| **403** | No active tenant mapping, or a `VB_AGENT` token. | `Your user account is not associated with any active tenant.` | `{"detail": …}` |
| **429** | Throttled. Shares one 30/min budget with `launch`, `cancel` and `reports`. | `Request was throttled. Expected available in N seconds.` | `{"detail": …}` |

> **Debounce this.** It scans every selected contact, and it draws from the same 30/min budget as
> `launch` — a wizard that previews on each keystroke will lock the user out of launching.

> **Suppression is evaluated live, not cached.** A preview is accurate as of the moment you call
> it; a contact can opt out between preview and launch, and again between launch and
> materialisation. Treat `callable` as an estimate, not a guarantee.

---

## Error reference

**Every error uses the standard envelope** — see [Response envelope](#response-envelope). Nothing
here returns a bare DRF body, and every error carries `request_id`.

| Code | Meaning | `message` | `errors` |
|---|---|---|---|
| `400` | Validation failed. Fix the payload. | First field error, or the guard's own message | `{"<field>": [...]}` or `{"code": "NO_ACTIVE_BOT"}` |
| `401` | Missing, malformed, or expired token. | `Authentication credentials were not provided.` | `{"detail": …}` |
| `403` | No active tenant mapping, or a `VB_AGENT` token on anything but `retrieve`. | `Your user account is not associated with any active tenant.` / `This endpoint is not available to agent tokens.` | `{"detail": …}` |
| `404` | Not found, soft-deleted, **or another tenant's**. Never 403, which would confirm it exists. | `Not found.` (`Campaign not found.` from lifecycle actions) | `{"detail": …}` |
| `409` | Wrong state for this action. | Names the current status and what it forbids | usually `null` |
| `429` | Throttled. | `Request was throttled. Expected available in N seconds.` | `{"detail": …}` |
| `500` | Unexpected server fault. | `Something went wrong` | `null` |

### 400 vs 409

- **400** — the request is wrong. Fix the payload and resend.
- **409** — the request is fine, the campaign moved. Refresh and reconsider.

That distinction drives the UI: a 400 belongs on the form field, a 409 belongs in a "this changed,
reload" prompt. Sending the same 400 payload again always fails; retrying a 409 after a refresh
often succeeds.

### Throttling

`launch`, `cancel`, `preview-audience` and `reports` share **one 30/min budget per user** — the
scope is shared, not per-endpoint, so 30 preview calls exhaust your launch allowance too. Preview
is included because it counts across the whole selected audience, and an under-debounced wizard
will call it on every keystroke; `reports` because each request queues a scan of the whole
campaign in a worker.

Everything else on this API is unthrottled.

## Configuration

| Setting | Default | Effect |
|---|---|---|
| `CAMPAIGN_MAX_SELECTED_CONTACTS` | `5000` | Cap on `contacts` and on `preview-audience` ids. |
| `CAMPAIGN_MAX_MEMBERS` | `100000` | Materialisation refuses above this and moves the campaign to `FAILED`. |
| `CAMPAIGN_MATERIALIZE_BATCH_SIZE` | `1000` | Rows per savepoint while freezing the audience. |
| `CAMPAIGN_MAX_CONCURRENT_RUNNING` | `5` | **Reserved — not enforced today.** |

Scheduled campaigns are swept **every 60 seconds**, so `scheduled_start_at` is a "not before"
guarantee rather than an exact firing time.

## Related

- [Campaign Runner API](campaign_runner.md) — the machine-facing side that pulls context and reports outcomes
- [Campaign Reports API](campaign_report.md) — poll and download the reports requested with `POST /{id}/reports/`
- [Campaign Overview](../../campaigns/README.md) — architecture and status diagrams
- [Contacts API](./contact.md) — where the audience comes from, and where `do_not_call` is set
- [Agent API](./agent.md) — the bot that places the calls
- [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md)

# Tenant Dashboard API

Every widget on the tenant analytics dashboard, in one request, for one date range.

**Base path**: `/api/v1/core/dashboard/`

---

## Contents

1. [Why one endpoint](#1-why-one-endpoint)
2. [`GET /summary/`](#2-get-summary)
3. [Date range](#3-date-range)
4. [Full response](#4-full-response)
5. [Field reference](#5-field-reference)
6. [Empty states](#6-empty-states)
7. [Error responses](#7-error-responses)
8. [What is deliberately not returned](#8-what-is-deliberately-not-returned)
9. [Performance](#9-performance)
10. [Known gaps](#10-known-gaps)
11. [Related](#11-related)

---

## 1. Why one endpoint

Every widget on the page is filtered by the same date range. Nine endpoints would mean
nine round trips that each have to agree about what "this month" means, and they would
drift the first time one of them rounded a boundary differently — leaving a dashboard
whose tiles contradict its own chart.

One request, one range, one answer.

This is **not** the full analytics module described in
`docs/dashboard/modules/06-analytics.md`. There is no metric registry and there are no
pre-computed rollups. Everything here is a live aggregate over data the existing models
already hold.

---

## 2. `GET /summary/`

- **Path**: `GET /api/v1/core/dashboard/summary/`
- **Headers**: `Authorization: Bearer <token>`
- **Auth**: any authenticated tenant user — owner, admin or member alike. Scoped to the
  caller's own tenant; there is no parameter that reaches another's.

| Query param | Type | Required | Description |
|---|---|---|---|
| `from` | date | No | Inclusive start, `YYYY-MM-DD`. Defaults to 6 days before `to`. |
| `to` | date | No | Inclusive end, `YYYY-MM-DD`. Defaults to now. |

```
GET /api/v1/core/dashboard/summary/
GET /api/v1/core/dashboard/summary/?from=2026-08-29&to=2026-09-04
```

`from` is taken from `00:00:00` of that day and `to` from `23:59:59.999999`, both UTC —
so a single-day range (`from=to=2026-09-04`) returns that whole day.

---

## 3. Date range

### Filtered on `created_at`

**Not `started_at`.** `started_at` is nullable, and a range filter on a nullable column
silently excludes every row that lacks one — a call log posted without a start time
would vanish from every total rather than being counted late. `created_at` is always
present, so the figures add up to the number of calls that actually exist.

The trade is that a log posted well after the call ended is filed on the day it
*arrived* rather than the day it *happened*. That is visible and explainable; silently
missing calls are neither.

`started_at` is still returned per call in `recent_calls`, so a client can show the real
call time even though the bucketing uses arrival. The
[call log list](../core/agent_call_log.md) filters on the same column, so a list a user
clicks through to always agrees with the total they clicked.

### The comparison window

Every `delta_pct` compares against the **immediately preceding window of the same
length**, derived server-side. A 7-day range is compared with the 7 days before it; a
30-day range with the 30 days before it. The client never computes this, so the two can
never disagree about how long "the same period last time" is.

Both windows are returned in `range` so the client can label them.

### `delta_pct` is `null`, not `0`

When the previous window held nothing, the delta is `null`. "No change" and "nothing to
compare against" are different statements, and a tenant's first week should not render
`+0.0%` as though it knew. **Render `null` as an em dash or hide the badge — never as
zero.**

---

## 4. Full response

Every field the endpoint can return, populated.

**`GET /api/v1/core/dashboard/summary/?from=2026-08-29&to=2026-09-04`** → **200 OK**

```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "range": {
      "from": "2026-08-29T00:00:00+00:00",
      "to": "2026-09-04T23:59:59.999999+00:00",
      "previous_from": "2026-08-22T00:00:00.000001+00:00",
      "previous_to": "2026-08-29T00:00:00+00:00"
    },
    "totals": {
      "total_calls":          { "value": 16,   "delta_pct": 12.5 },
      "avg_duration_seconds": { "value": 59,   "delta_pct": 2.4 },
      "connect_rate":         { "value": 90.0, "delta_pct": 3.2 },
      "active_agents":        { "value": 1 },
      "active_seats":         { "value": 3 }
    },
    "call_volume": [
      { "date": "2026-08-29", "inbound": 1, "outbound": 0, "total": 1 },
      { "date": "2026-08-30", "inbound": 0, "outbound": 0, "total": 0 },
      { "date": "2026-08-31", "inbound": 2, "outbound": 1, "total": 3 },
      { "date": "2026-09-01", "inbound": 1, "outbound": 3, "total": 4 },
      { "date": "2026-09-02", "inbound": 0, "outbound": 2, "total": 2 },
      { "date": "2026-09-03", "inbound": 2, "outbound": 1, "total": 3 },
      { "date": "2026-09-04", "inbound": 3, "outbound": 0, "total": 3 }
    ],
    "outcome_breakdown": [
      { "outcome": "information provided", "count": 7 },
      { "outcome": "callback requested",   "count": 4 },
      { "outcome": "no action",            "count": 3 }
    ],
    "campaigns": [
      {
        "id": "6f2a91c4-8d33-4b7e-9a10-2c5e77b41d08",
        "name": "Rabi Season Outreach",
        "status": "RUNNING",
        "dialed": 650,
        "dialable": 1000,
        "total_members": 1040,
        "pending": 350,
        "suppressed": 40,
        "completed": 590,
        "failed": 60,
        "progress_pct": 65.0
      },
      {
        "id": "b81d47e2-15aa-4c96-8f03-9de6a2c7154b",
        "name": "DentaCare Recall",
        "status": "RUNNING",
        "dialed": 120,
        "dialable": 500,
        "total_members": 512,
        "pending": 380,
        "suppressed": 12,
        "completed": 110,
        "failed": 10,
        "progress_pct": 24.0
      },
      {
        "id": "3ac0f918-77b2-49d1-a5e4-61c8bb90fe23",
        "name": "Property Followups",
        "status": "COMPLETED",
        "dialed": 250,
        "dialable": 250,
        "total_members": 250,
        "pending": 0,
        "suppressed": 0,
        "completed": 236,
        "failed": 14,
        "progress_pct": 100.0
      }
    ],
    "recent_calls": [
      {
        "id": "42ae04d5-964d-42c6-9c0b-365160e150c8",
        "customer_number": "+919228173970",
        "agent_name": "Vinyl Records",
        "direction": "inbound",
        "outcome": "no action",
        "duration_seconds": 47,
        "started_at": "2026-09-04T09:28:11+00:00"
      },
      {
        "id": "805f7762-367c-45b5-a7ca-9f1475c45964",
        "customer_number": "+919228173970",
        "agent_name": "Vinyl Records",
        "direction": "outbound",
        "outcome": "information provided",
        "duration_seconds": 132,
        "started_at": "2026-09-04T09:24:03+00:00"
      },
      {
        "id": "c1907b4e-2f88-4a55-b0d2-7e3419aa6d51",
        "customer_number": "+919812345601",
        "agent_name": "Vinyl Records",
        "direction": "inbound",
        "outcome": "callback requested",
        "duration_seconds": 0,
        "started_at": null
      }
    ],
    "agent_leaderboard": [
      {
        "agent_id": "9d4e1f60-3b27-4c88-91a5-0e77bd2a4c31",
        "agent_name": "Vinyl Records",
        "calls": 16,
        "connect_rate": 90.0,
        "avg_duration_seconds": 59
      }
    ],
    "bookings": [
      {
        "id": "e50b3a77-9c14-4d62-8ab1-72f0c9d5e618",
        "title": "Consultation — Amit Sharma",
        "attendee_name": "Amit Sharma",
        "host_name": "Priya Nair",
        "status": "CONFIRMED",
        "start_time": "2026-09-04T14:30:00+00:00"
      },
      {
        "id": "7c22ba09-6e41-4f38-95dc-1b804af3e7d2",
        "title": "Discovery Call",
        "attendee_name": "Sarah Connor",
        "host_name": "Aisha Khan",
        "status": "SCHEDULED",
        "start_time": "2026-09-05T10:00:00+00:00"
      }
    ]
  },
  "errors": null
}
```

---

## 5. Field reference

### `range`

| Field | Type | Notes |
|---|---|---|
| `from`, `to` | ISO 8601 | The window that was applied, after defaults. |
| `previous_from`, `previous_to` | ISO 8601 | The baseline every `delta_pct` was measured against. |

### `totals`

| Field | `value` type | `delta_pct` | Meaning |
|---|---|---|---|
| `total_calls` | integer | yes | Calls in range |
| `avg_duration_seconds` | integer | yes | Mean talk time **across connected calls only**. `0` when nothing connected |
| `connect_rate` | float \| `null` | yes | Percent of calls with any talk time. `null` when there were no calls |
| `active_agents` | integer | **no** | Live bots (`is_active`, not deleted) |
| `active_seats` | integer | **no** | Live tenant memberships |

`delta_pct` is a float or `null`, one decimal place, and may be negative.

> **`avg_duration_seconds` excludes zero-duration calls.** Including them would drag the
> average toward zero and turn it into a measure of answer rate rather than call length.

> **`active_agents` and `active_seats` are different things.** The design labels one tile
> "Active Seats (n agents)"; seats are people who can sign in, agents are bots. Both are
> returned so the client can label them correctly rather than picking one and hoping.

### `call_volume`

| Field | Type | Notes |
|---|---|---|
| `date` | `YYYY-MM-DD` | UTC calendar day |
| `inbound`, `outbound`, `total` | integer | |

**Every day in the range is present, including zero days.** A sparse series would
compress the x-axis and make a quiet Sunday look like it never happened. A 7-day range
always returns exactly 7 entries, ascending.

### `outcome_breakdown`

| Field | Type | Notes |
|---|---|---|
| `outcome` | string | Free text as the runner wrote it |
| `count` | integer | |

Highest count first. Blank and null outcomes are excluded, so these counts **do not**
necessarily sum to `total_calls`.

### `campaigns`

Up to **5**, newest first, excluding `DRAFT`. Not date-filtered.

| Field | Type | Meaning |
|---|---|---|
| `id`, `name` | string | |
| `status` | enum | `SCHEDULED`, `MATERIALIZING`, `HANDING_OFF`, `RUNNING`, `PAUSED`, `COMPLETED`, `CANCELLED`, `FAILED` |
| `dialed` | integer | `completed + failed` |
| `dialable` | integer | `total_members − suppressed` |
| `progress_pct` | float | `dialed / dialable`, `0.0` when `dialable` is 0 |
| `total_members`, `pending`, `suppressed`, `completed`, `failed` | integer | Raw counters |

> **`progress_pct` excludes suppressed members from the denominator.** They were never
> going to be dialled, so counting them would leave a fully-worked campaign reading as
> permanently incomplete.

### `recent_calls`

Up to **5**, newest first by `created_at`. **Not** filtered by the date range — "recent"
means recent, regardless of the window the rest of the page shows.

| Field | Type | Notes |
|---|---|---|
| `id` | string | |
| `customer_number` | string | The human's number, whichever end they were on — see [call logs](../core/agent_call_log.md#which-number-is-the-person) |
| `agent_name` | string \| `null` | |
| `direction` | `inbound` \| `outbound` | |
| `outcome` | string \| `null` | `null` when the runner sent none |
| `duration_seconds` | integer | `0` for an unconnected call |
| `started_at` | ISO 8601 \| **`null`** | Nullable — the runner does not always send it |

> **`started_at` can be `null` here** while the row still counts toward every total,
> because totals range on `created_at`. Clients must handle it; falling back to "just
> now" would be wrong.

### `agent_leaderboard`

Up to **10**, **ranked by call volume**. Range-filtered.

| Field | Type | Notes |
|---|---|---|
| `agent_id`, `agent_name` | string | |
| `calls` | integer | |
| `connect_rate` | float \| `null` | |
| `avg_duration_seconds` | integer | `0` when nothing connected |

Bots with no calls in the range are absent entirely — this is a leaderboard, not a
roster.

### `bookings`

Up to **5** upcoming appointments with `source = AI`, soonest first. **Not**
date-filtered — always the next 5 from now.

| Field | Type | Notes |
|---|---|---|
| `id`, `title` | string | |
| `attendee_name` | string \| `null` | First non-host participant |
| `host_name` | string \| `null` | **The person the appointment is with, not the bot that booked it.** Falls back to the host's email when they have no name |
| `status` | enum | `HELD`, `SCHEDULED`, `CONFIRMED`, `COMPLETED`, `CANCELED`, `NO_SHOW`, `REQUIRES_RESCHEDULE`, `RESCHEDULED` |
| `start_time` | ISO 8601 | |

> `Booking` has no foreign key to `Agent`. `source = AI` records *that* a bot booked it,
> never *which*. A "booked via <bot>" label needs a new field on `Booking` first.

---

## 6. Empty states

**A brand-new tenant with no data at all** still gets a complete, well-formed body —
every key is present, lists are empty, and nothing is `undefined`:

```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "range": {
      "from": "2026-08-29T00:00:00+00:00",
      "to": "2026-09-04T23:59:59.999999+00:00",
      "previous_from": "2026-08-22T00:00:00.000001+00:00",
      "previous_to": "2026-08-29T00:00:00+00:00"
    },
    "totals": {
      "total_calls":          { "value": 0, "delta_pct": null },
      "avg_duration_seconds": { "value": 0, "delta_pct": null },
      "connect_rate":         { "value": null, "delta_pct": null },
      "active_agents":        { "value": 0 },
      "active_seats":         { "value": 1 }
    },
    "call_volume": [
      { "date": "2026-08-29", "inbound": 0, "outbound": 0, "total": 0 },
      { "date": "2026-08-30", "inbound": 0, "outbound": 0, "total": 0 },
      { "date": "2026-08-31", "inbound": 0, "outbound": 0, "total": 0 },
      { "date": "2026-09-01", "inbound": 0, "outbound": 0, "total": 0 },
      { "date": "2026-09-02", "inbound": 0, "outbound": 0, "total": 0 },
      { "date": "2026-09-03", "inbound": 0, "outbound": 0, "total": 0 },
      { "date": "2026-09-04", "inbound": 0, "outbound": 0, "total": 0 }
    ],
    "outcome_breakdown": [],
    "campaigns": [],
    "recent_calls": [],
    "agent_leaderboard": [],
    "bookings": []
  },
  "errors": null
}
```

Things worth handling explicitly in the client:

| Situation | What you get |
|---|---|
| No calls in range | `connect_rate.value` is **`null`**, not `0` |
| No calls in the previous window | every `delta_pct` is **`null`** |
| Nothing connected, but calls exist | `avg_duration_seconds.value` is `0`, `connect_rate.value` is `0.0` |
| A day with no calls | present in `call_volume` with zeros |
| A call with no disposition | absent from `outcome_breakdown`, counted in `total_calls` |
| A call the runner sent no start time for | `started_at: null` in `recent_calls` |
| `active_seats` on a fresh tenant | `1` — the owner exists from onboarding |

---

## 7. Error responses

All errors carry `status: false` and `data: null`. **Two shapes, depending on where the
error came from** — worth knowing, because one has a `request_id` and the other does not.

### Returned by this endpoint

`400` and `403` are returned directly, so they carry no `request_id`:

```json
{
  "status": false,
  "message": "Range too wide — 366 days maximum.",
  "data": null,
  "errors": null
}
```

| Status | `message` | Cause |
|---|---|---|
| **400** | `` `from` must be a date, as YYYY-MM-DD. `` | Unparseable `from` |
| **400** | `` `to` must be a date, as YYYY-MM-DD. `` | Unparseable `to` |
| **400** | `` `from` cannot be after `to`. `` | Inverted range |
| **400** | `Range too wide — 366 days maximum.` | Span over `MAX_DAYS` |
| **403** | `Your user account is not associated with any organisation.` | Caller has no tenant mapping |

The 403 is reached by platform staff and machine accounts. There is no partial
response — the whole call fails, because every widget is tenant-scoped.

### Raised before this endpoint runs

Authentication failures never reach the view; they go through the project's exception
handler, which adds `request_id`:

```json
{
  "status": false,
  "message": "Authentication credentials were not provided.",
  "data": null,
  "errors": { "detail": "Authentication credentials were not provided." },
  "request_id": "45dbbcd6-a2e8-42d4-97e6-c4e4bef4113a"
}
```

| Status | When |
|---|---|
| **401** | Missing, malformed or expired token |
| **500** | Unhandled fault — `message` is `Something went wrong`, `errors` is `null` |

**Every response also carries an `X-Request-ID` header**, whether it succeeded or not.
Quote it when reporting a problem — it is the key the server logs are indexed by.

---

## 8. What is deliberately not returned

**No success rate, no conversion rate, no resolution rate.**

All three require `outcome` to be classifiable, and it is free text on both
[`AgentCallLog`](../core/agent_call_log.md) (*"Stored verbatim; no fixed vocabulary"*)
and `CampaignMember` (*"Runner-supplied disposition, stored verbatim"*).

Nothing in the backend can decide whether `callback requested` is a success. Inventing a
number from an unclassified string would be worse than omitting it: it would look
authoritative and mean nothing, and every downstream report would inherit the fiction.

What is returned instead:

| Instead of | You get | Why it is honest |
|---|---|---|
| Success rate | `connect_rate` | Derived from `duration_seconds`; genuinely true |
| Conversion rate | `dialed` / `dialable` / `progress_pct` | Derived from real `CampaignMember` statuses |
| Resolution rate | `outcome_breakdown` + per-agent `connect_rate` | The counts, without a verdict |

**To get the real rates**, `outcome` needs a vocabulary — a fixed enum, a per-tenant
mapping of which strings count as success, or a flag the runner sets. That is a product
decision, and once made these become straightforward additions here.

Also absent: the design's bot **"Standby"** badge. `Agent` has no status field, only
`is_active`.

---

## 9. Performance

**Twelve queries, all bounded.** No N+1 anywhere: `recent_calls` uses
`select_related("agent")`, `bookings` uses `prefetch_related("participants")` and picks
the attendee in Python rather than issuing a filter per row.

### The index this depends on

```python
models.Index(fields=["tenant", "-created_at"], name="core_call_tenant_created_idx")
```

Added in `core/migrations/0057_*`. Without it, `tenant_id` alone is the only usable
index: Postgres fetches every call the tenant has ever made and then filters by date —
fine at a hundred rows, a sequential scan at a million. Descending, so the same index
also serves the newest-first reads.

`Booking` already carries `(tenant, status, start_time)`.

### Bounded by construction

| Guard | Value | Why |
|---|---|---|
| `MAX_DAYS` | 366 | A five-year request would build 1,825 chart buckets over a full scan. Refused with a 400 rather than served slowly |
| `DEFAULT_DAYS` | 7 | A caller with no range gets a week, not everything |
| `PREVIEW_LIMIT` | 5 | List widgets are previews behind a "View All", not tables |
| `LEADERBOARD_LIMIT` | 10 | |

### When this stops being enough

Live aggregation is the right call while a tenant's call table is small, and it removes
an entire class of staleness bug — there is no rollup to be out of date. It stops being
right when a single tenant's call volume makes these aggregates slow even with the
index. At that point the answer is pre-computed daily rollups, which is what
`docs/dashboard/modules/06-analytics.md` describes.

> **Deploying the index on a large table.** `AddIndex` builds it with a lock that blocks
> writes for the duration. On a call-log table that is already large in production, swap
> the migration to `AddIndexConcurrently` with `atomic = False` before deploying.

---

## 10. Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **No success / conversion / resolution rate.** `outcome` has no vocabulary. | The three headline percentages the design asks for cannot be computed. See §8. |
| 2 | **Date bucketing uses arrival, not call time.** | A log posted late is filed on the day it arrived. Deliberate — see §3. |
| 3 | **`Booking` has no link to `Agent`.** | "Booked via <bot>" is not answerable; `host_name` is the human host. |
| 4 | **`Agent` has no status field.** | No "Standby"/"Active" badge; only `is_active`. |
| 5 | **No time-to-answer anywhere.** | If "Avg Response" means answer latency rather than talk time, it needs a new field on the call log. |
| 6 | **Live aggregates, no rollups.** | Fine now; revisit when one tenant's call volume makes it slow. |
| 7 | **`bookings` and `campaigns` ignore the date range.** | Both always show current state, so changing the range does not change them. |
| 8 | **No pagination on any list block.** | Fixed caps of 5 and 10; "View All" must go to the underlying resource's own endpoint. |

---

## 11. Related

- [Agent Call Logs](../core/agent_call_log.md) — the source for volume, duration and outcome; its list filters on the same column
- [Campaigns](../core/campaign.md) — the counters behind campaign progress
- [Bookings](../core/booking.md) — the full booking resource behind "View All"
- `docs/dashboard/modules/06-analytics.md` — the full analytics module this is a fast slice of

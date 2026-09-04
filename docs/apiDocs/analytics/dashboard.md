# Tenant Dashboard API

Every widget on the tenant analytics dashboard, in one request, for one date range.

**Base path**: `/api/v1/core/dashboard/`

---

## Contents

1. [Why one endpoint](#1-why-one-endpoint)
2. [`GET /summary/`](#2-get-summary)
3. [Date range](#3-date-range)
4. [Response](#4-response)
5. [What is deliberately not returned](#5-what-is-deliberately-not-returned)
6. [Performance](#6-performance)
7. [Known gaps](#7-known-gaps)
8. [Related](#8-related)

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
- **Auth**: any authenticated tenant user. Scoped to the caller's own tenant; there is
  no way to ask for another's.

| Query param | Type | Description |
|---|---|---|
| `from` | date | Inclusive start, `YYYY-MM-DD`. Defaults to 6 days before `to`. |
| `to` | date | Inclusive end, `YYYY-MM-DD`. Defaults to now. |

| Status | When | `message` |
|---|---|---|
| **200** | Retrieved. | `Retrieved successfully` |
| **400** | `from`/`to` unparseable, `from` after `to`, or range wider than 366 days. | e.g. `Range too wide — 366 days maximum.` |
| **401** | Missing or invalid token. | — |
| **403** | Caller has no tenant mapping. | `Your user account is not associated with any organisation.` |

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
call time even though the bucketing uses arrival.

### The comparison window

Every `delta_pct` compares against the **immediately preceding window of the same
length**, derived server-side. A 7-day range is compared with the 7 days before it; a
30-day range with the 30 days before it. The client never computes this, so the two can
never disagree about how long "the same period last time" is.

Both windows are returned in `range` so the client can label them.

### `delta_pct` is `null`, not `0`

When the previous window held nothing, the delta is `null`. "No change" and "nothing to
compare against" are different statements, and a tenant's first week should not render
`+0.0%` as though it knew.

---

## 4. Response

```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "range": {
      "from": "2026-08-29T00:00:00Z",
      "to": "2026-09-04T23:59:59Z",
      "previous_from": "2026-08-22T00:00:00Z",
      "previous_to": "2026-08-29T00:00:00Z"
    },
    "totals": { },
    "call_volume": [ ],
    "outcome_breakdown": [ ],
    "campaigns": [ ],
    "recent_calls": [ ],
    "agent_leaderboard": [ ],
    "bookings": [ ]
  },
  "errors": null
}
```

### `totals` — the stat tiles

| Key | Shape | Meaning |
|---|---|---|
| `total_calls` | `{value, delta_pct}` | Calls in range |
| `avg_duration_seconds` | `{value, delta_pct}` | Mean talk time **across connected calls only** — calls with `duration_seconds = 0` are excluded, or every unanswered call would drag the average toward zero and the number would measure answer rate rather than call length |
| `connect_rate` | `{value, delta_pct}` | Percentage of calls with any talk time. **This is what stands in for "success rate"** — see §5 |
| `active_agents` | `{value}` | Live bots |
| `active_seats` | `{value}` | Live tenant memberships |

> **`active_agents` and `active_seats` are different things.** The design labels one tile
> "Active Seats (n agents)"; seats are people who can sign in, agents are bots. Both are
> returned so the client can label them correctly rather than picking one and hoping.

### `call_volume` — the chart

```json
[{ "date": "2026-08-29", "inbound": 3, "outbound": 5, "total": 8 }]
```

Every day in the range is present, including days with no calls. A sparse series would
compress the x-axis and make a quiet Sunday look like it never happened.

### `outcome_breakdown`

```json
[{ "outcome": "information provided", "count": 5 }]
```

Counts per disposition, highest first. Blank outcomes are excluded. This is the raw
material for whatever "success" is eventually defined to be — see §5.

### `campaigns`

Up to 5, newest first, excluding `DRAFT`.

| Key | Meaning |
|---|---|
| `dialed` | `completed + failed` |
| `dialable` | `total_members − suppressed` |
| `progress_pct` | `dialed / dialable` |
| `pending`, `suppressed`, `completed`, `failed`, `total_members` | Raw counters |

> **`progress_pct` excludes suppressed members from the denominator.** They were never
> going to be dialled, so counting them would leave a fully-worked campaign reading as
> permanently incomplete.

Numbers come from the counters `Campaign` already maintains, so this costs one query and
never scans members.

### `recent_calls`

Up to 5, newest first. **Not** filtered by the date range — "recent" means recent,
regardless of what window the rest of the page is showing.

| Key | Notes |
|---|---|
| `customer_number` | The human's number, whichever end they were on — see [call logs](../core/agent_call_log.md#which-number-is-the-person) |
| `agent_name`, `direction`, `outcome`, `duration_seconds`, `started_at` | |

### `agent_leaderboard`

Up to 10, **ranked by call volume**, with `calls`, `connect_rate` and
`avg_duration_seconds` per bot. Not ranked by resolution rate — see §5.

### `bookings`

Up to 5 upcoming appointments with `source = AI`, soonest first.

| Key | Notes |
|---|---|
| `attendee_name` | First non-host participant |
| `host_name` | **The person the appointment is with, not the bot that booked it** |
| `title`, `status`, `start_time` | |

> `Booking` has no foreign key to `Agent`. `source = AI` records *that* a bot booked it,
> never *which*. A "booked via <bot>" label needs a new field on `Booking` first.

---

## 5. What is deliberately not returned

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

## 6. Performance

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

## 7. Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **No success / conversion / resolution rate.** `outcome` has no vocabulary. | The three headline percentages the design asks for cannot be computed. See §5. |
| 2 | **Date bucketing uses arrival, not call time.** | A log posted late is filed on the day it arrived. Deliberate — see §3. |
| 3 | **`Booking` has no link to `Agent`.** | "Booked via <bot>" is not answerable; `host_name` is the human host. |
| 4 | **`Agent` has no status field.** | No "Standby"/"Active" badge; only `is_active`. |
| 5 | **No time-to-answer anywhere.** | If "Avg Response" means answer latency rather than talk time, it needs a new field on the call log. |
| 6 | **Live aggregates, no rollups.** | Fine now; revisit when one tenant's call volume makes it slow. |
| 7 | **Bookings are not date-range filtered.** | `bookings` always shows the next 5 upcoming, regardless of the selected range. |

---

## 8. Related

- [Agent Call Logs](../core/agent_call_log.md) — the source for volume, duration and outcome
- [Campaigns](../core/campaign.md) — the counters behind campaign progress
- `docs/dashboard/modules/06-analytics.md` — the full analytics module this is a fast slice of

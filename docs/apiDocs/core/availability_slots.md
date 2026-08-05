# Available Slots API Documentation

Computes the times a meeting can actually be booked for one event template, over a window the
caller specifies.

The engine starts from each eligible host's materialized working hours and subtracts everything
that makes them unavailable — existing bookings *including their buffers*, approved leave, and busy
blocks from any connected external calendar — then applies the event template's minimum-notice and
horizon policy. What comes back is the set of start times that will succeed if booked immediately.

**This endpoint never books anything and never reserves anything.** Two callers can be handed the
same slot a millisecond apart; whoever posts first wins and the other gets a `409`. If you need to
hold a time while a customer decides, use `POST /bookings/hold/`.

> **VB_AGENT access:** allowed. Agent tokens **must** send `X-Tenant-ID: <tenant uuid>`
> (**400** if omitted), and the `event_template` must belong to that tenant (**403** otherwise).
> The tenant is taken from the header and then checked against the template — never derived
> from the template the caller named.
> See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/core/availability/available-slots/` | List bookable slots in a UTC window |

---

## 1. Get Available Slots

**`GET /api/v1/core/availability/available-slots/`**

Returns every bookable start time in the requested window, in both UTC and a display timezone.

- **Auth**: `Authorization: Bearer <token>` (plus `X-Tenant-ID` for agent tokens)
- **Idempotent**: yes — read-only, safe to retry and to poll.

### Query parameters

`*` marks required.

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `event_template` * | UUID | The meeting type to find times for. Determines duration, granularity, buffers, notice and horizon policy. | Must exist, be **active**, and belong to the caller's tenant. An inactive template is treated as not found → **400**. |
| `start_utc` * | ISO-8601 datetime | Start of the search window. **Inclusive.** | Must parse as ISO-8601. A value with no offset is read as UTC. |
| `end_utc` * | ISO-8601 datetime | End of the search window. **Exclusive** — a slot starting exactly at `end_utc` is not returned, so consecutive windows tile without overlap or gaps. | Must be strictly after `start_utc`. The span must not exceed the template's `max_horizon_days`. |
| `target_host` | UUID | The host whose calendar to check. | **Required for `DIRECT` templates**; **must be omitted for `COLLECTIVE`**. Must be a user configured (via `UserEventSkill`) to host this template — otherwise no slots are returned. |
| `timezone` | IANA zone name | Zone used to render the `*_local` fields, e.g. `Asia/Kolkata`. **Presentational only** — it never changes which slots are returned. | Must be a zone `tzdata` recognises. Defaults to the `target_host`'s timezone, then the tenant's, then `UTC`. |

#### Why `start_utc`/`end_utc` are instants and not dates

A date is a different 24 hours in every timezone, so `date_from=2026-08-08` cannot be answered
without deciding *whose* August 8th it is. **Your client is the only component that knows which
timezone the user picked**, so it resolves the day and sends instants:

```js
// The user picked Aug 8 while viewing in `userZone`.
const startUtc = zonedTimeToUtc('2026-08-08T00:00:00', userZone);
const endUtc   = zonedTimeToUtc('2026-08-09T00:00:00', userZone);
```

This is not cosmetic. With a host in `Asia/Kolkata` and a customer viewing in `America/New_York`,
treating "Aug 8" as the *host's* day returns three slots that land on **Aug 7** for the customer,
and omits two that land on their Aug 8 afternoon. Converting the response afterwards cannot repair
it — the wrong slots were chosen server-side.

### Response body — `200 OK`

```json
{
  "status": true,
  "message": "Available slots retrieved successfully",
  "data": {
    "timezone": "Asia/Kolkata",
    "slots": [
      {
        "start": "2026-07-05T14:00:00Z",
        "end": "2026-07-05T14:30:00Z",
        "start_local": "2026-07-05T19:30:00+05:30",
        "end_local": "2026-07-05T20:00:00+05:30",
        "eligible_host_ids": ["5fa85f64-5717-4562-b3fc-2c963f66afa7"],
        "external_check_failed": false
      }
    ]
  },
  "errors": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `data.timezone` | string | The IANA zone the `*_local` fields are expressed in. Echoed back so a UI can label the column without a second request. |
| `data.slots` | array | Bookable slots, ascending by `start`. **May be empty** — see below. |
| `slots[].start` | ISO-8601 UTC | **The canonical value. Send this as `start_time` when booking.** |
| `slots[].end` | ISO-8601 UTC | `start` + the template's duration. |
| `slots[].start_local` | ISO-8601 with offset | The *same instant* as `start`, rendered in `data.timezone`. For display only. |
| `slots[].end_local` | ISO-8601 with offset | The same instant as `end`, rendered locally. |
| `slots[].eligible_host_ids` | UUID array | Hosts free at this time. For `DIRECT` this is the one `target_host`. For `COLLECTIVE` it is every skilled host, all of whom get booked. |
| `slots[].external_check_failed` | boolean | `true` when a host's external calendar could not be read, so this slot is offered on **unverified** information — see below. |

#### Reading the two time forms

`start_local` is a *rendering* of `start`, never a different time. Both parse to the same instant.
The offset shown is resolved for that specific date, so a window spanning a DST transition
correctly carries different offsets on either side of it.

**Book with `start`.** Booking with `start_local` is arithmetically equivalent, but `start` is the
form the rest of the system speaks.

#### `external_check_failed: true`

A connected calendar provider (e.g. Google) was unreachable when this slot was computed. The slot
is still offered — an outage at Google should not empty your calendar — but the host may have a
conflicting external meeting nobody can see right now.

Surface it in the UI (a warning icon, "availability unconfirmed") and expect that booking it may
come back **503** while the provider is still down. Once the provider recovers, the flag clears on
the next request.

#### An empty `slots` array is a success, not an error

`200` with `"slots": []` is the normal answer to "nothing is free". Common causes, in the order
worth checking:

| Cause | How to tell |
|---|---|
| The window is fully booked or overlaps leave | Widen `start_utc`/`end_utc`. |
| Every candidate start falls inside `min_notice_minutes` | The template requires more notice than the window allows; try a later window. |
| The host has no availability profile | `GET /availability-profiles/?user=<id>` returns nothing. |
| The host's profile is `is_active: false` | Inactive profiles are excluded from availability entirely. |
| Availability was never materialized | The profile and rules exist but were created outside the API. Run `manage.py materialize_availability`. The server also logs `materialization is stale or failed` in this case. |
| `target_host` is not skilled for this template | The host has no `UserEventSkill` row for it. Check `GET /event-skills/`. |
| The window is beyond the horizon | Slots past `now + max_horizon_days` are filtered even if the range validates. |

### Error responses

Every error uses the standard envelope: `status: false`, a human `message`, `data: null`, and
`errors` carrying the field-level detail when there is any.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **400** | `end_utc` on or before `start_utc` | `end_utc must be after start_utc.` | `{"non_field_errors": [...]}` |
| **400** | Window longer than the template's horizon | `Range exceeds this event's 60-day horizon.` | `{"non_field_errors": [...]}` |
| **400** | `DIRECT` template with no `target_host` | `This event type requires a specific host.` | `{"target_host": [...]}` |
| **400** | `COLLECTIVE` template *with* a `target_host` | `Collective event types consider every skilled host.` | `{"target_host": [...]}` |
| **400** | Unknown or misspelled `timezone` | `'Mars/Olympus_Mons' is not a valid IANA timezone identifier.` | `{"timezone": [...]}` |
| **400** | Missing/unparseable `event_template`, `start_utc`, `end_utc` | field-specific | `{"<field>": [...]}` |
| **400** | Agent token without `X-Tenant-ID` | `This header is required for agent tokens.` | `{"X-Tenant-ID": [...]}` |
| **401** | Missing or invalid bearer token | `Authentication credentials were not provided.` | `{"detail": ...}` |
| **403** | Template belongs to another tenant | `Event template does not belong to your tenant.` | `{"detail": ...}` |
| **403** | Caller's user has no tenant mapping | `Your user account is not associated with any active tenant.` | `{"detail": ...}` |

Example — `400`:

```json
{
  "status": false,
  "message": "end_utc must be after start_utc.",
  "data": null,
  "errors": { "non_field_errors": ["end_utc must be after start_utc."] },
  "request_id": "a3f1…"
}
```

Example — `403`:

```json
{
  "status": false,
  "message": "Event template does not belong to your tenant.",
  "data": null,
  "errors": { "detail": "Event template does not belong to your tenant." },
  "request_id": "a3f1…"
}
```

> There is **no 404** on this endpoint. An event template the caller cannot see fails validation as
> a `400`, and one in another tenant is a `403` — neither confirms whether the id exists elsewhere.

---

## Behaviour by assignment strategy

| | `DIRECT` | `COLLECTIVE` |
|---|---|---|
| `target_host` | **required** | **must be omitted** |
| A slot is returned when… | that host is free | **every** skilled host is free |
| `eligible_host_ids` | the one requested host | all skilled hosts |
| Booking it creates | one booking | one booking per host, sharing a `group_id` |

`DIRECT` requires `target_host` because a booking names its host. Returning the union over all
skilled hosts would advertise times that then `409` when booked, since the specific host you picked
may not have been the free one.

---

## Notes and edge cases

- **Slots may straddle midnight.** Hosts can define overnight shifts (`22:00 → 06:00`), so
  `start` and `end` are not guaranteed to share a calendar date in any timezone. Do not group by
  the date substring of `start` — convert to your display zone first.
- **Availability is eventually consistent.** Editing an availability profile queues a background
  rebuild, so a slot list requested immediately after a profile write may reflect the previous
  rules for a few seconds.
- **Buffers are invisible here.** `buffer_before_minutes` / `buffer_after_minutes` shrink what is
  offered but never appear in `start`/`end`; those are always the customer-facing times.
- **Not cached.** Every call recomputes. External free/busy lookups are cached briefly server-side
  (`EXTERNAL_FREEBUSY_CACHE_SECONDS`, default 60s), so a change made directly in Google may take up
  to a minute to appear.

## Related

- [Booking API](./booking.md) — turn a slot into a booking
- [Availability Profile API](./availability_profile.md) — define the working hours behind these slots
- [Booking lifecycle flow](../../flows/02-booking-lifecycle.md)

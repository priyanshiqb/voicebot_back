# Business Hours API Documentation

When the **business** is open — the weekly opening hours for the whole tenant, in the tenant's own
timezone. Backs the working-hours grid.

- **Path**: `/api/v1/core/business-hours/`
- **Auth**: JWT. Machine (`VB_AGENT`) tokens are refused.
- **Shape**: a **singleton** — one row per tenant, so no id in the path and no list. The row is
  created on first read; you never have to `POST` one into existence.

> ## This is not an availability profile
>
> [`Availability Profiles`](./availability_profile.md) describe when an individual **person** is
> free, and drive bookable slots. This describes when the **company** is open.
>
> They legitimately disagree: a voicebot answers around the clock while staff work nine to five, and
> a host on leave does not close the business. Different subjects, different tables.
>
> **Business hours do not currently gate bookings.** Nothing in `available-slots` consults them —
> see [Known gaps](#known-gaps).

---

## How a week is expressed

Three rules carry the whole design. They match `AvailabilityRule` exactly, so anything you know about
one holds for the other.

| Rule | Meaning |
|---|---|
| **Several intervals per day are allowed** | `09:00–13:00` + `14:00–18:00` is a lunch closure — the normal shape for clinics and restaurants |
| **`closes_at <= opens_at` closes the next day** | `22:00 → 02:00` is an overnight shift. `00:00 → 00:00` is a full 24 hours |
| **A closed day has no intervals** | There is no `status: "holiday"` field. Closed is the absence of hours, so nothing can be closed-with-hours or open-with-none |

A zero-length interval is therefore not expressible, by design.

---

## 1. Read the schedule

`GET /api/v1/core/business-hours/`

No parameters. Creates the row if this tenant has never had one — reading must never fail because
nobody has written yet, since the same call renders the grid a tenant edits.

**200 OK**
```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "id": "c3d4e5f6-…",
    "timezone": {
      "id": "…",
      "timezone": "Asia/Kolkata",
      "offset": "UTC+05:30",
      "current_offset": "UTC+05:30"
    },
    "is_open": true,
    "closed_message": "",
    "schedule": [
      {
        "weekday": 0,
        "day": "Monday",
        "is_open": true,
        "intervals": [
          { "opens_at": "09:00:00", "closes_at": "13:00:00" },
          { "opens_at": "14:00:00", "closes_at": "18:00:00" }
        ]
      },
      { "weekday": 6, "day": "Sunday", "is_open": false, "intervals": [] }
    ],
    "created_at": "2026-08-07T10:58:03Z",
    "updated_at": "2026-08-07T09:12:44Z"
  },
  "errors": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `timezone` | object | IANA zone the wall-clock times are read in. **DST-aware** |
| `is_open` | boolean | Master switch — see below |
| `closed_message` | string | Optional line for the agent to use out of hours |
| `schedule` | array | **Always seven entries**, weekday order, Monday first |

The response also carries the standard audited-model block — `tenant`, `created_by`, `updated_by`,
`deleted_by`, `is_deleted`, `deleted_at` — trimmed from the example above for readability.

### `schedule` is always seven days

Closed days store nothing, but they still appear with `is_open: false` and an empty `intervals`
array. The grid a tenant edits has seven rows whatever the data says, and leaving the client to
synthesise the missing ones is how Sunday quietly disappears from the UI.

`is_open` per day is **derived** from whether that day has intervals. It is not stored, so it cannot
disagree with the list beside it.

> **The first read seeds `timezone` to UTC.** Not a guess at the tenant's real zone — a schedule
> silently anchored to the wrong timezone is worse than an obviously unset one, because every time in
> it is then wrong by hours without anything looking broken. Set it before the hours mean anything.

| Status | When | `message` |
|---|---|---|
| **200** | Always, for a user with a tenant. | `Retrieved successfully` |
| **400** | A superuser with no tenant mapping — there is no "all tenants" answer to give. | `No tenant context. Superusers must act within a tenant.` |
| **401** | No valid JWT. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping. | `Your user account is not associated with any active tenant.` |

---

## 2. Update the schedule

`PATCH /api/v1/core/business-hours/`

| Field | Type | Description |
|---|---|---|
| `timezone` | UUID | The zone the times are read in |
| `is_open` | boolean | Master switch |
| `closed_message` | string | Max 255 |
| `schedule` | array | The whole week — see replacement semantics below |

**Request**
```json
{
  "timezone": "a1b2c3d4-…",
  "schedule": [
    { "weekday": 0, "intervals": [
        { "opens_at": "09:00", "closes_at": "13:00" },
        { "opens_at": "14:00", "closes_at": "18:00" } ] },
    { "weekday": 5, "intervals": [{ "opens_at": "22:00", "closes_at": "02:00" }] }
  ]
}
```

Days you omit are **closed**. `day` and `is_open` are output-only — do not send them.

### `schedule` replaces the whole week

| You send | Result |
|---|---|
| `schedule` with entries | Those become the entire week. Days not listed are closed |
| `schedule: []` | Closed every day |
| **no `schedule` key** | The week is left exactly as it was |

That last row is the one to be careful with: `[]` and *absent* mean opposite things. Sending an empty
array to "leave it alone" wipes the schedule.

Replacement rather than merge because a weekly grid is edited as a whole. Patching individual
intervals would need stable ids for rows the tenant experiences as cells in a table.

### The master switch

`is_open: false` closes the business regardless of the schedule — for an indefinite shutdown, without
deleting hours that will be wanted again. Setting it back to `true` restores the week untouched.

### Validation

Intervals may not overlap **anywhere in the week**, and the week is treated as circular.

| Rejected | Why |
|---|---|
| `09:00–13:00` and `12:00–18:00` on Monday | Plain overlap |
| Monday `22:00–02:00` and Tuesday `01:00–09:00` | The overnight block runs into Tuesday |
| Sunday `22:00–03:00` and Monday `02:00–09:00` | **The week wraps.** Sunday night runs into Monday |
| The same `weekday` twice in one payload | Ambiguous — put all of a day's blocks in one entry |
| Coverage totalling more than 168 hours | Must contain an overlap |

Accepted, and worth knowing:

- **Seven `00:00 → 00:00` days** — exactly a week, so 24/7 is valid.
- **`09:00–13:00` then `13:00–18:00`** — closing time is exclusive, so touching blocks do not overlap.

| Status | When | `message` / `errors` |
|---|---|---|
| **200** | Updated. | `Updated successfully` |
| **400** | Overlapping hours. | `Overlapping opening hours: weekday 0 conflicts with weekday 1.` |
| **400** | A weekday listed twice. | `Weekday 0 appears more than once…` |
| **400** | `weekday` outside 0–6, or a malformed time. | Field errors under `errors.schedule` |
| **405** | `PUT`, `POST` or `DELETE`. | A singleton has nothing to create or destroy |

---

## Building the working-hours screen

1. **`GET`** once — you get seven rows whether or not anything is configured.
2. Render each day from `is_open` and `intervals`. An "Add hours" control appends a second interval;
   that is what makes split shifts possible.
3. On save, **`PATCH` the whole `schedule`**. Send only the days that have hours.
4. Show the timezone prominently. Until it is set it reads `UTC`, and every time on the screen means
   something different from what the tenant intends.

Weekday numbering is `0 = Monday … 6 = Sunday`, matching Python's `datetime.weekday()`. If your date
library starts the week on Sunday, convert at the boundary rather than storing a different
convention.

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **No date-specific overrides.** Public holidays, one-off closures and early-closing days cannot be expressed. | A tenant closing for Diwali has to edit the weekly grid and remember to put it back. |
| 2 | **Business hours do not gate bookings.** `available-slots` reads host availability only. | A customer can book a slot at a time the business is shut, if a host happens to be free. |
| 3 | **One schedule per tenant.** No per-location or per-purpose hours. | A two-branch business cannot express different hours per branch. |
| 4 | **Any tenant user can edit it.** There is no role check. | Same gap as the [users API](../user/users.md) — visibility equals edit rights. |
| 5 | **No seasonal schedules.** | Summer hours mean editing the grid twice a year. |

## Related

- [Availability Profiles](./availability_profile.md) — when a **person** is free; the model this one
  is deliberately not
- [Available Slots](./availability_slots.md) — what bookings actually consult today
- [Timezones](../onboard/timezones.md) — the `timezone` id comes from here

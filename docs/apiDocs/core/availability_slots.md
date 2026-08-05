# Available Slots API Documentation

Compute bookable time slots for an event template over a date range, scoped to the
authenticated tenant. The engine takes each eligible host's working hours and subtracts
existing bookings (including buffers), leave, and connected external-calendar busy times,
then applies the event's minimum-notice and horizon policy.

> **VB_AGENT access:** allowed. Agent tokens **must** send `X-Tenant-ID: <tenant uuid>`
> (**400** if omitted), and the `event_template` must belong to that tenant (**403** otherwise).
> The tenant is taken from the header and then checked against the template — never derived
> from the template the caller named.
> See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

## Endpoints Summary
- `GET /api/v1/core/availability/available-slots/` - List bookable slots

---

## Endpoint Details

### 1. Get Available Slots
- **Path**: `GET /api/v1/core/availability/available-slots/`
- **Headers**: `Authorization: Bearer <token>`
- **Query Parameters**:
  - `event_template` (required): Event template id. Must belong to the caller's tenant.
  - `start_utc` (required): Search window start, an ISO-8601 **instant** (e.g. `2026-08-08T04:00:00Z`). **Inclusive.** A value without an offset is read as UTC.
  - `end_utc` (required): Search window end, an ISO-8601 instant. **Exclusive**, so consecutive windows tile without overlapping. Must be after `start_utc` and within the template's `max_horizon_days`.
  - `target_host`: **Required for `DIRECT` templates** — slots are computed for that host, who must then be the one booked. Must be omitted for `COLLECTIVE` (`400` otherwise).

#### Why instants and not dates

A date is a different 24 hours in every timezone, so `date_from=2026-08-08` cannot be answered
without deciding *whose* August 8th it is. **Your client is the only component that knows which
timezone the user picked**, so it resolves the day and sends instants.

```js
// The user picked Aug 8 while viewing in `userZone`.
const startUtc = zonedTimeToUtc('2026-08-08T00:00:00', userZone);
const endUtc   = zonedTimeToUtc('2026-08-09T00:00:00', userZone);
```

Getting this wrong is not cosmetic. With a host in `Asia/Kolkata` and a customer viewing in
`America/New_York`, treating "Aug 8" as the host's day returns three slots that land on **Aug 7**
for the customer, and omits two that land on their Aug 8 afternoon. Converting the response for
display cannot repair it — the wrong slots were selected server-side.
- **Behaviour by strategy**:
  - `DIRECT`: a slot is returned when the named `target_host` is free; `eligible_host_ids` contains that host.
  - `COLLECTIVE`: a slot is returned only if **every** skilled host is free at that time.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Available slots retrieved successfully",
    "data": [
      {
        "start": "2026-07-05T14:00:00Z",
        "end": "2026-07-05T14:30:00Z",
        "eligible_host_ids": ["5fa85f64-5717-4562-b3fc-2c963f66afa7"]
      },
      {
        "start": "2026-07-05T14:30:00Z",
        "end": "2026-07-05T15:00:00Z",
        "eligible_host_ids": [
          "5fa85f64-5717-4562-b3fc-2c963f66afa7",
          "6ab85f64-5717-4562-b3fc-2c963f66afb8"
        ]
      }
    ],
    "errors": null
  }
  ```
- **Notes**:
  - `start` / `end` are always UTC ISO-8601 with a `Z` suffix. Convert to the user's timezone for display; book in UTC.
  - Times returned here are candidate `start_time` values for `POST /bookings/` or `POST /bookings/hold/`.
  - A slot may straddle midnight in any timezone — hosts can define overnight shifts (`22:00 → 06:00`). Do not assume `start` and `end` share a calendar date.
  - Paging is safe: because `end_utc` is exclusive, querying consecutive windows never duplicates or drops a slot.
- **Error Responses**:
  - `403 Forbidden`: the event template belongs to another tenant.
    ```json
    { "status": false, "message": "Event template does not belong to your tenant.", "data": null, "errors": null }
    ```
  - `400 Bad Request`: `end_utc` on/before `start_utc`, or the range exceeds the event horizon.

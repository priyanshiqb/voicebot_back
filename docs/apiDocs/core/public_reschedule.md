# Public Reschedule API Documentation

The **unauthenticated, token-gated** page a customer lands on when their booking has to move.

When a host takes emergency leave, the reschedule engine flags their affected bookings as
`REQUIRES_RESCHEDULE` and emails each customer a link containing a single-use token. These two
endpoints are what that link talks to: `GET` shows the original booking plus fresh times, `POST`
commits the new one.

> **No authentication.** The token *is* the credential. It is single-use, expiring, and scoped to
> exactly one booking — so treat the URL as a secret and never log it.

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/core/public/reschedule/{token}/` | Show the booking being moved and the times available |
| `POST` | `/api/v1/core/public/reschedule/{token}/` | Rebook into a chosen time and consume the token |

**Throttled** at 30 requests/minute per IP (anonymous scope). Exceeding it returns `429`.

> **Response shape differs from the rest of the API.** These endpoints predate the shared envelope
> helper: success bodies carry `status`, `data`, and (on `POST`) `message`, but **no `errors` key
> and no `request_id`**. Error bodies carry `status` and `message`, and only the payload-validation
> case adds `errors`.

---

## 1. Get Reschedule Options

**`GET /api/v1/core/public/reschedule/{token}/`**

Returns the booking that needs moving, plus bookable slots for the same event type and host.

Safe to call repeatedly — it does **not** consume the token.

### Path parameters

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `token` * | UUID | The single-use token from the reschedule email. | Must match a booking still in `REQUIRES_RESCHEDULE` and not past its expiry. |

### Query parameters

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `start_utc` | ISO-8601 datetime | Start of the slot search window. | Defaults to **now**. Clamped so it is never earlier than now. A value with no offset is read as UTC. Unparseable values fall back to the default rather than erroring. |
| `end_utc` | ISO-8601 datetime | End of the window, exclusive. | Defaults to **now + the event's `max_horizon_days`**, and is clamped to that ceiling. If it ends up on or before `start_utc`, one day is used. |

These are **instants, not dates**, for the same reason as the authenticated slots endpoint: a date
is a different 24 hours in every timezone, and only the customer's browser knows which one they
mean. Resolve their chosen day locally, then send instants.

### Response — `200 OK`

```json
{
  "status": true,
  "data": {
    "booking": {
      "id": "9c1e85f64-5717-4562-b3fc-2c963f66af22",
      "title": "Intro call with Jane",
      "original_start_time": "2026-07-05T14:00:00Z",
      "event_type": "Intro Call",
      "duration_minutes": 30
    },
    "slots": [
      { "start": "2026-07-06T14:00:00Z", "end": "2026-07-06T14:30:00Z" },
      { "start": "2026-07-06T14:30:00Z", "end": "2026-07-06T15:00:00Z" }
    ]
  }
}
```

| Field | Type | Meaning |
|---|---|---|
| `data.booking.id` | UUID | The booking being replaced. |
| `data.booking.title` | string | Shown so the customer recognises which meeting this is. |
| `data.booking.original_start_time` | ISO-8601 UTC | The time that no longer works. Render it in the customer's zone so they can see what changed. |
| `data.booking.event_type` | string | The event template's name. |
| `data.booking.duration_minutes` | integer | Length of the meeting; every slot below is this long. |
| `data.slots[]` | array | Bookable alternatives, ascending. `start`/`end` are UTC. **May be empty.** |

**All times are UTC.** Unlike the authenticated slots endpoint, this response carries no `timezone`
field and no pre-rendered local values — the customer's zone is unknown to the server. Convert
client-side using the browser's zone.

#### An empty `slots` array

`200` with `"slots": []` means nothing is free in the window — widen it, or the host genuinely has
no availability before the horizon. It is not an error, and the token is still valid.

Note the slots are scoped to **the host who will actually be rebooked** (for collective events, to
the whole group). That is deliberate: offering the union across hosts would show times that then
`409` on submit.

### Error responses

| Status | When | `message` |
|---|---|---|
| **400** | Token unknown, already used, or its booking is no longer `REQUIRES_RESCHEDULE` | `This reschedule link is invalid or has already been used.` |
| **400** | Token past `reschedule_token_expires_at` | `This reschedule link has expired.` |
| **400** | Empty token segment | `Invalid reschedule link.` |
| **429** | More than 30 requests/minute from this IP | throttle detail |

```json
{ "status": false, "message": "This reschedule link has expired." }
```

> All three token failures return the **same status and shape**, so a caller cannot distinguish
> "never existed" from "already used" — that is intentional, and stops the endpoint being used to
> probe for valid tokens.

---

## 2. Rebook

**`POST /api/v1/core/public/reschedule/{token}/`**

Books the chosen time, cancels the original, and **consumes the token**. This is the only
destructive call in the pair.

### Request body

| Field | Type | Description | Validation |
|---|---|---|---|
| `start_time` * | ISO-8601 datetime | The chosen slot's `start`, copied from the `GET` response. | Must parse as a datetime. Send it with an offset (`Z`); a naive value is read as UTC. Must still be free, inside the host's working hours, and satisfy the event's notice and horizon policy. |

```json
{ "start_time": "2026-07-06T14:00:00Z" }
```

### Response — `201 Created`

```json
{
  "status": true,
  "message": "Your booking has been rescheduled.",
  "data": { "booking_id": "4dd85f64-5717-4562-b3fc-2c963f66af33" }
}
```

`booking_id` is the **new** booking. The original is cancelled as part of the same transaction. For
a collective event the whole group is rebooked and this is the first member's id.

### Error responses

| Status | When | `message` | Retryable? |
|---|---|---|---|
| **400** | `start_time` missing or unparseable | `A valid start_time is required.` (with an `errors` object) | fix and resend |
| **400** | Token invalid, used, or expired | as in `GET` | no — the link is spent |
| **400** | Policy violation — inside the notice window, past the horizon, or in the past | the specific reason, e.g. `This time is too soon to book.` | pick another slot |
| **400** | The original host is no longer configured for this event type | `The requested host is not configured to host this event type.` | no — needs an admin |
| **409** | The slot was taken between `GET` and `POST`, **or** the time falls outside the host's working hours | `The selected time is no longer available.` | **yes** — re-run `GET` and offer fresh times |
| **429** | Throttled | throttle detail | yes, after backoff |
| **503** | A host's external calendar could not be read, so the booking could not be verified | `Could not verify the host's calendar availability. Please try again.` | **yes** — transient |

The `409` is the one to design for. Slots are advisory, not reservations, and a customer who leaves
the page open for ten minutes will hit it. Re-fetch and re-offer rather than surfacing an error.

The `503` is deliberate: rather than book against a calendar it could not read and risk
double-booking the host, the system refuses. Treat it as "try again shortly", not a failure.

```json
{ "status": false, "message": "The selected time is no longer available." }
```

Payload-validation errors are the one case with an `errors` object:

```json
{
  "status": false,
  "message": "A valid start_time is required.",
  "errors": { "start_time": ["Datetime has wrong format..."] }
}
```

---

## Token lifecycle

| Stage | Token state |
|---|---|
| Host takes leave → booking flagged `REQUIRES_RESCHEDULE` | minted, emailed |
| Customer opens the link, `GET` (any number of times) | still valid |
| Customer submits, `POST` succeeds | **consumed** — every later call is `400` |
| `POST` fails (409/503/policy) | still valid — the customer can retry |
| `BOOKING_RESCHEDULE_TOKEN_TTL_HOURS` elapses (default 72) | expired — `400` |

A collective booking's members deliberately **share one token**, so a single customer action moves
the whole group.

## Implementation notes

- The original slot stays blocked while the booking sits in `REQUIRES_RESCHEDULE`, so the customer
  cannot pick their original time back even if the leave was later cancelled. Self-consistent — the
  engine will not offer it either.
- The rebook runs in one transaction with a row lock on the token's booking, so two simultaneous
  submissions cannot both succeed.

## Possible improvement

This response returns bare UTC with no timezone context, while the authenticated
[slots endpoint](./availability_slots.md) now returns `timezone` plus `start_local`/`end_local`.
Adding a `timezone` query parameter here would let the customer's browser state its zone and get
pre-rendered local times back, matching the rest of the API. Not currently implemented.

## Related

- [Available Slots API](./availability_slots.md)
- [Booking API](./booking.md)
- [Leave → reschedule flow](../../flows/03-leave-and-reschedule.md)

# Booking API Documentation

Creates and manages appointments — the write side of the scheduling module.

Every mutation goes through a service layer that enforces the rules (working hours, buffers,
notice, horizon, host eligibility) and fires the side effects (confirmation email with `.ics`,
external-calendar mirroring, audit log). Because of that, **`PUT` and `PATCH` are disabled**: a
booking's fields are never edited directly, only moved through defined transitions.

**Double-booking is impossible.** A Postgres exclusion constraint rejects any overlap for the same
host in the same tenant, buffers included, so a slot lost to a race returns **409** rather than
producing two bookings.

**Statuses:** `HELD` · `SCHEDULED` · `CONFIRMED` · `COMPLETED` · `CANCELED` · `NO_SHOW` ·
`REQUIRES_RESCHEDULE` · `RESCHEDULED`

> **VB_AGENT access:** every booking action, including `DELETE` (which cancels). The one exception
> is [`daily-count`](#11-date-wise-booking-count), which is reporting rather than booking. Agent tokens
> **must** send `X-Tenant-ID: <tenant uuid>`: they have no tenant mapping and a booking has to land
> somewhere. Omitting it is **400**; a booking in another tenant is **404**.
> See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/core/bookings/` | List bookings (paginated) |
| `GET` | `/api/v1/core/bookings/{id}/` | Retrieve one booking in full |
| `POST` | `/api/v1/core/bookings/` | Book a slot outright |
| `POST` | `/api/v1/core/bookings/hold/` | Reserve a slot temporarily while details are confirmed |
| `POST` | `/api/v1/core/bookings/{id}/confirm/` | Turn a hold into a real booking |
| `POST` | `/api/v1/core/bookings/{id}/cancel/` | Cancel, enforcing the notice window |
| `POST` | `/api/v1/core/bookings/{id}/complete/` | Mark the meeting as having happened |
| `POST` | `/api/v1/core/bookings/{id}/no-show/` | Mark the customer as absent |
| `DELETE` | `/api/v1/core/bookings/{id}/` | Cancel, **bypassing** the notice window |
| `GET` | `/api/v1/core/bookings/daily-count/` | Date-wise booking counts over a range (max 61 days) |
| `GET` | `/api/v1/core/bookings/dropdown/` | Compact list (see caveat) |
| `POST` | `/api/v1/core/bookings/bulk_delete/` | Raw soft-delete — **no notifications** (see caveat) |

---

## Shared error semantics

Every action routes service errors through one mapper, so the same failure means the same thing
everywhere:

| Exception | Status | Meaning for the caller |
|---|---|---|
| `PolicyViolationError` | **400** | The request breaks a configured rule (past, notice, horizon, cancellation window). `errors` is keyed by the offending field, usually `start_time`. |
| `NoEligibleHostError` | **400** | Nobody can host this — configuration, not timing. Retrying the same request will fail again. |
| `InvalidTransitionError` | **400** | The booking is not in a state where this action makes sense. |
| `SlotConflictError` | **409** | Someone else took the time, or the hold lapsed. **Retryable** after fetching fresh slots. |
| `ExternalCalendarUnavailable` | **503** | A host's external calendar could not be read, so the system refused to book unverified rather than risk a double-booking. **Retryable** shortly. |

The `409`/`503` distinction matters when writing a client: `409` means *pick another time*, `503`
means *try the same time again in a moment*.

---

## 1. List Bookings

**`GET /api/v1/core/bookings/`** — paginated, tenant-scoped, newest start time first.

### Query parameters

| Parameter | Type | Description |
|---|---|---|
| `page`, `page_size` | integer | Pagination. |
| `status` | enum | Filter by booking status. |
| `primary_host` | UUID | Only bookings hosted by this user. |
| `event_template` | UUID | Only bookings of this meeting type. |
| `source` | enum | Where it came from: `CUSTOMER`, `INTERNAL`, `PUBLIC_LINK`, `AI`. |
| `ordering` | string | `start_time` or `created_at`; prefix `-` to reverse. Default `-start_time`. |
| `date` | date | Bookings on this **local** date. `YYYY-MM-DD`. |
| `start_date` | date | Local date to start from, **inclusive**. |
| `end_date` | date | Local date to end at, **inclusive**. |
| `start_time_after` | datetime | Starts at or after this **UTC instant**, ISO-8601. |
| `start_time_before` | datetime | Starts strictly **before** this UTC instant, so consecutive windows tile. |
| `timezone` | string | IANA zone the three date filters are read in. Defaults to the caller's, then the tenant's, then UTC. Unknown zone is a **400**. |

All filters combine with `AND`, so `?date=2026-08-05&status=CONFIRMED&primary_host=<id>` is one host's
confirmed bookings that day.

### Dates or instants — pick the one that matches the question

**Local dates** (`date`, `start_date`, `end_date`) answer *"show me Tuesday"*. The day a booking
belongs to depends on where you stand — a `20:00 UTC` appointment is **the next day** in Kolkata —
so these are resolved in `timezone`, and a client never has to do timezone maths to render a day
view.

**UTC instants** (`start_time_after`, `start_time_before`) answer *"show me 14:00–17:00"*, for
sub-day windows. `_before` is exclusive so adjacent windows tile without double-counting.

```
GET /api/v1/core/bookings/?date=2026-08-05&timezone=Asia/Kolkata
GET /api/v1/core/bookings/?start_date=2026-08-01&end_date=2026-08-31&timezone=Asia/Kolkata
GET /api/v1/core/bookings/?start_time_after=2026-08-05T09:00:00Z&start_time_before=2026-08-05T12:00:00Z
```

> **These are the same dates [daily-count](#11-date-wise-booking-count) uses.** Both go through one
> code path, so a day showing `count: 4` returns exactly those 4 rows when you drill into it with
> `?date=` and the same `timezone`. Send the same zone to both or the two will legitimately disagree.

Unlike daily-count there is **no 61-day cap** here — that limit exists because the count response is
zero-filled, and this one is paginated.

### Response — `200 OK`

```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "id": "4cc85f64-5717-4562-b3fc-2c963f66af30",
      "event_template": { "id": "3fa8…", "name": "30-Min Demo", "color": "#000000" },
      "title": "30-Min Demo",
      "primary_host": { "id": "5fa8…", "email": "host@acme.com", "first_name": "Alex", "last_name": "Host" },
      "start_time": "2026-07-05T14:00:00Z",
      "end_time": "2026-07-05T14:30:00Z",
      "status": "SCHEDULED",
      "meeting_url": "https://meet.google.com/abc-defg-hij",
      "created_at": "2026-07-01T10:00:00Z"
    }
  ],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

All times are **UTC**. `start_time` and `end_time` are not guaranteed to share a calendar date —
hosts can define overnight shifts, so a booking may legitimately straddle midnight.

---

## 2. Retrieve Booking

**`GET /api/v1/core/bookings/{id}/`** — the full record, including participants and audit fields.

| Field | Meaning |
|---|---|
| `start_time` / `end_time` | Customer-facing times, UTC. |
| `status` | See the status list above. |
| `source` | Which channel created it. |
| `hold_expires_at` | Set only while `HELD`; the instant the reservation lapses. |
| `reschedule_token` | Present only once the booking is flagged `REQUIRES_RESCHEDULE`. Members of a collective group **share one token**. |
| `external_event_id` / `external_provider` | The mirrored calendar event, if the host has one connected. |
| `meeting_url` | Join URL for video meetings (e.g., Google Meet). Generated when mirrored to the external calendar. |
| `group_id` | Non-null for collective bookings; identifies the sibling rows booked together. |
| `cancellation_reason` | Free text captured at cancellation. |
| `participants[]` | Host and attendees. `role` is `HOST`, `ATTENDEE`, or `GUEST`. |

Buffers are **not** exposed: `effective_start_time`/`effective_end_time` are internal to overlap
detection. What you see is what the customer sees.

| Status | When |
|---|---|
| **200** | Found. |
| **403** | Caller's account has no tenant mapping. |
| **404** | No such booking **in your tenant** — identical to an id that exists nowhere. |

---

## 3. Create Booking

**`POST /api/v1/core/bookings/`**

Books a slot immediately. Use this when the customer has already committed; use
[Hold](#4-hold-a-slot) when they still need to confirm details on a call.

### Request body

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `event_template` * | UUID | The meeting type being booked. Determines duration, buffers, and every policy applied below. | Must exist, **be active**, and belong to your tenant. A deactivated template is rejected — the same rule the AI bridge applies, so both agree on what is bookable. |
| `start_time` * | ISO-8601 datetime | When the meeting starts. Copy a `start` from [available-slots](./availability_slots.md). | Must parse; naive values are read as UTC. Must not be in the past, must satisfy `min_notice_minutes`, must be within `max_horizon_days`, and must fall inside the host's working hours. |
| `target_host` * | UUID | The person who will host. **Required for `DIRECT`; must be omitted for `COLLECTIVE`.** | Must have an active `UserEventSkill` for this template. Nothing is ever auto-assigned — a booking always names its host. |
| `participants` * | array | The people attending. The host is added automatically; list only the customer side. | At least 1, at most **25**. |

#### `participants[]` object

| Field | Type | Description | Validation |
|---|---|---|---|
| `email` * | email | Where the confirmation and `.ics` go. | Must be a valid address. |
| `name` | string | Display name on the invite. | Max 255 chars; may be blank. |
| `phone_number` | string | Used to link the attendee to an existing Contact when one matches. | Max 20 chars; may be blank. |
| `role` | enum | `ATTENDEE` (default) or `GUEST`. | `HOST` is not accepted — the host is derived from `target_host`. |

```json
{
  "event_template": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "start_time": "2026-07-05T14:00:00Z",
  "target_host": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
  "participants": [
    { "name": "Jamie Customer", "email": "jamie@example.com", "phone_number": "+15550001111" }
  ]
}
```

### Success — `201 Created`

**`DIRECT`** → one booking object, `message: "Booked successfully"`.

**`COLLECTIVE`** → an **array**, one booking per skilled host, all sharing a `group_id`,
`message: "Collective booking created"`. All-or-nothing: if any host is busy, none are booked.

```json
{
  "status": true,
  "message": "Collective booking created",
  "data": [
    { "id": "b1", "primary_host": "host-1", "group_id": "g-123", "status": "SCHEDULED" },
    { "id": "b2", "primary_host": "host-2", "group_id": "g-123", "status": "SCHEDULED" }
  ],
  "errors": null
}
```

### Error responses

| Status | Cause | `message` | `errors` |
|---|---|---|---|
| **400** | Start time already passed | `Cannot book a slot in the past.` | `{"start_time": [...]}` |
| **400** | Inside the notice window | `This event requires at least 120 minutes notice.` | `{"start_time": [...]}` |
| **400** | Beyond the booking horizon | `This event cannot be booked more than 60 days in advance.` | `{"start_time": [...]}` |
| **400** | `DIRECT` without a host | `This event type requires a specific host.` | `{"target_host": [...]}` |
| **400** | `COLLECTIVE` **with** a host | `Collective event types book every skilled host.` | `{"target_host": [...]}` |
| **400** | Host not configured for this template | `This host is not configured to host this event type.` | `{"target_host": [...]}` |
| **400** | Template inactive or in another tenant | `Invalid pk …` / `Event template does not belong to your tenant.` | `{"event_template": [...]}` |
| **400** | No participants, or more than 25 | `At least one participant is required.` / `A booking may have at most 25 participants.` | `{"participants": [...]}` |
| **400** | Agent token without `X-Tenant-ID` | `This header is required for agent tokens.` | `{"X-Tenant-ID": [...]}` |
| **409** | Slot taken, **or** outside the host's working hours | `The selected time is no longer available.` | `null` |
| **409** | Collective, and at least one host is busy | `Not all required hosts are available at this time.` | `null` |
| **503** | A host's external calendar was unreachable | `Could not verify the host's calendar availability. Please try again.` | `null` |

> **Why "outside working hours" is a 409 and not a 400.** The engine answers one question — *is
> anyone free for this exact time?* — and a time outside the shift is simply not free. Both cases
> resolve the same way: fetch fresh slots and pick another.

---

## 4. Hold a Slot

**`POST /api/v1/core/bookings/hold/`**

Reserves a slot for a short window so nobody else can take it while you collect details — the
natural first step on a live phone call.

- **Body**: identical to Create.
- **Not available for `COLLECTIVE`** templates; book those in one step.
- The hold **blocks the slot for everyone**, exactly like a real booking.
- `hold_expires_at` is set from `BOOKING_HOLD_TTL_SECONDS` (default **300s**). A background job
  releases lapsed holds every minute; until then the slot stays blocked.

| Status | When | `message` |
|---|---|---|
| **201** | Held. `status: "HELD"`, `hold_expires_at` set. | `Slot held successfully` |
| **400 / 409 / 503** | Same causes as Create. | — |

**Always follow with `confirm` or `cancel`.** An abandoned hold blocks its slot until it expires.

---

## 5. Confirm a Hold

**`POST /api/v1/core/bookings/{id}/confirm/`** — no body.

Promotes `HELD` → `CONFIRMED` and clears `hold_expires_at`. This is when the confirmation email
and calendar mirroring fire.

| Status | When | `message` |
|---|---|---|
| **200** | Confirmed. | `Booking confirmed` |
| **400** | Not currently held. | `Only held bookings can be confirmed (current status: SCHEDULED).` |
| **409** | The hold already lapsed and the slot was released. | `This hold has expired.` |
| **404** | No such booking in your tenant. | — |

A `409` here means the reservation is gone — re-check availability before promising the customer
anything.

---

## 6. Cancel a Booking

**`POST /api/v1/core/bookings/{id}/cancel/`**

Customer-facing cancellation. **Enforces the template's `cancellation_lead_minutes`.**

| Field | Type | Description | Validation |
|---|---|---|---|
| `reason` | string | Free-text note stored on the booking and included in the notification. | Optional; blank allowed. |

| Status | When | `message` |
|---|---|---|
| **200** | Cancelled; notification sent and any mirrored calendar event removed. | `Booking canceled` |
| **200** | A collective booking — **the whole group** is cancelled. | `Collective booking canceled` |
| **400** | Inside the notice window. | `This booking can no longer be canceled (requires 60 minutes notice).` |
| **400** | Already in a terminal state. | `Booking is already CANCELED and cannot be canceled.` |
| **404** | No such booking in your tenant. | — |

Cancelling **any** member of a collective group cancels every sibling — the meeting cannot
half-exist. Use `DELETE` when an operator needs to override the notice window.

---

## 7. Complete / No-Show

**`POST /api/v1/core/bookings/{id}/complete/`** → `COMPLETED`, `message: "Booking completed"`
**`POST /api/v1/core/bookings/{id}/no-show/`** → `NO_SHOW`, `message: "Booking marked as no-show"`

Both are outcome records for a meeting whose time has passed. Neither sends a notification.

| Status | When | `message` |
|---|---|---|
| **200** | Transition applied. | as above |
| **400** | Illegal transition from the current state. | `Cannot mark a CANCELED booking as COMPLETED.` |
| **404** | No such booking in your tenant. | — |

---

## 8. Delete (Force Cancel)

**`DELETE /api/v1/core/bookings/{id}/`**

Despite the verb, this **cancels** — the row is retained, notifications are sent, and mirrored
calendar events are removed. The difference from `cancel/` is that it **bypasses the notice
window**, for operators acting on the customer's behalf.

| Status | When | `message` |
|---|---|---|
| **200** | Cancelled (whole group if collective). | `Deleted successfully` |
| **404** | No such booking in your tenant. | — |

---

## 9. Dropdown

**`GET /api/v1/core/bookings/dropdown/`**

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `limit` | integer | Max rows. | Above 1000 is a **400** (`Limit cannot exceed 1000`), not a silent cap. |
| filters | — | Same as List. | — |

> **Caveat:** this viewset defines no dropdown serializer, so the response is **full booking rows**
> (List shape), not `{label, value}` pairs. Do not assume the compact shape other resources use.

---

## 10. Bulk Delete

**`POST /api/v1/core/bookings/bulk_delete/`**

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Bookings to soft-delete. | Non-empty list; every id must exist, be undeleted, and be in your tenant. All-or-nothing. |

> **⚠️ This is a raw soft-delete, not a cancellation.** It bypasses the booking service entirely,
> so it sends **no cancellation emails** and leaves **mirrored calendar events in place** on the
> hosts' external calendars. The customer is never told, and the host's calendar still shows the
> meeting.
>
> Use `POST /{id}/cancel/` or `DELETE /{id}/` for anything customer-facing. Reserve this for
> administrative cleanup of test or junk data.

| Status | When | `message` |
|---|---|---|
| **200** | All soft-deleted. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | Any id unknown, already deleted, or in another tenant; **nothing deleted**. | `Some ids are invalid` |

---

## 11. Date-wise Booking Count

**`GET /api/v1/core/bookings/daily-count/`** — how many bookings fall on each date in a range. One
number per day, for a calendar heat map or a volume chart.

Counts by **`start_time`** — when the appointment happens, not when the row was created.

### Query parameters

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `start_date` * | date | First date to count, **inclusive**. `YYYY-MM-DD`. | Required. |
| `end_date` * | date | Last date to count, **inclusive**. | Required. On or after `start_date`, and at most **61 days** of range. |
| `timezone` | string | IANA zone the dates are resolved in, e.g. `Asia/Kolkata`. | Must be a real zone — an unknown one is a **400**. |
| `status`, `primary_host`, `event_template`, `source` | — | Same filters as [List](#1-list-bookings). | — |

Both bounds are **inclusive**, unlike the instant bounds on
[available-slots](./availability_slots.md) — these are dates a human picked on a calendar, and an
exclusive `end_date` would drop the day they clicked. So `2026-08-01 → 2026-08-31` is 31 days, and
the widest legal range is 61 days (`start_date + 60`).

### Response — `200 OK`

```json
{
  "status": true,
  "message": "Booking counts retrieved successfully",
  "data": {
    "start_date": "2026-08-01",
    "end_date": "2026-08-05",
    "timezone": "Asia/Kolkata",
    "total": 9,
    "days": [
      { "date": "2026-08-01", "count": 4 },
      { "date": "2026-08-02", "count": 0 },
      { "date": "2026-08-03", "count": 2 },
      { "date": "2026-08-04", "count": 0 },
      { "date": "2026-08-05", "count": 3 }
    ]
  },
  "errors": null
}
```

**Every date in the range is returned, empty ones as `count: 0`.** Leaving the client to fill the
gaps is how a chart ends up with unequal buckets. This is also why the range is capped: the payload
size is the length of the range, not the number of bookings.

### `timezone` changes the numbers

This is the one parameter to get right. `start_time` is a UTC instant, and which calendar date it
belongs to depends on where you stand — a `20:00 UTC` appointment is **the next day** in Kolkata.
Reading counts in UTC for a tenant in IST silently moves every evening booking to the day before,
and nothing in the response looks wrong.

Resolution order when you omit it:

1. The caller's own `timezone` (from their user profile)
2. The tenant's base timezone
3. UTC

The resolved zone is always echoed back in `timezone`, so a client can label the chart without a
second request. An unrecognised zone is rejected rather than falling back — a silent fallback would
return a well-formed set of wrong numbers.

### Scope

Exactly the same rows [List](#1-list-bookings) would return: your tenant, excluding soft-deleted.

**All statuses are counted** unless you narrow them, so `CANCELED` and lapsed `HELD` rows are in the
total. Pass `?status=CONFIRMED` for confirmed bookings only. Pass `?primary_host=<user id>` for one
host's calendar rather than the whole tenant's.

To fetch the bookings behind a day's number, call [List](#1-list-bookings) with `?date=<that date>`
and **the same `timezone`**. Both endpoints resolve dates through one shared code path, so the
results match exactly.

| Status | When | `message` / `errors` |
|---|---|---|
| **200** | Counts returned. | `Booking counts retrieved successfully` |
| **400** | `start_date` or `end_date` missing or malformed. | Field errors under `errors` |
| **400** | `end_date` before `start_date`. | `end_date must be on or after start_date.` |
| **400** | Range wider than 61 days. | `Range covers 92 days; the maximum is 61.` |
| **400** | Unknown `timezone`. | `'Mars/Olympus' is not a valid IANA timezone identifier.` |
| **403** | Caller has no tenant mapping, **or** is a `VB_AGENT` token — see below. | — |

> **VB_AGENT callers are refused here.** This is the one action on this viewset they cannot reach.
> Reporting is not the voicebot's job; it books appointments rather than reads statistics about them.

---

## Side effects

| Action | Email + `.ics` | External calendar |
|---|---|---|
| Create / Confirm | confirmation to every participant\* | event created on the host's calendar |
| Cancel / `DELETE` | cancellation notice\* | event removed |
| Bulk delete | **none** | **left in place** |

\* Only if the tenant has configured a booking email sender — see below. Calendar sync is unaffected
either way.

Notifications are queued after the transaction commits, so a rolled-back booking never emails
anyone. Each logical notification is sent **at most once**, enforced by a unique dedup key — a
retry cannot produce a duplicate email. The `.ics` `SEQUENCE` increments on every revision, so
calendar clients accept updates rather than ignoring them.

> ### Email requires the tenant to have configured a sender
>
> **Booking email sends through the tenant's own SMTP server, or not at all.** There is no fallback to
> the platform sender. A tenant with no verified, assigned configuration gets **none** of the emails
> in the table above — confirmations, cancellations, reminders and reschedule requests are all
> silently absent from the customer's inbox.
>
> Everything else still works: the booking is created, the status transitions happen, the calendar
> event is mirrored, push notifications fire, and every endpoint returns its normal 2xx. Only the mail
> stops.
>
> Each unsent message leaves a `NotificationLog` row with `status: FAILED` and an `error` saying why,
> so this is diagnosable rather than mysterious — and because `FAILED` rows are re-enqueueable,
> configuring a sender later lets the same logical notification go out rather than being lost.
>
> Set it up: [Email Configuration](../tenant/email_configuration.md) to create and verify a
> configuration, then [Tenant Settings](../tenant/settings.md) to assign it. Check
> `booking_email.will_send` to confirm mail is actually flowing.
>
> **If a customer reports a missing confirmation, check that first** — before the notification log,
> before the worker, before the provider.

### Notification history and retention

Every attempt writes a `NotificationLog` row: type, recipient, status, `sent_at`, the failure reason,
and **which SMTP configuration carried it** (`email_configuration`) — the field to check when
deliverability changes after a provider switch.

Message bodies are deliberately **not** stored. They add nothing a `notification_type` does not
already say, and for authentication mail they would be a credential store nobody meant to build.

Rows are purged daily by `core.tasks.purge_notification_logs`:

| Rows | Kept for | Setting |
|---|---|---|
| Sent / pending | **90 days** | `NOTIFICATION_LOG_RETENTION_DAYS` |
| Failed | **180 days** — the only trace of a message that never arrived | `NOTIFICATION_LOG_FAILED_RETENTION_DAYS` |

> **Rows for bookings that have not happened yet are never purged, at any age.** The row *is* the
> idempotency mechanism, not a record of it — deleting one frees its `dedup_key` and re-arms that
> notification. A confirmation written today for a booking eleven months out has to outlive the
> retention window, or the customer gets a second copy.

## Common scenarios

| Goal | Sequence |
|---|---|
| Straightforward booking | `GET available-slots` → `POST /bookings/` |
| Live phone call | `GET available-slots` → `POST /hold/` → collect details → `POST /{id}/confirm/` |
| Customer changes their mind mid-call | `POST /{id}/cancel/` on the hold, then hold the new time |
| Slot lost to a race (`409`) | Re-fetch slots and offer alternatives — never retry blindly |
| Provider outage (`503`) | Retry the same request after a short delay |
| Operator cancelling last-minute | `DELETE /{id}/` (skips the notice window) |

## Related

- [Available Slots API](./availability_slots.md) — find bookable times first
- [Public Reschedule API](./public_reschedule.md) — the customer-facing move flow
- [Booking lifecycle flow](../../flows/02-booking-lifecycle.md)

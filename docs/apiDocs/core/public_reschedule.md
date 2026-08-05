# Public Reschedule API Documentation

**Public, unauthenticated** endpoints that let a customer reschedule a booking that was
flagged `REQUIRES_RESCHEDULE` (e.g. after a host logged emergency leave). Access is via the
single-use `reschedule_token` embedded in the "please reschedule" email link. The token is
**single-use, expiring** (default 72h), and the endpoint is **rate-limited (30/min)**.

> No `Authorization` header. The tenant and booking are derived from the token. `{token}`
> is a UUID; a malformed token yields `404`, an unknown/expired/used token yields `400`.

## Endpoints Summary
- `GET /api/v1/core/public/reschedule/{token}/` - Booking summary + available slots
- `POST /api/v1/core/public/reschedule/{token}/` - Book a new slot (consumes the token)

---

## Endpoint Details

### 1. Get Reschedule Options
- **Path**: `GET /api/v1/core/public/reschedule/{token}/`
- **Query Parameters** (optional):
  - `start_utc`, `end_utc`: ISO-8601 **instants** bounding the slot search. Defaults to now
    through the event's horizon, and both are clamped to that range. Resolve the customer's
    chosen day in the timezone their browser is displaying — a date means a different 24 hours
    in every zone. A value without an offset is read as UTC.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "data": {
      "booking": {
        "id": "4cc85f64-5717-4562-b3fc-2c963f66af30",
        "title": "30-Min Demo",
        "original_start_time": "2026-07-05T14:00:00Z",
        "event_type": "30-Min Demo",
        "duration_minutes": 30
      },
      "slots": [
        { "start": "2026-07-08T15:00:00Z", "end": "2026-07-08T15:30:00Z" },
        { "start": "2026-07-08T15:30:00Z", "end": "2026-07-08T16:00:00Z" }
      ]
    }
  }
  ```
- **Error Response (400 Bad Request)** — invalid / used / expired token:
  ```json
  { "status": false, "message": "This reschedule link is invalid or has already been used." }
  ```

---

### 2. Rebook (Confirm New Time)
- **Path**: `POST /api/v1/core/public/reschedule/{token}/`
- **Request Payload**:
  ```json
  { "start_time": "2026-07-08T15:00:00Z" }
  ```
- **Behaviour**: Atomically books the chosen slot as a **new** booking, marks the old
  booking(s) `RESCHEDULED`, and **consumes the token** (single-use). For a `COLLECTIVE`
  meeting the whole group is rebooked; for `DIRECT` the original host is targeted.
- **Success Response (201 Created)**:
  ```json
  {
    "status": true,
    "message": "Your booking has been rescheduled.",
    "data": { "booking_id": "7dd85f64-5717-4562-b3fc-2c963f66af40" }
  }
  ```
- **Error Responses**:
  - `400 Bad Request` — missing/invalid `start_time`, or token invalid/expired/used (including
    a second attempt after the token was consumed), or a policy violation
    (past / min-notice / horizon).
  - `409 Conflict` — the chosen slot was taken meanwhile:
    ```json
    { "status": false, "message": "The selected time is no longer available." }
    ```
    → re-`GET` the endpoint for fresh slots and retry.

---

> This flow is reached from [Leave & Reschedule](../../flows/03-leave-and-reschedule.md),
> Outcome B (booking flagged for customer rescheduling).

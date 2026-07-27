# Available Slots API Documentation

Compute bookable time slots for an event template over a date range, scoped to the
authenticated tenant. The engine takes each eligible host's working hours and subtracts
existing bookings (including buffers), leave, and connected external-calendar busy times,
then applies the event's minimum-notice and horizon policy.

## Endpoints Summary
- `GET /api/v1/core/availability/available-slots/` - List bookable slots

---

## Endpoint Details

### 1. Get Available Slots
- **Path**: `GET /api/v1/core/availability/available-slots/`
- **Headers**: `Authorization: Bearer <token>`
- **Query Parameters**:
  - `event_template` (required): Event template id. Must belong to the caller's tenant.
  - `date_from` (required): Start date `YYYY-MM-DD` (inclusive).
  - `date_to` (required): End date `YYYY-MM-DD` (inclusive). Must be on/after `date_from` and within the template's `max_horizon_days`.
  - `target_host` (optional): Restrict to a single host (required behaviour for `DIRECT` templates).
- **Behaviour by strategy**:
  - `ROUND_ROBIN` / `DIRECT`: a slot is returned if at least one eligible host is free; `eligible_host_ids` lists who.
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
  - All times are UTC-aware. Convert to the customer's timezone for display; book in UTC.
  - Times returned here are candidate `start_time` values for `POST /bookings/` or `POST /bookings/hold/`.
- **Error Responses**:
  - `403 Forbidden`: the event template belongs to another tenant.
    ```json
    { "status": false, "message": "Event template does not belong to your tenant.", "data": null, "errors": null }
    ```
  - `400 Bad Request`: `date_to` before `date_from`, or the range exceeds the event horizon.

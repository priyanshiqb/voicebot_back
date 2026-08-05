# Booking module — hardening register

**Status:** ✅ **all findings fixed** (699 tests pass on Postgres). Each section below keeps its
original diagnosis under a "Fixed" note, so the reasoning survives even though the code has
moved on. Two proposed DB constraints were **withdrawn during implementation** as not
expressible — see *Cross-cutting 1*. The only item still open is *Cross-cutting 4* (the
`audit_bookings` invariant command), which is additive rather than a fix.

Companion to
[booking-flow-internals.md](../flows/booking-flow-internals.md) (which describes current
behaviour) and
[overnight-availability-utc-materialization-plan.md](overnight-availability-utc-materialization-plan.md)
(which changes availability).

Everything below is traced to a line and, where marked **verified**, was reproduced or
confirmed against the code/environment rather than inferred. Ordered by what can hurt you
soonest.

Now is the cheap moment for the schema-level items: with no production data, a constraint is a
migration instead of a migration plus a data-cleanup plus a coordination window.

---

## Severity key

| | Meaning |
|---|---|
| **P0** | Can return 5xx, lose data, or double-book. Fix before launch. |
| **P1** | Silently wrong — no error surfaces, the damage is discovered later by a human. |
| **P2** | Operational fragility; degrades under load or hides failures. |

---

## ~~P0-1 — Public reschedule 500s on a naive datetime~~ ✅ FIXED

**Fixed** with `PublicRescheduleInputSerializer` (DRF `DateTimeField`, so `USE_TZ` coercion
applies) plus `_ensure_aware()` at the booking-service boundary, which covers all four callers
rather than only this one. Regression tests
`test_post_naive_start_time_is_accepted_not_500`, `test_rebook_accepts_naive_start_time`,
`test_post_malformed_start_time_400_not_500`, `test_post_missing_start_time_400` — the first
two were confirmed to fail with `TypeError` against the pre-fix source. Original finding below.

**Where:** [public_reschedule.py:109-117](../../core/views/public_reschedule.py#L109-L117)

```python
raw_start = request.data.get("start_time")
start_time = parse_datetime(raw_start) if raw_start else None
if start_time is None:
    return Response(..., status=400)
new = rebooking.rebook(str(token), start_time)
```

`parse_datetime("2026-09-01T10:00:00")` (no offset) returns a **naive** datetime — only `None`
is rejected. `USE_TZ = True` ([base.py:211](../../config/settings/base.py#L211)), so
`timezone.now()` is aware, and `_validate_policy` does
`if start_time < now` ([bookings.py:44](../../core/services/bookings.py#L44)):

```
TypeError: can't compare offset-naive and offset-aware datetimes
```

The view catches `InvalidRescheduleToken`, `PolicyViolationError`, `NoEligibleHostError`,
`SlotConflictError` — **not** `TypeError`. It propagates to a 500.

**Why it matters most:** this is the one unauthenticated, public endpoint in the module
([public_reschedule.py:39-41](../../core/views/public_reschedule.py#L39-L41)). Anyone with a
reschedule link can trigger it with a plausible-looking timestamp — no malice required, a
frontend that forgets the `Z` suffix does it.

**Fix.** Replace hand-rolled parsing with a serializer, so DRF applies `USE_TZ` coercion and
returns a clean 400:

```python
class PublicRescheduleInputSerializer(serializers.Serializer):
    start_time = serializers.DateTimeField()
```

Then add a belt-and-braces normalization in `create_booking` — it is called from four entry
points (REST, public link, AI bridge, rebooking) and should not trust any of them:

```python
if timezone.is_naive(start_time):
    start_time = timezone.make_aware(start_time, dt_timezone.utc)
```

**Test:** `POST /public/reschedule/{token}/` with `"2026-09-01T10:00:00"` → 400, not 500.

---

## ~~P0-2 — `Timezone.timezone` accepts any string; availability 500s on a bad one~~ ✅ FIXED

**Fixed** by the [timezone conversion authority work](timezone-conversion-authority-plan.md):
`validate_iana_timezone` is now on the field, with `test_full_clean_rejects_bad_timezone` and
`test_every_seeded_zone_resolves` (the latter also catches a missing `tzdata` in CI). Original
finding retained below for context.

**Where:** [timezone.py](../../onboard/models/timezone.py) — was a plain
`CharField(unique=True)` with no validator.

`ZoneInfo(profile.timezone.timezone)` ([availability.py:281](../../core/services/availability.py#L281))
raises `ZoneInfoNotFoundError` for an unrecognised key. Nothing catches it, so a single
mistyped seed row 500s the availability endpoint for every host in that timezone.

Under the materialization plan the blast radius **grows**: the same call moves into
`Timezone.to_utc` and runs for every profile in the beat task, so one bad row aborts
materialization for everyone processed after it.

**Fix.**

```python
def validate_iana_timezone(value):
    try:
        ZoneInfo(value)
    except Exception:
        raise ValidationError(f"{value!r} is not a valid IANA timezone identifier.")

timezone = models.CharField(max_length=100, unique=True,
                            validators=[validate_iana_timezone])
```

Plus the CI test already specified in the plan (`test_every_seeded_zone_constructs`), and
per-profile isolation in the beat loop so one failure cannot abort the batch (P2-1).

---

## ~~P0-3 — `max_horizon_days` is unbounded~~ ✅ FIXED

**Fixed.** `MAX_HORIZON_DAYS = 365` and `MIN_SLOT_GRANULARITY_MINUTES = 5` on
[event_template.py](../../core/models/event_template.py), enforced by field validators, DRF
serializer methods, **and** two DB check constraints (`event_template_horizon_bounded`,
`event_template_granularity_min`) so a direct-ORM writer cannot bypass them. Migration
`0039_bound_scheduling_policy_fields`. Two test fixtures that used `max_horizon_days=400`
were lowered to 365. Original finding below.

**Where:** [event_template.py:69-72](../../core/models/event_template.py#L69-L72) —
`PositiveSmallIntegerField(default=60)`, no `MaxValueValidator`. Accepts up to **32767**.

`AvailableSlotsQuerySerializer` bounds a query span by `max_horizon_days`
([availability.py:33-37](../../core/serializers/availability.py#L33-L37)) — i.e. by a value the
tenant sets. Combined with `slot_granularity_minutes = 1` (the check constraint only requires
`> 0`) and `duration_minutes = 1`:

```
32767 days x 1440 candidate starts = ~47,000,000 loop iterations per host
```

per request, in-process, on a synchronous endpoint. For a COLLECTIVE template that multiplies
by host count. A tenant can do this to themselves through the normal UI.

**Under the new plan this gets worse, not better** — D6 sets the materialization horizon to
`max(setting, max_horizon_days)`, so the beat task would try to generate ~90 years of windows
per profile. This is why the plan now carries **D15**.

**Fix.** `MaxValueValidator(365)` on the field, a hard cap in `horizon_days_for()`, and a
sanity floor on `slot_granularity_minutes` (`MinValueValidator(5)` — a 1-minute granularity
has no legitimate booking use and is purely a cost multiplier).

---

## ~~P0-4 — `participants` list is unbounded~~ ✅ FIXED

**Fixed.** `MAX_PARTICIPANTS_PER_BOOKING = 25` enforced in `validate_participants`, and
`_create_participants` now `bulk_create`s attendees so the host row lock is held for one
statement rather than N. Note `bulk_create` skips `pre_save`, so contact resolution is applied
explicitly — covered by `test_bulk_create_still_resolves_contacts`, since losing it would be a
silent regression. Original finding below.

**Where:** [booking.py:111](../../core/serializers/booking.py#L111) —
`participants = BookingParticipantInputSerializer(many=True)`, validated only for non-emptiness
([:113-116](../../core/serializers/booking.py#L113-L116)).

`_create_participants` loops the list inside the booking transaction
([bookings.py:67-76](../../core/services/bookings.py#L67-L76)), one `INSERT` each, while holding
`SELECT ... FOR UPDATE` on the host row ([bookings.py:203](../../core/services/bookings.py#L203)).
A 50k-participant payload therefore holds a host-level lock for the duration of 50k inserts —
blocking every other booking for that host, and each insert fires the contact-resolution
`pre_save` signal ([signals.py:54-65](../../core/signals.py#L54-L65)).

**Fix.** `max_length` on the list field (25 is generous for a meeting), and `bulk_create` for
the attendee rows.

---

## ~~P0-5 — `on_commit` callbacks can 500 a committed booking~~ ✅ FIXED

**Fixed** with `core/services/deferred.py`. Every `transaction.on_commit` in `bookings.py`,
`reschedule.py` and `rebooking.py` is now wrapped in `deferred.safe(...)`, which logs a
traceback and returns instead of propagating. Note the deliberate carve-out: under
`CELERY_TASK_ALWAYS_EAGER` it **re-raises**, because eager mode means the task body runs inline
(a test) and swallowing there would turn a genuinely broken task into a silently passing test.
Original finding below.

**Where:** [bookings.py:233-237](../../core/services/bookings.py#L233-L237) and every other
`transaction.on_commit(...)` in the module.

```python
transaction.on_commit(lambda: notifications.send_confirmation(booking_id))
transaction.on_commit(lambda: _enqueue_calendar_push(booking_id))
```

`on_commit` callbacks run **after** the commit, still inside the request. `send_confirmation`
writes a `NotificationLog` row and calls `.delay()`; `_enqueue_calendar_push` calls `.delay()`.
If the broker is unreachable, `.delay()` raises — the booking is already durably committed, but
the caller gets a 500 and no notification is ever enqueued.

The failure mode is the bad one: **the customer is booked and believes they are not.** They
rebook, and the second attempt 409s against their own first booking.

**Fix.** Wrap each deferred side effect so it cannot fail the response:

```python
def _safe_enqueue(fn, *args):
    def _run():
        try:
            fn(*args)
        except Exception:
            logger.exception("deferred side effect failed; booking is committed")
    return _run

transaction.on_commit(_safe_enqueue(notifications.send_confirmation, booking_id))
```

The log line then needs an alert — a swallowed exception with no alert is just a quieter bug.
Longer term this is what a transactional outbox is for, but the wrapper is the 20-line version
that removes the 500.

---

## ~~P1-1 — Calendar-provider failure silently widens availability~~ ✅ FIXED

**Fixed.** `_external_busy()` now returns `(intervals, degraded)`, separating "the host has
nothing booked" from "we could not ask" — conflating those was the whole bug. Behaviour by
caller, matching the rule in *Cross-cutting 3*:

- **Browsing** degrades open (an outage must not empty the calendar) but every affected slot
  carries `external_check_failed`, exposed as `external_check_failed` on the slots API and
  `unverified` in the AI tool result.
- **Committing** raises the new `ExternalCalendarUnavailable` → **503** on both the booking
  API and the public reschedule endpoint, and `{"reason": "calendar_unavailable", "retry":
  true}` in the AI bridge. Deliberately *not* `SlotConflictError`: the slot may well be free,
  so telling the caller it is taken would be untrue.
- The failure now logs at **ERROR** with a traceback and writes `CalendarIntegration.last_error`.
  Previously a WARNING was the only trace and nothing surfaced it.

⚠️ **Product consequence, stated explicitly:** during a provider outage, hosts with a connected
calendar cannot be booked at all. That is deliberate — an unverified booking double-books the
host against an external meeting and nothing in this system can detect it afterwards — but it
trades availability for safety, and it is a one-line change in `eligible_free_hosts` to flip.
Original finding below.

**Where:** [availability.py:171-176](../../core/services/availability.py#L171-L176)

```python
except Exception as exc:
    logger.warning("free/busy fetch failed for integration %s: %s", integration.id, exc)
    return []
```

Deliberate — availability degrades rather than fails — and the trade-off is defensible. But `[]`
is indistinguishable from "the host has nothing in their calendar", so during a Google outage
the system **actively offers slots the host is busy for**, and the resulting double-booking is
invisible until the host notices. The exclusion constraint does not help: it only knows about
`Booking` rows, not external events.

**Suggested change.** Return a sentinel rather than a bare list, and let the caller decide:

- `get_available_slots` (browsing) — degrade open, as today, but mark affected slots
  `external_check_failed: true` so the UI can warn.
- `create_booking` (committing) — **fail closed**. A `SlotConflictError` asking the customer to
  retry is strictly better than a silent double-booking, and this path is already retry-shaped.

Also add a `CalendarIntegration.last_error` write + alert here; today the WARNING is the only
trace and nothing surfaces it.

---

## ~~P1-2 — `NotificationLog` is marked `SENT` before the email is sent~~ ✅ FIXED

**Fixed.** `_emit` leaves the row `PENDING` and passes `notification_log_id` to the task, which
marks `SENT` on success or `FAILED` with the error once retries are exhausted. `FAILED` rows are
re-enqueueable, so a lost notification can now recover — previously impossible.

One consequence had to be handled: moving the `SENT` write into the task opens a window where a
still-queued row could be re-enqueued by the next caller, which would double-send reminders
(the beat scans every 5 minutes). `PENDING_IN_FLIGHT_GRACE` (15 min, measured from `updated_at`)
treats a recent `PENDING` as in flight; past that it becomes re-sendable, which is also how a
message the broker lost recovers.

Testing note: under `CELERY_TASK_ALWAYS_EAGER`, `self.retry()` raises `Retry` immediately and
`request.retries` never advances, so retry exhaustion is unreachable end-to-end. The terminal
branch is tested by invoking the task with `retries=max_retries`. Original finding below.

**Where:** [notifications.py:96-105](../../core/services/notifications.py#L96-L105) — `.delay()`
then immediately `log.status = SENT; log.sent_at = now()`.

If `send_scheduling_email_task` exhausts its 3 retries
([tasks.py:48-50](../../core/tasks.py#L48-L50)) the row still reads `SENT`. The `FAILED` status and
the `error` field ([notification_log.py:26-63](../../core/models/notification_log.py#L26-L63)) are
declared and **never written by any code path**. Because `_emit` short-circuits on
`status == SENT` ([notifications.py:91-92](../../core/services/notifications.py#L91-L92)), the
notification can also never be retried later.

So: customer silently never receives their confirmation, and the system reports success.

**Fix.** Leave the row `PENDING` at enqueue; have the task mark `SENT` on success and `FAILED`
with the exception text in `on_failure`. Pass `dedup_key` into the task so it can find its row.
Then a periodic sweep can retry `FAILED` rows, which is currently impossible.

---

## ~~P1-3 — `is_active` is not honoured on the REST booking path~~ ✅ FIXED

**Fixed.** Both `BookingCreateSerializer.event_template` and
`AvailableSlotsQuerySerializer.event_template` now use
`EventTemplate.objects.filter(is_active=True)`. Original finding below.

**Where:** [booking.py:100](../../core/serializers/booking.py#L100) —
`EventTemplate.objects.all()`, no `is_active` filter. The default manager excludes
`is_deleted` only ([base_model_core.py:19-25](../../utils/models/base_model_core.py#L19-L25)).

The AI bridge **does** filter it
([agent_scheduling.py:46-48](../../core/services/agent_scheduling.py#L46-L48)), so deactivating a
template stops the voicebot booking it but not the REST API — two callers disagreeing about
what is bookable. An admin who deactivates a template reasonably believes it is off.

**Fix.** `EventTemplate.objects.filter(is_active=True)` in the serializer field. The
availability profile half of this is already handled by the materialization plan's D9.

---

## ~~P1-4 — Buffer handling differs between the two availability entry points~~ ✅ FIXED

**Fixed.** `eligible_free_hosts` now tests leave and external-busy against `eff_start`/`eff_end`,
matching `get_available_slots`. Working hours stay on the raw times in both — buffers pad the
surrounding calendar, they do not extend the shift. Original finding below.

**Where:** `get_available_slots` tests all three busy sources against buffer-inclusive
`eff_start`/`eff_end` ([availability.py:299-301](../../core/services/availability.py#L299-L301));
`eligible_free_hosts` tests leave and external-busy against **raw** `start_utc`/`end_utc` and
only bookings against `effective_*`
([availability.py:232-242](../../core/services/availability.py#L232-L242)).

A leave that overlaps only a booking's buffer therefore blocks the slot at listing time but
passes the create-time check — the two answers disagree about the same slot.

**Fix.** Build one `busy` list and test it once against `eff_start`/`eff_end` in both paths.
Already scheduled as §6.5 of the materialization plan; listed here because it is independently
worth doing.

---

## ~~P1-5 — Calendar tasks retry permanent errors, then fail silently~~ ✅ FIXED

**Fixed.** `calendars.is_permanent()` / `http_status_of()` classify provider errors, and
`_handle_permanent_calendar_error()` acts on them: 401/403 → `REAUTH_REQUIRED` (which also drops
the integration out of availability lookups), 404/410 → clear `external_event_id` so the next
push re-inserts instead of updating an event that no longer exists, other 4xx → `ERROR` with
`last_error`. 408/429 stay retryable — rate limiting is not a defect. Applied to all four
calendar tasks. Original finding below.

**Where:** [tasks.py:129-131](../../core/tasks.py#L129-L131) and the sibling calendar tasks —
`except Exception: ... raise self.retry(exc=exc)`.

Every failure is treated as transient. A revoked token (401), a deleted remote event (404), or
a malformed payload (400) is retried 3× and then abandoned. Nothing marks the integration
`ERROR`, nothing alerts, and `Booking.external_event_id` keeps pointing at an event that no
longer exists — so the next `push_booking_to_calendar` takes the `update_event` branch
([tasks.py:121-123](../../core/tasks.py#L121-L123)) and fails forever.

**Fix.** Split permanent from transient: on 401/403 set `sync_status = REAUTH_REQUIRED`; on 404
clear `external_event_id` so the next push re-inserts; retry only 5xx/timeouts. Alert on
terminal failure.

---

## ~~P1-6 — `.ics` `SEQUENCE` never increments~~ ✅ FIXED

**Fixed.** `_ics_sequence()` counts the booking's CREATED/CONFIRMED/RESCHEDULED/CANCELED
`BookingEvent` rows. Derived from the append-only log rather than stored, so it only ever
grows — exactly the guarantee RFC 5545 needs. Group invites share one UID, so they take the
max across the group's rows. Original finding below.

**Where:** [ics.py:23-36](../../core/services/ics.py#L23-L36) — `sequence=0` default, no caller
overrides, despite the docstring stating it must increase on each update.

Per RFC 5545 a client may ignore an updated `VEVENT` with the same `UID` and a
non-increasing `SEQUENCE`. So a reschedule confirmation can leave the original time sitting in
the attendee's calendar.

**Fix.** Derive from the booking's revision count — e.g. `BookingEvent.objects.filter(booking=b,
event_type__in=(CREATED, RESCHEDULED, CONFIRMED)).count()`.

---

## ~~P2-1 — Beat tasks iterate unbounded and abort as a batch~~ ✅ FIXED

**Fixed.** `send_due_reminders` and the two calendar beat loops now stream with `.iterator()`
and isolate per item; `release_expired_holds` is capped at 500 per run (the beat fires every
60s, so a backlog drains across runs) and isolated per hold. One bad row no longer strands
every later one — which previously also meant the next run hit the same row again. Original
finding below.

- `release_expired_holds` ([bookings.py:504-523](../../core/services/bookings.py#L504-L523))
  materializes every expired id, then opens **one transaction per hold**.
- `send_due_reminders` ([reminders.py:23-38](../../core/services/reminders.py#L23-L38)) loads all
  upcoming bookings into memory — no `.iterator()`, no tenant scoping, no cap.
- `refresh_calendar_tokens` ([tasks.py:202-221](../../core/tasks.py#L202-L221)) and the planned
  materialization task loop over integrations/profiles with no per-item isolation.

One raising row aborts the whole run, and the next run hits the same row — a permanently stuck
beat task with no alert.

**Fix.** `.iterator(chunk_size=...)`, a per-run cap, and `try/except` **per item** with the
failure logged and the loop continuing. This matters most for the new materialization task,
where one bad timezone (P0-2) would otherwise take out everyone's availability.

---

## ~~P2-2 — No alerting on exhausted Celery retries~~ ✅ FIXED

**Fixed.** `ReportedTask` base class with an `on_failure` hook, applied to all 18 tasks in
`core/tasks.py`. Emits one structured ERROR to the dedicated `core.tasks.failures` logger with
task name, id and retry count — alert on that logger. Original finding below.

Every task in [core/tasks.py](../../core/tasks.py) uses `max_retries=3` and then gives up. There
is no dead-letter queue, no `on_failure` hook, and no metric. Confirmations, calendar pushes,
and leave-driven reschedules can all fail permanently and silently.

**Fix.** A shared `Task` base class with `on_failure` that writes a structured ERROR and
increments a counter — `django-prometheus` is already a dependency
([pyproject.toml](../../pyproject.toml)).

---

## ~~P2-3 — `_load_by_token` picks a group member non-deterministically~~ ✅ FIXED

**Fixed.** `.order_by("created_at", "id")`. Original finding below.

**Where:** [rebooking.py:29-36](../../core/services/rebooking.py#L29-L36) — `.filter(...).first()`
under `Booking.Meta.ordering = ["-start_time"]`. Every row in a COLLECTIVE group shares a
token *and* a `start_time`, so the tiebreak is whatever Postgres returns.

Harmless today (the collective rebooking path uses only `group_id` and `event_template`, which
are identical across the group), but it is load-bearing correctness resting on an accident.

**Fix.** `.order_by("created_at", "id")` for a stable pick.

---

## Cross-cutting suggestions

**1. Push invariants down to the database while it is free.** ✅ **partially done.** The
exclusion constraint ([booking.py](../../core/models/booking.py)) is the best thing in this
codebase — double-booking is impossible rather than merely checked. Extending it:

- ✅ `booking_held_requires_expiry` — a hold with no expiry never lapses and blocks its slot
  forever, because `release_expired_holds` filters on `hold_expires_at__lt`. Shipped.
- ✅ `event_template_horizon_bounded`, `event_template_granularity_min` — see P0-3. Shipped.
- ❌ **`CHECK (group_id IS NOT NULL)` for COLLECTIVE — withdrawn, not expressible.**
  `assignment_strategy` lives on `EventTemplate`, and a table-level `CHECK` cannot join. It
  would need a trigger or a denormalized column; neither is worth it for this.
- ❌ **Partial unique index on `reschedule_token` — withdrawn, and it would have been a bug.**
  A COLLECTIVE group deliberately shares **one** token across all its member rows
  ([reschedule.py](../../core/services/reschedule.py) assigns the same `token` to every member),
  so a unique index would make group reschedule fail outright. Uniqueness belongs to the
  *reschedule request*, which is not a row in this table. See P2-3 for the related ordering
  issue, which is the real defect in that area.
- `AvailabilityWindow`'s `end_utc > start_utc` (in the overnight plan, not yet built).

**2. Contract-test the four booking entry points against each other.** REST, public link, AI
bridge, and rebooking all reach `create_booking` with different validation in front of them —
which is exactly how P0-1 and P1-3 arose. One parametrized suite asserting that all four reject
the same bad inputs would have caught both.

**3. Decide the fail-open/fail-closed rule once, explicitly.** Today it is implicit and
inconsistent: external calendar errors fail **open** (P1-1), missing availability profiles fail
**closed** silently, notification failures fail **open** and lie. Write the rule down —
suggestion: *browsing degrades open with a visible marker; committing fails closed* — and make
each site match it.

**4. Add a booking-invariant audit command.** A read-only `manage.py audit_bookings` that
reports: active bookings outside their host's availability windows, HELD rows past expiry,
`external_event_id` set on cancelled bookings, `NotificationLog` rows `PENDING` for over an
hour, profiles with rules but no windows. Cheap to write, and it turns every silent failure
mode above into something a cron job notices.

---

## Suggested order

| Step | Items | Rationale |
|---|---|---|
| ~~0~~ | ~~P0-2~~ | ✅ done — shipped with the timezone conversion authority |
| ~~1~~ | ~~P0-1~~ | ✅ done — serializer + service-boundary guard, 4 regression tests |
| ~~2~~ | ~~P0-3, P0-4, *Cross-cutting 1*~~ | ✅ done — migration `0039`, 13 tests. Two proposed constraints withdrawn as not expressible |
| ~~3~~ | ~~P0-5, P1-2~~ | ✅ done — `deferred.safe` + task-owned notification status, 11 tests |
| ~~4~~ | ~~P1-1, P1-3, P1-4~~ | ✅ done — 11 tests. Fail-open/closed rule now applied consistently |
| ~~5~~ | ~~P2-1~~ | ✅ done — streaming + per-item isolation |
| ~~6~~ | ~~P1-5, P1-6, P2-2, P2-3~~ | ✅ done — 10 tests. *Cross-cutting 4* (audit command) still open |

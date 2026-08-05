# Booking Flow — Current Implementation Reference

A code-level walkthrough of how the scheduling & booking module actually works today.
Where [flows/README.md](README.md) and its numbered docs describe the flows from a user's
point of view, this document describes the **implementation**: which module owns each
decision, what the database enforces, where transactions and locks sit, what runs
asynchronously, and where the current behaviour has sharp edges.

Every claim here is traceable to a file and line. Nothing in this document is proposed
work — it is a description of the code as it stands.

---

## Contents

1. [Component map](#1-component-map)
2. [Data model](#2-data-model)
3. [System invariants](#3-system-invariants)
4. [Preconditions — what setup a booking requires](#4-preconditions--what-setup-a-booking-requires)
5. [Phase A — Slot discovery](#5-phase-a--slot-discovery)
6. [Phase B — Booking creation](#6-phase-b--booking-creation)
7. [Hold → confirm](#7-hold--confirm)
8. [Collective bookings](#8-collective-bookings)
9. [Cancel, complete, no-show](#9-cancel-complete-no-show)
10. [Leave → reschedule engine](#10-leave--reschedule-engine)
11. [Public rebooking](#11-public-rebooking)
12. [External calendar sync](#12-external-calendar-sync)
13. [Notifications & reminders](#13-notifications--reminders)
14. [AI-agent booking path](#14-ai-agent-booking-path)
15. [Tenancy & authorization](#15-tenancy--authorization)
16. [Async surface — tasks & beat schedule](#16-async-surface--tasks--beat-schedule)
17. [Status machine](#17-status-machine)
18. [Error → HTTP mapping](#18-error--http-mapping)
19. [Configuration surface](#19-configuration-surface)
20. [Known gaps & sharp edges](#20-known-gaps--sharp-edges)

---

## 1. Component map

| Layer | Module | Responsibility |
|---|---|---|
| HTTP | [core/views/booking.py](../../core/views/booking.py) | Booking CRUD + lifecycle actions; maps domain errors to HTTP codes |
| HTTP | [core/views/availability.py](../../core/views/availability.py) | `GET available-slots` |
| HTTP | [core/views/public_reschedule.py](../../core/views/public_reschedule.py) | Unauthenticated, token-gated rebooking |
| HTTP | [core/views/calendar_webhook.py](../../core/views/calendar_webhook.py) | Google push-notification receiver |
| Validation | [core/serializers/booking.py](../../core/serializers/booking.py) | Input shape, tenant ownership, host-skill check |
| Validation | [core/serializers/availability.py](../../core/serializers/availability.py) | Slot-query shape, horizon check, strategy/host coherence |
| Engine | [core/services/availability.py](../../core/services/availability.py) | Slot generation, free-host resolution |
| Engine | [core/services/bookings.py](../../core/services/bookings.py) | **Sole** source of booking mutations |
| Engine | [core/services/reschedule.py](../../core/services/reschedule.py) | Leave → flag affected bookings |
| Engine | [core/services/rebooking.py](../../core/services/rebooking.py) | Token-gated customer rebooking |
| Side effects | [core/services/notifications.py](../../core/services/notifications.py) | Email + `.ics`, idempotent |
| Side effects | [core/services/reminders.py](../../core/services/reminders.py) | Beat-driven reminder scan |
| Side effects | [core/services/calendar_sync.py](../../core/services/calendar_sync.py) | Watch lifecycle + incremental sync |
| Side effects | [core/services/calendars/google.py](../../core/services/calendars/google.py) | Google provider adapter |
| Audit | [core/services/events.py](../../core/services/events.py) | Append-only `BookingEvent` writer |
| AI bridge | [core/services/agent_scheduling.py](../../core/services/agent_scheduling.py) | Guardrailed surface for the voicebot |
| AI bridge | [core/services/agent_scheduling_tools.py](../../core/services/agent_scheduling_tools.py) | LLM tool schemas + dispatcher |
| Async | [core/tasks.py](../../core/tasks.py) | All Celery tasks |
| Async | [core/signals.py](../../core/signals.py) | Leave → enqueue reschedule; contact resolution |

**Architectural rule:** the viewset never calls `serializer.save()` for a booking.
`BookingSerializer` is entirely read-only ([booking.py:65](../../core/serializers/booking.py#L65)) and
`http_method_names` excludes `put`/`patch` ([booking.py:40](../../core/views/booking.py#L40)). All writes
funnel through `core.services.bookings`, which is what makes the concurrency and
notification guarantees below possible.

---

## 2. Data model

All models inherit soft-delete + audit scaffolding from
[utils/models/base_model_core.py](../../utils/models/base_model_core.py). Two tiers are in use:

- **Tier 2** (`TenantBaseModel`) — adds `created_by` / `updated_by` / `deleted_by`. Used where
  accountability matters.
- **Tier 1** (`TenantBaseModelCore`) — no user-audit FKs. Used for high-volume junction/log rows.

The default manager `objects` **already excludes `is_deleted=True`**
([base_model_core.py:19-25](../../utils/models/base_model_core.py#L19-L25)); `all_objects` sees everything.
Bulk `.delete()` raises by design ([base_model_core.py:6-7](../../utils/models/base_model_core.py#L6-L7)).
`is_active` is **not** filtered by the default manager — this matters, see §20.

### 2.1 `EventTemplate` (Tier 2)

[core/models/event_template.py](../../core/models/event_template.py) — the bookable appointment type.
Carries every policy knob the engine reads:

| Field | Role in the flow |
|---|---|
| `duration_minutes` | Slot length; `end_time = start_time + duration` |
| `buffer_before_minutes` / `buffer_after_minutes` | Folded into `effective_*` times, so padding participates in overlap detection |
| `assignment_strategy` | `DIRECT` (name a host) or `COLLECTIVE` (book every skilled host) |
| `min_notice_minutes` | Earliest bookable offset from now (default 120) |
| `max_horizon_days` | Furthest bookable date (default 60) |
| `slot_granularity_minutes` | Step between candidate starts (default 15) |
| `cancellation_lead_minutes` / `reschedule_lead_minutes` | Minimum notice to cancel/reschedule (default 60) |

`soft_delete()` is overridden to suffix the name with a deletion timestamp so the
`unique(name, tenant)` slot is reusable, trimming the base name first so the result never
exceeds `max_length` ([event_template.py:119-144](../../core/models/event_template.py#L119-L144)).

### 2.2 `UserEventSkill` (Tier 1)

[core/models/user_event_skill.py](../../core/models/user_event_skill.py) — the routing table:
which host may take which event type. Unique on `(user, event_template, tenant)` while active.
**This is the only thing that makes a user a candidate host.**

### 2.3 `AvailabilityProfile` + `AvailabilityRule`

[availability_profile.py](../../core/models/availability_profile.py) (Tier 2) is a container:
`user`, `timezone` (FK to an IANA zone), `name`. Unique per `(user, tenant)` while active —
**one profile per user**.

[availability_rule.py](../../core/models/availability_rule.py) (Tier 1) holds the **authoring** form
of the hours:

- `weekday` — `0=Monday … 6=Sunday`, matching Python's `weekday()`. The day the block **starts** on.
- `start_time` / `end_time` — `TimeField`, **naive wall-clock in the parent profile's timezone**, not UTC
- `end_time <= start_time` means the block **ends on the next day**: `22:00 → 06:00` is an overnight
  shift and `00:00 → 00:00` is a full 24 hours. There is deliberately **no** `end > start` check
  constraint — that is exactly the rule that forbade overnight.
- A zero-length block is therefore not expressible, by design.
- Multiple rows per weekday allowed → split shifts

Coherence is enforced across the **whole week** rather than per weekday: `validate_rules` places
every rule on a circular 604 800-second timeline and compares each pair against its neighbours a
week either side ([availability_profile.py](../../core/serializers/availability_profile.py)).
Per-weekday sorting structurally cannot see `Mon 22:00→06:00` colliding with `Tue 02:00→09:00`, nor
Sunday night colliding with Monday morning.

Sending `rules` on `PUT`/`PATCH` **replaces** the whole set; omitting the key leaves it untouched.
Any write that changes rules, the timezone, or `is_active` queues a rebuild of the materialized
windows (§2.4).

### 2.4 `AvailabilityWindow` (Tier 1) — the operational form

[core/models/availability_window.py](../../core/models/availability_window.py). One materialized,
**absolute UTC** interval per rule per local date. **This is the only availability the engine
reads** — it never walks `AvailabilityRule`.

| Field | Meaning |
|---|---|
| `profile` / `rule` | provenance; both `CASCADE` |
| `user` | denormalized from `profile.user` so the engine filters without a join |
| `local_date` | the local date the block **starts** on — regeneration key, and what makes a materialization bug diagnosable |
| `start_utc` / `end_utc` | absolute instants; `end_utc > start_utc` is a check constraint |
| `dst_flag` | `"" \| "nonexistent" \| "ambiguous"` — diagnostic; answers "why was this shift an hour short in March?" |

Unique on `(rule, local_date)`. Rows are **pure derived data**: hard-deleted and regenerated, never
soft-deleted. Rebuild with `manage.py materialize_availability`.

Why the split exists: a recurring weekly rule cannot itself be stored in UTC and stay DST-correct.
"09:00 Monday in Europe/London" is `09:00Z` in winter and `08:00Z` in summer — one stored UTC time
is wrong for half the year, whichever half you pick. So authoring stays wall-clock, conversion
happens **once** at materialization time, and every comparison the engine performs is UTC-to-UTC.

The conversion itself lives on `onboard.Timezone.to_utc()`
([timezone.py](../../onboard/models/timezone.py)) — no scheduling code constructs `ZoneInfo`
directly. DST policy, applied in
[availability_materializer.py](../../core/services/availability_materializer.py):

| Case | Policy |
|---|---|
| Transition falls *inside* the block, both boundaries clear | nothing to do — it falls out of the UTC arithmetic (a `22:00→06:00` rule is 7 h in spring, 9 h in autumn) |
| **Nonexistent** boundary (spring-forward gap) | snap to the **exact transition instant**, flag `nonexistent` |
| **Ambiguous** boundary (fall-back repeat) | `fold=0` for a start, `fold=1` for an end, so the block covers real elapsed time |
| Block **wholly** inside the gap | emit **no window** for that date, INFO log — it genuinely does not occur |

Regeneration is driven by: a profile write (post-commit task), the daily
`materialize_availability_horizon` beat task, or the management command. Horizon is
`AVAILABILITY_MATERIALIZE_DAYS` (default 90), raised to cover any `EventTemplate.max_horizon_days`
the tenant advertises and hard-capped at `MAX_HORIZON_DAYS` (365).

### 2.5 `AvailabilityException` (Tier 2)

[core/models/availability_exception.py](../../core/models/availability_exception.py) — leave/time-off.
Note the contrast with `AvailabilityRule`: same field names, different type. Here
`start_time`/`end_time` are **`DateTimeField`s, absolute UTC windows**. Types:
`VACATION | SICK | EMERGENCY | HOLIDAY | OTHER`.

### 2.6 `Booking` (Tier 2)

[core/models/booking.py](../../core/models/booking.py) — one real appointment occurrence.

Times are stored twice:

- `start_time` / `end_time` — what the customer sees
- `effective_start_time` / `effective_end_time` — buffer-inclusive, what the **database** uses for overlap

The correctness backbone is a Postgres GiST exclusion constraint
([booking.py:161-176](../../core/models/booking.py#L161-L176)):

```python
ExclusionConstraint(
    name="core_booking_no_overlap_per_host",
    expressions=[
        ("tenant",       RangeOperators.EQUAL),
        ("primary_host", RangeOperators.EQUAL),
        (TsTzRange("effective_start_time", "effective_end_time", RangeBoundary()),
         RangeOperators.OVERLAPS),
    ],
    condition=Q(is_deleted=False) & Q(status__in=ACTIVE_BOOKING_STATUSES),
)
```

Three things follow from this and they drive the whole service design:

- Two active bookings for the same host in the same tenant **cannot** overlap. Not "should not" — cannot.
- The default `[)` half-open bound means back-to-back bookings do **not** clash.
- `ACTIVE_BOOKING_STATUSES = ["HELD", "SCHEDULED", "CONFIRMED", "REQUIRES_RESCHEDULE"]`
  ([booking.py:16](../../core/models/booking.py#L16)) — so a **hold occupies the slot**, and so does a
  booking flagged for reschedule.

Other fields: `status`, `source` (`CUSTOMER | INTERNAL | PUBLIC_LINK | AI`), `hold_expires_at`,
`reschedule_token` + expiry, `external_event_id` / `external_provider` (calendar mirror idempotency
key), `group_id` (ties COLLECTIVE rows together), `cancellation_reason`.

### 2.7 `BookingParticipant` (Tier 1)

[core/models/booking_participant.py](../../core/models/booking_participant.py) — `HOST | ATTENDEE | GUEST`,
with `response_status` for RSVP. `contact` is resolved from `phone_number` by a `pre_save` signal
([signals.py:54-65](../../core/signals.py#L54-L65)) and is `SET_NULL` so deleting a contact never
destroys booking history.

### 2.8 `BookingEvent` (Tier 1)

[core/models/booking_event.py](../../core/models/booking_event.py) — append-only audit log. Rows are
never updated or deleted. Because the project has no history/versioning layer, **this is the
only trail** for support and debugging. Written exclusively via
[`events.log_event()`](../../core/services/events.py).

### 2.9 `NotificationLog` (Tier 1)

[core/models/notification_log.py](../../core/models/notification_log.py) — the idempotency ledger.
`UniqueConstraint(tenant, dedup_key)` guarantees a given logical notification is enqueued at
most once, across Celery retries and duplicate service calls.

### 2.10 `CalendarIntegration` + `ExternalBusyBlock`

[calendar_integration.py](../../core/models/calendar_integration.py) (Tier 2) — OAuth connection.
Tokens stored Fernet-encrypted via `set_tokens()` / `get_access_token()`, never serialized.
Also holds the watch-channel state (`watch_channel_id`, `watch_resource_id`, `watch_token`,
`watch_expires_at`) and the incremental `sync_token`.

[external_busy_block.py](../../core/models/external_busy_block.py) (Tier 1) — a pure cache of the
host's external busy intervals, kept fresh by webhook sync. Rows are **hard**-deleted.

---

## 3. System invariants

These are the properties the code is built to preserve. Each is enforced somewhere specific.

| # | Invariant | Enforced by |
|---|---|---|
| 1 | No two active bookings overlap for one host in one tenant | DB exclusion constraint, [booking.py:161](../../core/models/booking.py#L161) |
| 2 | Buffers count toward overlap | `effective_*` columns written in [bookings.py:190-191](../../core/services/bookings.py#L190-L191) |
| 3 | Nothing is auto-assigned to a host pool | `create_booking` raises without `target_host_id` ([bookings.py:140-141](../../core/services/bookings.py#L140-L141)) |
| 4 | Every service query is explicitly tenant-scoped | Services run outside DRF; no implicit scoping ([availability.py:1-8](../../core/services/availability.py#L1-L8)) |
| 5 | External side effects never fire on a rolled-back transaction | `transaction.on_commit` throughout `bookings.py` |
| 6 | A logical notification is sent at most once | `NotificationLog.dedup_key` unique per tenant |
| 7 | Bookings mutate only through the service layer | Read-only serializer + no PUT/PATCH |
| 8 | Wall-clock hours resolve DST-correctly | `Timezone.to_utc()` ([timezone.py](../../onboard/models/timezone.py)), applied once at materialization |
| 9 | Every interval the engine compares is an absolute UTC instant | `AvailabilityWindow.start_utc`/`end_utc`; the engine never reads a `TimeField` |

---

## 4. Preconditions — what setup a booking requires

Before any booking can succeed, four things must exist for the tenant:

1. **`EventTemplate`** — defines duration and policy.
2. **`UserEventSkill`** linking a user to that template — without it the user is not a candidate,
   and both the serializer ([booking.py:161-169](../../core/serializers/booking.py#L161-L169)) and the
   service ([bookings.py:182-185](../../core/services/bookings.py#L182-L185)) reject the booking.
3. **`AvailabilityProfile` + at least one `AvailabilityRule`** for that user — `_load_profiles`
   returns nothing for a host without a profile, and `eligible_free_hosts` then skips them
   silently.
4. **Materialized `AvailabilityWindow` rows** covering the requested dates. Writing rules through
   the API queues this automatically; rules inserted through the ORM, a fixture, or a shell do
   **not** materialize themselves and the host stays invisible until
   `manage.py materialize_availability` runs.
5. *(Optional)* **`CalendarIntegration`** — enables external free/busy and event mirroring.

A host missing #3 produces **zero slots and no error message** — the most common
"why is nothing showing up" cause. Missing #4 produces zero slots *and* an ERROR log (§20.1).

---

## 5. Phase A — Slot discovery

**Endpoint:** `GET /api/v1/core/availability/available-slots/`
→ [`AvailabilityViewSet.available_slots`](../../core/views/availability.py#L61)

### 5.1 Request validation

[`AvailableSlotsQuerySerializer`](../../core/serializers/availability.py#L7) requires
`event_template`, `date_from`, `date_to`, and conditionally `target_host`:

- `date_to >= date_from`
- `(date_to - date_from).days <= max_horizon_days`
- **DIRECT** → `target_host` **required**. The comment at
  [availability.py:44-46](../../core/serializers/availability.py#L44-L46) explains why: returning the
  union over all skilled hosts would advertise slots that then 409 on booking.
- **COLLECTIVE** → `target_host` must be **absent**.

### 5.2 Tenant resolution

[`_resolve_tenant`](../../core/views/availability.py#L29): superuser → the template's own tenant;
`VB_AGENT` → the `X-Tenant-ID` header; everyone else → their tenant mapping. In all
non-superuser cases the template must actually belong to the resolved tenant. Deriving the
tenant *from the template* was deliberately avoided — that would let the caller pick its own scope.

### 5.3 The engine — `get_available_slots`

[core/services/availability.py:248-323](../../core/services/availability.py#L248-L323). Step by step:

**1. Compute policy bounds**

```python
min_start = now + min_notice_minutes
horizon   = now + max_horizon_days
duration  = duration_minutes
step      = slot_granularity_minutes
```

**2. Resolve candidate hosts** — [`_resolve_host_ids`](../../core/services/availability.py#L52).
Starts from `skilled_host_ids` (the `UserEventSkill` rows). A caller-supplied `target_host_id`
is still intersected against that list — an unchecked target would be a way to book *any* user,
including one from another tenant, as host for an event type they were never configured for.
The match is done on `str()` but returns the canonical UUID, because the AI tool layer hands
in JSON strings and downstream maps are keyed by UUID.

**3. Load profiles** — [`_load_profiles`](../../core/services/availability.py#L71), one query with
`select_related("timezone").prefetch_related("rules")`. Returns `{host_id: (profile, [rules])}`.
Hosts with no profile are absent from the map and get skipped. The `rules` are used only to detect
stale materialization (step 4a) — the engine does **not** generate slots from them.

**4. Build a generous UTC window** — `date_from 00:00 UTC − 1 day` to `date_to 23:59:59.999999 UTC + 1 day`.
The ±1 day absorbs timezone skew and picks up an overnight window anchored the day before the range.

**4a. Load materialized windows** — [`_load_windows`](../../core/services/availability.py), one
query for all hosts, returning `{host_id: [(start_utc, end_utc), …]}` ordered by start. The filter is
pure interval overlap (`start_utc < range_end AND end_utc > range_start`), never a date match, which
is why an overnight window anchored on the previous local date is found with no date fudging.

A host with rules but **no future windows at all** logs ERROR
(`materialization is stale or failed`) and yields no slots. There is deliberately no rule-walking
fallback — a second code path would hide exactly that failure. Note the check is "no future windows
anywhere", not "none in this range": a host who simply does not work on the queried date is normal
and must stay silent, or the alert is worthless.

**5. Per host, fetch busy intervals once** — no N+1. Three sources concatenated:

| Source | Function | Filter |
|---|---|---|
| Own bookings | [`_busy_intervals`](../../core/services/availability.py#L98) | active statuses, `effective_*` overlap |
| Leave | [`_exception_intervals`](../../core/services/availability.py#L113) | `AvailabilityException` overlap |
| External calendar | [`_external_busy_intervals`](../../core/services/availability.py#L125) | see §5.4 |

**6. Walk the windows**

```python
for w_start, w_end in host_windows:
    cursor = w_start
    while cursor + duration <= w_end:
        start_utc, end_utc = cursor, cursor + duration
        if date_from <= start_utc.astimezone(tz).date() <= date_to and (
            min_start <= start_utc <= horizon
        ):
            if not _overlaps(busy, start_utc - buf_before, end_utc + buf_after):
                slot_map.setdefault(start_utc, set()).add(host_id)
        cursor += step
```

Pure UTC arithmetic — DST was already resolved when the window was materialized.

Two details that look incidental but are not:

- **Raw windows, not merged.** The cursor restarts at each window's own start, which is the same
  instant the old per-day rule loop started from, so slot alignment is preserved exactly. Merging
  here would let a granularity that does not divide the day (e.g. 25 min) drift across day
  boundaries and silently move every advertised time.
- **The local-date filter.** Windows are loaded a day either side, so the requested range is
  enforced on the slot's **local** date — which is what `date_from`/`date_to` mean to the caller.
  A consequence worth knowing: the 02:00 tail of a Monday-night shift is offered when **Tuesday**
  is requested, not Monday.

**7. Collapse to slots** — `slot_map` is `start_utc → set(host_id)`.

- **COLLECTIVE**: a slot survives only if `len(hosts) == len(considered_hosts)` — every skilled host free.
- **DIRECT**: any slot with ≥1 free host survives (and since `target_host_id` is mandatory for
  DIRECT, that set is a single host).

Returns a sorted list of frozen `Slot(start, end, eligible_host_ids)` dataclasses
([availability.py:27-32](../../core/services/availability.py#L27-L32)).

### 5.4 External busy resolution

[`_external_busy_intervals`](../../core/services/availability.py#L125) has three paths:

1. **No connected integration** → `[]`.
2. **Live watch channel** (`watch_channel_id` set and `watch_expires_at > now`) → read
   `ExternalBusyBlock` locally. Webhook sync keeps it fresh, so no provider round-trip.
3. **No live watch** → live free/busy call, cached under
   `freebusy:{integration_id}:{window_start_ts}:{window_end_ts}` for
   `EXTERNAL_FREEBUSY_CACHE_SECONDS` (default 60).

Any provider exception is caught, logged at WARNING, and returns `[]`
([availability.py:171-173](../../core/services/availability.py#L171-L173)). This is deliberate: availability
degrades to "the external calendar said nothing" rather than failing the booking flow. The
trade-off is that a Google outage can surface slots the host is actually busy for.

### 5.5 Response

```json
{ "status": true, "message": "Available slots retrieved successfully",
  "data": [ { "start": "...", "end": "...", "eligible_host_ids": ["..."] } ] }
```

---

## 6. Phase B — Booking creation

**Endpoint:** `POST /api/v1/core/bookings/` → [`BookingViewSet.create`](../../core/views/booking.py#L106)
→ `_create(status_field=SCHEDULED, hold=False)`.

### 6.1 Serializer validation — `BookingCreateSerializer`

[core/serializers/booking.py:96-170](../../core/serializers/booking.py#L96-L170):

1. Shape: `event_template`, `start_time`, optional `target_host`, non-empty `participants`.
2. Tenant resolution — `require_agent_tenant(request)` first (machine callers), falling back to
   `request.user.tenant_mapping.tenant`. The comment at
   [booking.py:122-126](../../core/serializers/booking.py#L122-L126) records why: the mapping lookup raises
   `RelatedObjectDoesNotExist`, which subclasses `AttributeError`, so without the agent branch
   **every** VB_AGENT booking would fail as "not associated with an active tenant".
3. `event_template.tenant_id == tenant.id`.
4. Strategy coherence — COLLECTIVE rejects `target_host`; DIRECT requires it.
5. **Host-skill check.** `target_host` comes from an unscoped queryset (any user in any tenant)
   and is now the sole host-selection input, so a `UserEventSkill` row must exist for
   `(tenant, template, user)`.

### 6.2 Service — `create_booking`

[core/services/bookings.py:119-159](../../core/services/bookings.py#L119-L159):

```python
if not target_host_id: raise NoEligibleHostError(...)
end_time = start_time + duration_minutes
_validate_policy(event_template, start_time, now)
return _with_conflict_retry(lambda: _create_once(...))
```

`_validate_policy` ([bookings.py:43-55](../../core/services/bookings.py#L43-L55)) rejects, in order:
past start → `min_notice_minutes` → `max_horizon_days`. All three raise `PolicyViolationError`
carrying `field="start_time"`.

### 6.3 `_create_once` — the critical section

[core/services/bookings.py:162-238](../../core/services/bookings.py#L162-L238):

1. **Pre-check** via `eligible_free_hosts`. If empty, distinguish two cases:
   - not skilled → `NoEligibleHostError` (400, caller must fix)
   - skilled but busy → `SlotConflictError` (409, worth retrying)

   This distinction is the whole reason `is_host_skilled` exists as a separate query.

2. **Compute effective times** — `start − buffer_before`, `end + buffer_after`.

3. **Hold TTL** — only when `status == HELD`: `now + BOOKING_HOLD_TTL_SECONDS` (default 300).

4. **Transaction**:
   ```python
   with transaction.atomic():
       CustomUser.objects.filter(pk=host_id).select_for_update().get()   # serialize per host
       booking = Booking.objects.create(...)                              # may raise IntegrityError
   ```
   The row lock on the **host user** is what serializes concurrent inserts for the same host.
   Without it, two transactions racing the GiST exclusion check deadlock rather than one
   losing cleanly.

5. **`IntegrityError` → `SlotConflictError`.** The comment at
   [bookings.py:220-221](../../core/services/bookings.py#L220-L221) states the design rule plainly: *the
   exclusion constraint — not the pre-check above — is what actually decides.* The pre-check is a
   fast path and an error-message improver, never the authority.

6. **Participants** — host row first (`role=HOST`, from `get_full_name() or email`), then each
   supplied attendee ([bookings.py:58-76](../../core/services/bookings.py#L58-L76)).

7. **Audit** — `BookingEvent.CREATED` with `{status, source}`.

8. **Deferred side effects** — only for non-`HELD` bookings:
   ```python
   transaction.on_commit(lambda: notifications.send_confirmation(booking_id))
   transaction.on_commit(lambda: _enqueue_calendar_push(booking_id))
   ```
   A held slot deliberately sends nothing — the customer hasn't committed yet.

### 6.4 Deadlock retry

[`_with_conflict_retry`](../../core/services/bookings.py#L103-L116) retries up to
`_MAX_CONFLICT_RETRIES = 5` on `OperationalError` whose pgcode is `40P01` (deadlock detected) or
`40001` (serialization failure) — the two ways Postgres resolves a race on the exclusion
constraint. These are distinct from `IntegrityError`, which means a conflicting row is already
committed and retrying is pointless. After exhausting retries the slot is reported taken.

### 6.5 Concurrency summary

```
Request A ─┐
           ├─→ eligible_free_hosts (advisory)
Request B ─┘        │
                    ▼
            SELECT ... FOR UPDATE on host user   ← serialization point
                    │
                    ▼
            INSERT booking
                    │
        ┌───────────┴───────────┐
   success                  IntegrityError → 409 SlotConflictError
        │
   on_commit → email + calendar push
```

---

## 7. Hold → confirm

### 7.1 Hold

`POST /api/v1/core/bookings/hold/` → [`hold_slot`](../../core/services/bookings.py#L241-L265),
which is `create_booking` with `status=HELD`, `source=CUSTOMER`, and a TTL.

Because `HELD` is in `ACTIVE_BOOKING_STATUSES`, the hold **occupies the exclusion constraint** —
that is the entire point. Nobody else can take the slot until it is confirmed or lapses.

### 7.2 Confirm

`POST /api/v1/core/bookings/{id}/confirm/` → [`confirm_hold`](../../core/services/bookings.py#L378-L396):

- `SELECT ... FOR UPDATE` on the booking
- status must be `HELD` → else `InvalidTransitionError` (400)
- `hold_expires_at < now` → `SlotConflictError` (409)
- → `CONFIRMED`, `hold_expires_at = None`, log `CONFIRMED`
- on commit: confirmation email + calendar push

### 7.3 Expiry

Beat task `cleanup_expired_holds` runs **every 60 seconds** →
[`release_expired_holds`](../../core/services/bookings.py#L504-L523). Collects expired ids, then
re-checks each under `select_for_update` before flipping to `CANCELED` with reason
`"Hold expired"` and logging `HOLD_EXPIRED`. Idempotent by construction — safe to run repeatedly.

Note the expired hold becomes `CANCELED`, not deleted; it drops out of the exclusion
constraint because `CANCELED` is not an active status.

---

## 8. Collective bookings

For `assignment_strategy == COLLECTIVE`, `BookingViewSet._create` routes to
[`create_collective_booking`](../../core/services/bookings.py#L268-L303) — **one `Booking` row per
skilled host**, all sharing a `group_id`, all-or-nothing.

[`_create_collective_once`](../../core/services/bookings.py#L306-L375):

1. Every required host must pass `eligible_free_hosts` individually; any failure →
   `SlotConflictError("Not all required hosts are available at this time.")`.
2. Lock **all** hosts in deterministic `pk` order — ordering is what prevents GiST deadlocks
   between two collective bookings sharing hosts.
3. Insert each row; any `IntegrityError` aborts the whole meeting (the enclosing
   `transaction.atomic()` rolls back every row already inserted).
4. One `send_group_confirmation(group_id)` on commit, plus a calendar push per row.

Holds are **not** supported for COLLECTIVE — the AI bridge returns
`{"ok": False, "reason": "collective_no_hold"}` ([agent_scheduling.py:240-245](../../core/services/agent_scheduling.py#L240-L245))
and the REST hold path silently treats it as a DIRECT hold, which then fails serializer
validation because `target_host` is absent.

Cancelling any member cancels the group — [`cancel_group`](../../core/services/bookings.py#L432-L479),
routed by `if booking.group_id` in both `cancel` and `perform_destroy`.

---

## 9. Cancel, complete, no-show

### 9.1 Cancel

`POST /api/v1/core/bookings/{id}/cancel/` (body: `{"reason": "..."}`) →
[`cancel_booking`](../../core/services/bookings.py#L399-L429):

- `SELECT ... FOR UPDATE`
- already terminal (`COMPLETED | CANCELED | NO_SHOW | RESCHEDULED`) → `InvalidTransitionError`
- lead-time check, **skipped for `HELD`** and skipped when `enforce_lead=False`:
  `start_time − now < cancellation_lead_minutes` → `PolicyViolationError`
- → `CANCELED` + reason, log `CANCELED`
- on commit, **only if it wasn't a hold**: cancellation email + calendar delete

`REQUIRES_RESCHEDULE` is *not* terminal, so a booking awaiting reschedule can still be cancelled.

### 9.2 DELETE

`DELETE /api/v1/core/bookings/{id}/` → [`perform_destroy`](../../core/views/booking.py#L244-L253).
**DELETE is a cancellation, not a data deletion**, and it passes `enforce_lead=False` — internal
ops bypass the customer-facing lead-time rule.

### 9.3 Complete / no-show

`POST .../complete/` and `POST .../no-show/` → [`_terminal_transition`](../../core/services/bookings.py#L490-L501).
Blocked for terminal statuses **and** for `HELD`. No notification, no calendar side effect —
just the status flip and a `BookingEvent`.

---

## 10. Leave → reschedule engine

### 10.1 Trigger

`POST /api/v1/core/availability-exceptions/` creates an `AvailabilityException`. The `post_save`
signal ([signals.py:19-32](../../core/signals.py#L19-L32)) is intentionally thin — no queries, no
notifications — it only enqueues on commit:

```python
transaction.on_commit(lambda: reschedule_on_leave.delay(tenant_id, exception_id))
```

So the heavy work runs off the request path and never on a rolled-back transaction.

### 10.2 `handle_leave`

[core/services/reschedule.py:105-134](../../core/services/reschedule.py#L105-L134). Finds the leaving
user's bookings that overlap the leave window:

```python
Booking.objects.filter(
    tenant=tenant, primary_host=leave.user,
    status__in=(SCHEDULED, CONFIRMED),
    effective_start_time__lt=leave.end_time,
    effective_end_time__gt=leave.start_time,
)
```

`PROCESSABLE_STATUSES` excludes `REQUIRES_RESCHEDULE` — which is what makes re-runs idempotent,
since an already-flagged booking is simply skipped. It also excludes `HELD`, leaving transient
holds to expire on their own.

### 10.3 `_process_booking`

[reschedule.py:43-102](../../core/services/reschedule.py#L43-L102), per booking, under
`select_for_update`:

1. **Re-validate under the lock** — deleted, wrong status, host changed, or already started → `"skipped"`.
2. Mint `token = uuid4()`, `expires = now + BOOKING_RESCHEDULE_TOKEN_TTL_HOURS` (default 72h).
3. If `group_id` is set, flag **every** member of the group with the **shared** token — if one
   required host is out, the collective meeting can't happen.
4. Each row → `REQUIRES_RESCHEDULE` + token + expiry, and logs `RESCHEDULE_REQUESTED`
   with `{"reason": "leave:<TYPE>"}`.
5. On commit: one customer email with the tokenized link, plus an FCM push to tenant admins.

**Hosts are never swapped silently.** The docstring at
[reschedule.py:7-9](../../core/services/reschedule.py#L7-L9) records why: bookings name their host
(DIRECT) or need every skilled host (COLLECTIVE), so there is no pool to reassign into. Pooled
auto-reassignment was removed along with the `ROUND_ROBIN` strategy — the `REASSIGNED` enum
members in `BookingEvent` and `NotificationLog` are retained only so historical rows still render.

---

## 11. Public rebooking

**Endpoint:** `GET|POST /api/v1/core/public/reschedule/<uuid:token>/`
→ [`PublicRescheduleView`](../../core/views/public_reschedule.py#L32).
`authentication_classes = []`, `AllowAny`, throttled at **30/min** per anon client.

### 11.1 Token validation

[`_load_by_token`](../../core/services/rebooking.py#L26-L43) requires a booking that is
`REQUIRES_RESCHEDULE`, not deleted, with a matching token that hasn't expired. Anything else →
`InvalidRescheduleToken` (400). Because `rebook()` clears the token on success, the link is
genuinely single-use.

### 11.2 GET — context + slots

Clamps the requested date range to `[today, today + max_horizon_days]`, then calls
[`list_slots`](../../core/services/rebooking.py#L51-L70). Note the host scoping:

```python
target_host_id = None if COLLECTIVE else booking.primary_host_id
```

Scoping to the host `rebook()` will actually book prevents offering a slot that only a
*different* host is free for, which would 409 on submit.

Returns the original booking summary plus `[{start, end}]` — deliberately **without**
`eligible_host_ids`, since this is an unauthenticated surface.

### 11.3 POST — rebook

[`rebook`](../../core/services/rebooking.py#L89-L152), all inside one `transaction.atomic()`:

1. Re-load by token with `select_for_update`.
2. Gather old rows (whole group if `group_id`).
3. [`_collect_attendees`](../../core/services/rebooking.py#L73-L86) — de-duplicates non-host
   participants by email across all old rows.
4. Create the new booking — COLLECTIVE via `create_collective_booking`, DIRECT via
   `create_booking` with **the same `primary_host_id`**. Source is `PUBLIC_LINK`.
5. Retire each old row → `RESCHEDULED`, clear token + expiry, log `RESCHEDULED`, and enqueue a
   calendar delete for any row that had an `external_event_id`.

The new booking goes through the full `create_booking` path, so policy, host-skill, working-hours,
and the exclusion constraint all apply exactly as they do for a first-time booking.

---

## 12. External calendar sync

### 12.1 Outbound — mirroring bookings

[`push_booking_to_calendar`](../../core/tasks.py#L100-L131), enqueued on commit by create/confirm.
Idempotent by `external_event_id`: `update_event` when set, `push_event` (then store the returned
id + provider) when not. No-op when the host has no `CONNECTED` integration. Logs
`EXTERNAL_SYNCED`. Retries 3× / 30s on any exception.

[`delete_booking_calendar_event`](../../core/tasks.py#L134-L156), enqueued on cancel and on
rebook-retirement. No-op if never pushed; clears `external_event_id` after a successful delete.

`delete_calendar_event_for_host` ([tasks.py:159-187](../../core/tasks.py#L159-L187)) is explicitly
marked legacy — its only caller was leave-time auto-reassignment, removed with `ROUND_ROBIN`.
It is retained for one release so in-flight broker messages resolve.

### 12.2 Inbound — webhook sync

```
Google ──POST──► /api/v1/core/calendar-webhook/google/
                  ├─ validate X-Goog-Channel-ID + X-Goog-Channel-Token
                  ├─ ignore X-Goog-Resource-State == "sync"  (handshake ping)
                  └─ incremental_calendar_sync.delay(integration_id)
                                    │
                                    ▼
                     calendar_sync.run_incremental_sync
                                    │
                     provider.sync_events(integration, now, now+60d)
                                    │
                     ┌──────────────┴──────────────┐
                cancelled/no times            has times
                     │                             │
              delete ExternalBusyBlock      upsert ExternalBusyBlock
```

The webhook view **always returns 200**, even for unknown channels, so Google stops retrying
([calendar_webhook.py:15-19](../../core/views/calendar_webhook.py#L15-L19)).

The **echo guard** at [calendar_sync.py:34-46](../../core/services/calendar_sync.py#L34-L46) collects
the `external_event_id`s we ourselves pushed for that host and skips them on sync — otherwise
every booking we mirrored would come back as an external busy block and block its own slot.

After a successful pass: `sync_token` (the provider cursor), `last_synced_at`, and a cleared
`last_error` are persisted.

### 12.3 Watch lifecycle

- **On connect** — `establish_calendar_watch` calls `setup_watch` then seeds the cache with a
  full `run_incremental_sync`. No-op when `GOOGLE_CALENDAR_WEBHOOK_URL` is unset, which cleanly
  disables webhooks and falls back to on-demand free/busy.
- **`setup_watch`** ([calendar_sync.py:87-114](../../core/services/calendar_sync.py#L87-L114)) stops any
  existing channel, generates a fresh `secrets.token_urlsafe(24)` channel secret, and stores
  channel id / resource id / token / expiry.
- **Renewal** — beat task `renew_calendar_watches` hourly, for channels expiring within 6 hours.
- **Token refresh** — beat task `refresh_calendar_tokens` every 15 min, for access tokens expiring
  within 10 min. On failure the integration flips to `REAUTH_REQUIRED` with `last_error` set
  ([tasks.py:214-220](../../core/tasks.py#L214-L220)), which removes it from availability lookups.

---

## 13. Notifications & reminders

### 13.1 Design rule

Notifications are emitted **explicitly from the service layer**, never from a blanket `post_save`
on `Booking` ([notifications.py:1-9](../../core/services/notifications.py#L1-L9)). A signal would
double-fire on internal saves such as audit stamping.

### 13.2 `_emit` — the idempotency gate

[notifications.py:76-111](../../core/services/notifications.py#L76-L111):

```python
log, created = NotificationLog.objects.get_or_create(tenant=..., dedup_key=..., defaults={...})
if not created and log.status == SENT: return False
send_scheduling_email_task.delay(...)
log.status, log.sent_at = SENT, now()
events.log_event(booking, NOTIFICATION_SENT, metadata={"type": ...})
return True
```

Returns `True` **iff it actually enqueued this call** — that boolean is what the reminder
scanner counts.

| Notification | Dedup key | Recipients |
|---|---|---|
| Confirmation | `{booking.id}:CONFIRMATION` | all participants (incl. host) |
| Cancellation | `{booking.id}:CANCELED` | all participants |
| Reschedule request | `{booking.id}:RESCHEDULE_REQUEST:{token}` | **customers only** (host excluded) |
| Reminder | `{booking.id}:REMINDER:{offset}` | all participants |
| Group confirm/cancel | `group:{group_id}:{type}` | union across group rows |

### 13.3 `.ics` invites

[core/services/ics.py](../../core/services/ics.py) — a dependency-free RFC 5545 builder.
`METHOD:REQUEST` + `STATUS:CONFIRMED` for confirmations and reminders; `METHOD:CANCEL` +
`STATUS:CANCELLED` for cancellations. `UID` is the booking id (or `group-{group_id}`), so mail
clients thread updates onto the same event.

### 13.4 Reminders

Beat task `send_due_reminders` every 5 minutes →
[`reminders.send_due_reminders`](../../core/services/reminders.py#L16-L39):

```python
offsets = sorted(BOOKING_REMINDER_OFFSETS_MINUTES)          # default [60, 1440]
upcoming = bookings where SCHEDULED|CONFIRMED and now < start <= now + max(offsets)
for booking in upcoming:
    minutes_until = (start - now).minutes
    for offset in offsets:            # ascending
        if minutes_until <= offset:
            send_reminder(booking, offset); break
```

Ascending order + `break` means the **nearest** offset whose window has been entered wins, and
`NotificationLog` guarantees one email per `(booking, offset)` regardless of how often the scan runs.

### 13.5 Push

`send_push_to_admins_task` / `send_push_to_user_task` wrap
[core/services/push.py](../../core/services/push.py) (FCM). Currently the only booking-flow caller is
the reschedule engine, which alerts tenant admins that a booking needs rescheduling.

---

## 14. AI-agent booking path

The voicebot books through [`core/services/agent_scheduling.py`](../../core/services/agent_scheduling.py),
a thin guardrailed wrapper that **does not change the engine**. Two invariants on every call
([agent_scheduling.py:1-15](../../core/services/agent_scheduling.py#L1-L15)):

1. **Tenant** always comes from `agent.tenant` and is passed explicitly — there is no request
   context in the AI runtime.
2. **Guardrail** — an agent may only act on templates in its own `event_templates` M2M.
   [`_resolve_template`](../../core/services/agent_scheduling.py#L40-L53) scopes through that relation,
   which enforces both the guardrail and tenant isolation in one query, and additionally
   requires `is_active=True`.

Every function returns a structured, LLM-narratable result and **never raises domain exceptions
into the conversation** — [`_error_result`](../../core/services/agent_scheduling.py#L85-L102) maps them:

| Exception | Result |
|---|---|
| `SlotConflictError` | `{"ok": false, "reason": "slot_taken", "retry": true}` |
| `NoEligibleHostError` | `{"ok": false, "reason": "no_host_available"}` |
| `PolicyViolationError` | `{"ok": false, "reason": "policy_violation", "field": ...}` |
| `InvalidTransitionError` | `{"ok": false, "reason": "invalid_state"}` |
| `TemplateNotAllowed` | `{"ok": false, "reason": "template_not_allowed"}` |

Anything else re-raises — unknown failures are not swallowed into the conversation.

Bookings are tagged `Source.AI` with `actor=None`; origin is captured by the source tag plus the
`BookingEvent` log. Cancellations from the AI use `enforce_lead=False` (internal-actor semantics).

[`agent_scheduling_tools.py`](../../core/services/agent_scheduling_tools.py) exposes seven
function-calling tools in the Anthropic `tools` shape — `list_event_types`, `list_event_hosts`,
`check_availability`, `hold_slot`, `confirm_booking`, `book_meeting`, `cancel_booking`. The
**agent is not a tool argument**; it is bound by `call_tool(agent, name, arguments)`, which also
parses date/datetime strings and returns `{"ok": false, "reason": "bad_arguments"}` on
`KeyError | ValueError | TypeError`.

Typical conversation shape:

```
list_event_types → check_availability(date_from, date_to)
                 → [pick a slot + one of its host_ids]
                 → hold_slot(..., target_host_id)      ← reserves it during the call
                 → [collect name/email]
                 → confirm_booking(booking_id)
```

---

## 15. Tenancy & authorization

### 15.1 Human callers

`TenantBaseViewSet.get_queryset` filters every queryset to `user.tenant_mapping.tenant`, and
`perform_create` injects that tenant on write ([tenant_base_viewset.py](../../tenant/common/views/tenant_base_viewset.py)).
Superusers bypass both. No mapping → `PermissionDenied`.

### 15.2 Machine (`VB_AGENT`) callers

`VB_AGENT` users have **no** `TenantUserMapping` — the role is global by design — so they name
their tenant with the **mandatory `X-Tenant-ID` header**
([agent_tenant_scope.py](../../tenant/common/views/agent_tenant_scope.py)). Omitting it is a 400, never a
global read.

Read the caveat in that module's docstring carefully: `AgentToken` carries no tenant, so the
header is a **filter, not an authorization check**. It stops a misconfigured runner from touching
the wrong tenant and makes scope auditable, but it does not contain a leaked token.

`BookingViewSet.vb_agent_allowed_actions` grants machine callers the full lifecycle —
`list, retrieve, create, hold, confirm, cancel, complete, no_show, destroy`
([booking.py:50-60](../../core/views/booking.py#L50-L60)) — because booking *is* the voicebot's job.
`AvailabilityViewSet` grants only `available_slots`.

Both the viewset's `_tenant()` and its `get_queryset()` resolve the tenant the same way, which is
load-bearing: if they disagreed, a create would land in a tenant the caller cannot read back.

---

## 16. Async surface — tasks & beat schedule

| Task | Trigger | Retries |
|---|---|---|
| `send_scheduling_email_task` | on_commit from notifications | 3 × 30s |
| `push_booking_to_calendar` | on_commit from create/confirm | 3 × 30s |
| `delete_booking_calendar_event` | on_commit from cancel/rebook | 3 × 30s |
| `reschedule_on_leave` | `post_save` signal on `AvailabilityException` | 3 × 30s |
| `incremental_calendar_sync` | Google webhook | 3 × 30s |
| `establish_calendar_watch` | after OAuth connect | none (warns) |
| `send_push_to_admins_task` | reschedule engine | none |
| `materialize_profile_task` | on_commit from an availability-profile write | 3 × 30s |

Every task uses `base=ReportedTask`, whose `on_failure` emits one structured ERROR to the
`core.tasks.failures` logger after retries are exhausted. That logger is the alerting hook for
permanent async failure.

Beat schedule ([config/settings/base.py](../../config/settings/base.py)):

| Task | Interval | Notes |
|---|---|---|
| `cleanup_expired_holds` | 60 s | ≤500 holds per run, isolated per hold |
| `send_due_reminders` | 300 s | streamed with `.iterator()`, isolated per booking |
| `refresh_calendar_tokens` | 900 s | |
| `renew_calendar_watches` | 3600 s | |
| `materialize_availability_horizon` | 86400 s | rolls the window horizon forward, purges windows that ended >1 day ago. **Not optional** — see §20.1 |
| `start_due_campaigns` | 60 s | |
| `cleanup_expired_contact_imports` | 600 s | |

---

## 17. Status machine

```
                    ┌──────────────────────────────┐
   hold ──────────► │            HELD              │
                    └───┬──────────────────────┬───┘
                        │ confirm              │ TTL lapse (beat)
                        ▼                      ▼
   create ─────► ┌────────────┐            CANCELED
                 │ SCHEDULED  │◄──── rebook (new row)
                 └─────┬──────┘
                       │ confirm_hold ──► CONFIRMED
                       │
     ┌─────────────────┼─────────────────┬──────────────────┐
     ▼                 ▼                 ▼                  ▼
 COMPLETED         CANCELED           NO_SHOW      REQUIRES_RESCHEDULE
                                                            │ rebook
                                                            ▼
                                                       RESCHEDULED
```

- **Active** (occupy the exclusion constraint): `HELD, SCHEDULED, CONFIRMED, REQUIRES_RESCHEDULE`
- **Terminal** (`TERMINAL_STATUSES`, [bookings.py:35-40](../../core/services/bookings.py#L35-L40)):
  `COMPLETED, CANCELED, NO_SHOW, RESCHEDULED`

---

## 18. Error → HTTP mapping

[`BookingViewSet._run`](../../core/views/booking.py#L83-L101) wraps every service call:

| Exception | HTTP | Body |
|---|---|---|
| `PolicyViolationError` | 400 | `errors: {<exc.field or "detail">: [msg]}` |
| `NoEligibleHostError` | 400 | `message` |
| `InvalidTransitionError` | 400 | `message` |
| `SlotConflictError` | **409** | `message` |
| DRF `ValidationError` | 400 | standard DRF envelope |
| `PermissionDenied` (tenant) | 403 | `message` |

The public reschedule view maps the same set plus `InvalidRescheduleToken` → 400
([public_reschedule.py:116-123](../../core/views/public_reschedule.py#L116-L123)).

---

## 19. Configuration surface

[config/settings/base.py:448-455](../../config/settings/base.py#L448-L455) and
[:515-531](../../config/settings/base.py#L515-L531):

| Setting | Default | Effect |
|---|---|---|
| `AVAILABILITY_MATERIALIZE_DAYS` | 90 | Days of UTC windows kept ahead of today. Raised automatically to cover any `EventTemplate.max_horizon_days` the tenant advertises, hard-capped at 365 |
| `BOOKING_HOLD_TTL_SECONDS` | 300 | Hold lifetime |
| `BOOKING_REMINDER_OFFSETS_MINUTES` | `[1440, 60]` | Reminder trigger points |
| `BOOKING_RESCHEDULE_TOKEN_TTL_HOURS` | 72 | Public reschedule link lifetime |
| `EXTERNAL_FREEBUSY_CACHE_SECONDS` | 60 | Live free/busy cache TTL |
| `SCHEDULING_TOKEN_ENCRYPTION_KEY` | `""` | Fernet key for OAuth tokens |
| `GOOGLE_CALENDAR_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | `""` | OAuth |
| `GOOGLE_CALENDAR_WEBHOOK_URL` | `""` | **Blank disables webhooks**, falls back to on-demand free/busy |
| `FRONTEND_PROJECT_URL` | — | Base for the `/reschedule/{token}` link |

---

## 20. Known gaps & sharp edges

Observations from reading the current code. None are speculative — each names the line that
produces the behaviour.

### 20.1 Stale materialized windows narrow availability silently

The cost of moving availability into `AvailabilityWindow` (§2.4): it is now **eventually consistent
with a horizon**. A rule edit is correct immediately via the post-commit rebuild, but if
`materialize_availability_horizon` stops running, availability quietly shrinks as the existing
horizon is consumed — no error, just fewer slots each day.

The mitigation is a canary, not a guarantee: `get_available_slots` and `eligible_free_hosts` log
ERROR `materialization is stale or failed` when a host has rules and no future windows at all.
**Alert on that line.** Recovery is `manage.py materialize_availability`.

### 20.2 The original slot is blocked during reschedule

`REQUIRES_RESCHEDULE` is an active status, so a flagged booking keeps occupying the exclusion
constraint and keeps appearing in `_busy_intervals`. A customer following the reschedule link
therefore cannot pick the original time — even if the leave that caused it was subsequently
cancelled. Self-consistent (the engine won't offer it either), but worth knowing.

### 20.3 Notification bodies are UTC-only

[`_body`](../../core/services/notifications.py) renders `"%Y-%m-%d %H:%M UTC"`. The customer's
timezone is available on neither the participant nor the booking, so emails always show UTC even
though the whole engine is timezone-aware. The building block now exists —
`Timezone.offset_label_at()` (§2.4) — but nothing captures the customer's zone to render with.

### 20.4 No invariant-audit command

There is no `manage.py audit_bookings` to assert the §3 invariants against live data (orphan
participants, `effective_*` drift from buffers, `HELD` rows past expiry, windows whose rule no
longer exists). Every invariant is enforced at write time, so this is monitoring rather than a
fix — but nothing currently detects a row that got in another way.

### 20.5 Legacy surface awaiting removal

- `delete_calendar_event_for_host` ([tasks.py](../../core/tasks.py)) — retained one release for
  in-flight broker messages.
- `BookingEvent.REASSIGNED` and `NotificationLog.REASSIGNED` — no longer written; kept so
  historical rows render.

### 20.6 Recently closed

Listed because earlier revisions of this document described them as open:

| Was | Now |
|---|---|
| Overnight shifts not representable | §2.3 wrap semantics + §2.4 materialization |
| `is_active` inconsistently honoured | materialization skips inactive profiles; `BookingCreateSerializer` filters `is_active=True` |
| Buffers differed between the two availability entry points | both build one `busy` list and test it against `eff_*` |
| `NotificationLog` marked `SENT` at enqueue | row stays `PENDING`; the task writes `SENT`/`FAILED` |
| `.ics` `SEQUENCE` always 0 | derived from the `BookingEvent` revision count |
| External-calendar failure silently widened availability | browsing flags the slot `external_check_failed`; committing raises `ExternalCalendarUnavailable` |

---

## Appendix — endpoint index

| Method | Path | Auth | Handler |
|---|---|---|---|
| `GET` | `/api/v1/core/availability/available-slots/` | Bearer (+ `X-Tenant-ID` for VB_AGENT) | `AvailabilityViewSet.available_slots` |
| `GET` | `/api/v1/core/bookings/` | Bearer | `BookingViewSet.list` |
| `GET` | `/api/v1/core/bookings/{id}/` | Bearer | `BookingViewSet.retrieve` |
| `POST` | `/api/v1/core/bookings/` | Bearer | `BookingViewSet.create` |
| `POST` | `/api/v1/core/bookings/hold/` | Bearer | `BookingViewSet.hold` |
| `POST` | `/api/v1/core/bookings/{id}/confirm/` | Bearer | `BookingViewSet.confirm` |
| `POST` | `/api/v1/core/bookings/{id}/cancel/` | Bearer | `BookingViewSet.cancel` |
| `POST` | `/api/v1/core/bookings/{id}/complete/` | Bearer | `BookingViewSet.complete` |
| `POST` | `/api/v1/core/bookings/{id}/no-show/` | Bearer | `BookingViewSet.no_show` |
| `DELETE` | `/api/v1/core/bookings/{id}/` | Bearer | cancel, `enforce_lead=False` |
| `GET`/`POST` | `/api/v1/core/public/reschedule/{token}/` | **none**, 30/min | `PublicRescheduleView` |
| `POST` | `/api/v1/core/calendar-webhook/google/` | channel token | `GoogleCalendarWebhookView` |

Related reading: [2. Booking Lifecycle](02-booking-lifecycle.md) ·
[3. Leave & Reschedule](03-leave-and-reschedule.md) ·
[4. Google Calendar Sync](04-google-calendar-sync.md) ·
[6. AI Agent Booking](06-ai-agent-booking.md)

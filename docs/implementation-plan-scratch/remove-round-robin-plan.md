# Remove the ROUND_ROBIN assignment strategy — implementation plan

**Status:** ✅ implemented (2026-07-31) — 402/402 tests pass on Postgres.

Deviations from the plan as written, all decided during implementation:

- **O1 adopted** — `GET /available-slots` now requires `target_host` for `DIRECT` and rejects it for `COLLECTIVE`.
- **O2 adopted** — added a `list_event_hosts` AI tool, and `check_availability` now returns `host_ids` per slot so the voice flow can still answer "when is anyone free?" and then book a named host.
- **`UserEventSkill.weight` removed** (not in the original plan). Its only documented purpose was round-robin bias and no code read it; dropped in the same migration, along with its serializer/admin/API-doc entries.
- **Bug found and fixed while testing:** `_resolve_host_ids` returned the caller's raw `target_host_id`, so a string UUID never matched the `UUID`-keyed profile map and produced a spurious `409`. It now returns the canonical `UUID`. This was latent before — the AI tool layer passes JSON strings and previously relied on pooled assignment. Covered by `test_target_host_accepts_string_uuid`.
- **`schema.yml` NOT regenerated.** The committed file is stale independently of this work — it contains no `assignment_strategy` at all, and regenerating adds ~7.7k unrelated lines. Left for a separate change.

## 1. Decisions (locked)

| # | Decision |
|---|---|
| D1 | `ROUND_ROBIN` is deleted from `EventTemplate.AssignmentStrategy`. Only `COLLECTIVE` and `DIRECT` remain. |
| D2 | New model default is `DIRECT`. |
| D3 | A data migration rewrites every existing `assignment_strategy='ROUND_ROBIN'` row to `'DIRECT'`. |
| D4 | `target_host` becomes **required** on every non-COLLECTIVE booking. Callers that omit it get a `400`. This is a deliberate breaking API change. |
| D5 | Pooled auto-assignment disappears: `_order_hosts_by_load` (least-loaded selection) is deleted. |
| D6 | Leave-time **auto-reassignment disappears**. Every overlapping booking is flagged `REQUIRES_RESCHEDULE` and the customer gets the self-serve link. |

### Consequences worth stating up front

- **Load balancing is gone.** Nothing distributes work across a host pool any more; whoever the caller names gets the booking.
- **Leave handling degrades from "silent fix" to "ask the customer".** Today a `ROUND_ROBIN` booking whose host calls in sick moves to a free colleague without bothering the customer. After this change every such booking emails the customer a reschedule link. That is a real product regression — it is the direct consequence of D1/D6, not an accident of the implementation. Flagging it so it is a choice, not a surprise.
- **`NotificationLog.REASSIGNED` and `BookingEvent.REASSIGNED` become write-dead but must stay in the enums** — historical rows reference them and `get_..._display()` would break on read.

---

## 2. Blast radius

| Layer | Files |
|---|---|
| Model + migration | [event_template.py](../../core/models/event_template.py), new `core/migrations/0037_*` |
| Services | [bookings.py](../../core/services/bookings.py), [reschedule.py](../../core/services/reschedule.py), [availability.py](../../core/services/availability.py), [rebooking.py](../../core/services/rebooking.py), [agent_scheduling.py](../../core/services/agent_scheduling.py), [agent_scheduling_tools.py](../../core/services/agent_scheduling_tools.py), [notifications.py](../../core/services/notifications.py) |
| Serializers | [booking.py](../../core/serializers/booking.py), [availability.py](../../core/serializers/availability.py) |
| Views | [availability.py](../../core/views/availability.py) |
| Tests | `core/tests/scheduling/` — 12 of 14 files touch `create_booking`/`make_template` |
| Docs | 4 API docs, 6 flow docs, 3 plan/checklist docs, `schema.yml` |

---

## 3. Phase 1 — Model + data migration

### 3.1 `core/models/event_template.py`

```python
class AssignmentStrategy(models.TextChoices):
    COLLECTIVE = "COLLECTIVE", "Collective"
    DIRECT = "DIRECT", "Direct"
```

and [event_template.py:60](../../core/models/event_template.py#L60) → `default=AssignmentStrategy.DIRECT`.

### 3.2 `core/migrations/0037_remove_round_robin_strategy.py`

Two operations, **data first**:

```python
def forwards(apps, schema_editor):
    EventTemplate = apps.get_model("core", "EventTemplate")
    EventTemplate.objects.filter(assignment_strategy="ROUND_ROBIN").update(
        assignment_strategy="DIRECT"
    )

operations = [
    migrations.RunPython(forwards, migrations.RunPython.noop),
    migrations.AlterField(model_name="eventtemplate", name="assignment_strategy", ...),
]
```

Two traps:

1. **Soft-deleted rows must be migrated too.** `EventTemplate.objects` is `ExceptDeletedManager` ([base_model_core.py](../../utils/models/base_model_core.py)), which filters `is_deleted=False`. Historical models from `apps.get_model` get a plain `models.Manager` because neither manager sets `use_in_migrations`, so the snippet above *does* cover soft-deleted rows — but that is load-bearing and non-obvious. Add a comment saying so, and do **not** switch it to `_default_manager` "for correctness".
2. **The reverse is a genuine one-way door.** Once rows are `DIRECT` there is no way to tell which were originally `ROUND_ROBIN`. `RunPython.noop` as the reverse is correct; note the irreversibility in the migration docstring. If a rollback path matters, dump `id`s to a file in `forwards` first.

`assignment_strategy` is a plain `varchar` with no DB-level check constraint, so the `AlterField` is a no-op state change in Postgres. No table rewrite, no downtime.

---

## 4. Phase 2 — Booking service

### 4.1 `core/services/bookings.py`

| Change | Location |
|---|---|
| Delete `_order_hosts_by_load()` | [bookings.py:58-70](../../core/services/bookings.py#L58-L70) |
| Delete `from collections import Counter` | [bookings.py:15](../../core/services/bookings.py#L15) |
| Delete `horizon = ...` and `ordered = ...` | [bookings.py:198-199](../../core/services/bookings.py#L198-L199) |
| Collapse the host loop to a single attempt | [bookings.py:214-243](../../core/services/bookings.py#L214-L243) |
| Rewrite module docstring (it names ROUND_ROBIN) | [bookings.py:7-8](../../core/services/bookings.py#L7-L8) |

Guard at the top of `create_booking` so the service defends itself rather than relying on the serializer:

```python
if event_template.assignment_strategy != EventTemplate.AssignmentStrategy.COLLECTIVE \
        and not target_host_id:
    raise NoEligibleHostError("This event type requires a specific host.")
```

`NoEligibleHostError` already maps to `400` in [booking.py:60](../../core/views/booking.py#L60) and to `no_host_available` in the AI bridge, so no new error plumbing is needed.

The single-attempt body keeps everything that makes concurrency safe — only the fallback loop goes:

```python
host_id = free[0]
with transaction.atomic():
    try:
        CustomUser.objects.filter(pk=host_id).select_for_update().get()
        booking = Booking.objects.create(...)
    except IntegrityError:
        raise SlotConflictError("The selected time is no longer available.")
    ...
```

Keep `_with_conflict_retry` and the `select_for_update` lock: the GiST exclusion constraint and the 40001/40P01 retry path are still exercised by two callers racing the *same* host — which is now the only race that exists. `test_concurrency.py` already pins the target host, so it covers the new shape unchanged.

**Side benefit:** one fewer query per booking (the `Counter` load query over the whole horizon window is gone).

### 4.2 Close the target-host validation gap — required, not optional

[availability.py:192](../../core/services/availability.py#L192) currently does:

```python
host_ids = [target_host_id] if target_host_id else skilled_host_ids(tenant, event_template)
```

A supplied `target_host_id` **bypasses the `UserEventSkill` check entirely**. Today that is partly masked because `ROUND_ROBIN` is the default and most bookings go through the skilled pool. After this change `target_host` is the *only* host-selection input on every non-collective booking, so the gap becomes the main path: any user with an `AvailabilityProfile` can be booked as host for any event type.

Fix in both `eligible_free_hosts` and `get_available_slots` ([availability.py:237](../../core/services/availability.py#L237)):

```python
skilled = skilled_host_ids(tenant, event_template)
if target_host_id:
    skilled_str = {str(h) for h in skilled}
    host_ids = [target_host_id] if str(target_host_id) in skilled_str else []
else:
    host_ids = skilled
```

The `str()` normalization matters — `values_list` yields `UUID`, callers pass either `UUID` or `str`.

Knock-on: `_create_collective_once` ([bookings.py:341-345](../../core/services/bookings.py#L341-L345)) calls `eligible_free_hosts(..., target_host_id=host_id)` per host, with ids that came from `skilled_host_ids` — still passes, at the cost of one extra skill query per host. Acceptable; optimise later if collective templates grow large.

Also tighten the serializer: `target_host` is `PrimaryKeyRelatedField(queryset=CustomUser.objects.all())` ([booking.py:95-97](../../core/serializers/booking.py#L95-L97)) — *any* user in the system, cross-tenant included. `validate()` already resolves `tenant`; add a tenant + skill check there so the caller gets a field-level `400` instead of a generic conflict.

---

## 5. Phase 3 — Reschedule engine

`core/services/reschedule.py` loses its entire first branch.

| Delete | Reason |
|---|---|
| `_try_reassign()` [81-130](../../core/services/reschedule.py#L81-L130) | the round-robin path |
| `_reassign_host_participant()` [61-78](../../core/services/reschedule.py#L61-L78) | only caller was `_try_reassign` |
| `_enqueue_calendar_push()` [37-40](../../core/services/reschedule.py#L37-L40) | only caller was `_try_reassign` |
| `_enqueue_calendar_delete_for_host()` [43-46](../../core/services/reschedule.py#L43-L46) | ditto |
| `_enqueue_push_user()` [49-52](../../core/services/reschedule.py#L49-L52) | ditto |
| strategy branch in `_process_booking` [149-170](../../core/services/reschedule.py#L149-L170) | every booking now falls through to the flag path |

Then:

- `handle_leave` result drops the `"reassigned"` key → `{"flagged": 0, "skipped": 0}`. Only consumer is the `reschedule_on_leave` task return value ([tasks.py:67](../../core/tasks.py#L67)) and the tests.
- Rewrite the module docstring [1-13](../../core/services/reschedule.py#L1-L13) — it currently documents the two-outcome design.
- `delete_calendar_event_for_host` in [tasks.py:160-185](../../core/tasks.py#L160-L185) becomes unreferenced. **Keep the task** (removing a Celery task that may have in-flight messages on the broker is a deploy hazard) but update its docstring to say it is retained for compatibility. Delete it in a follow-up release.
- `notifications.send_reassignment()` [144-159](../../core/services/notifications.py#L144-L159) becomes dead → delete the function, **keep** `NotificationLog.NotificationType.REASSIGNED` and `BookingEvent.EventType.REASSIGNED` with a `# legacy: no longer written` comment.

---

## 6. Phase 4 — API surface

### 6.1 `core/serializers/booking.py` [121-129](../../core/serializers/booking.py#L121-L129)

```python
if (event_template.assignment_strategy
        != EventTemplate.AssignmentStrategy.COLLECTIVE
        and not attrs.get("target_host")):
    raise serializers.ValidationError(
        {"target_host": "This event type requires a specific host."}
    )
```

Plus the tenant/skill checks from §4.2.

### 6.2 `core/serializers/availability.py` — decision needed, see §9

You have uncommitted work here whose `help_text` reads *"Recommended for DIRECT templates, but not enforced — if omitted, the engine considers all skilled hosts regardless of strategy."* That sentence stops being merely loose and becomes a footgun: `available-slots` would return the union of every skilled host's free time, but booking any of those slots requires naming a host who may not be the one that slot was free for → surprise `409`. Recommendation is to require `target_host` for non-COLLECTIVE queries.

### 6.3 Other API-layer edits

- [views/availability.py:36-40](../../core/views/availability.py#L36-L40) — the `@extend_schema` description (uncommitted) names ROUND_ROBIN; rewrite to the two-strategy behaviour.
- [rebooking.py:107-113](../../core/services/rebooking.py#L107-L113) — the ternary collapses to `target_host_id = booking.primary_host_id` unconditionally.
- [views/event_template.py:21](../../core/views/event_template.py#L21) and [admin/event_template_admin.py](../../core/admin/event_template_admin.py) — no code change; the filter choices follow the model.
- Regenerate `schema.yml`.

### 6.4 AI agent bridge — highest-risk surface

[agent_scheduling_tools.py:33-36](../../core/services/agent_scheduling_tools.py#L33-L36) tells the LLM:

> *"Optional. Pin a specific host — only for DIRECT event types or when the caller names a person. **Omit to let the system auto-assign a free host.**"*

That instruction is now actively wrong — following it produces a guaranteed failure on every booking. Required edits:

1. Rewrite `_TARGET_HOST_ID`'s description: required for non-collective meetings, omit only for collective.
2. Add `"target_host_id"` to the `required` list for `book_meeting` [111](../../core/services/agent_scheduling_tools.py#L111) and `hold_slot` [83](../../core/services/agent_scheduling_tools.py#L83) — but note this is wrong for COLLECTIVE templates, which take no host. Since one schema serves both strategies, keep it out of `required` and instead make the *description* unambiguous, letting the service return a structured error.
3. There is no tool for "list the hosts who can take this meeting". The LLM now must supply a host id it has no way to obtain — `list_event_types` returns only template id/name/duration/strategy. **A `list_hosts(event_template_id)` tool is a hard prerequisite**, not a nice-to-have, or voice booking breaks completely.
4. `agent_scheduling.book()`/`hold()` — add an explicit guard returning `{"ok": False, "reason": "host_required", "message": ...}` so the model gets an actionable reason instead of a generic `no_host_available`.

---

## 7. Phase 5 — Tests

Run everything with Postgres — the default SQLite test DB is broken on `ArrayField`:

```
python manage.py test core.tests.scheduling --settings=config.settings.test_postgres
```

### 7.1 One-line, high-leverage change

[factories.py:70](../../core/tests/scheduling/factories.py#L70) → `assignment_strategy=EventTemplate.AssignmentStrategy.DIRECT`. All 50 `make_template` call sites inherit it.

### 7.2 Tests that must be rewritten, not just re-run

| Test | Action |
|---|---|
| `test_engine.py::test_round_robin_fallback_then_full` [97](../../core/tests/scheduling/test_engine.py#L97) | delete; replace with `test_second_booking_same_host_conflicts` + `test_target_host_required` |
| `test_reschedule.py::test_leave_reassigns_round_robin` [39](../../core/tests/scheduling/test_reschedule.py#L39) | delete |
| `test_reschedule.py::test_direct_strategy_never_auto_reassigns` [67](../../core/tests/scheduling/test_reschedule.py#L67) | rename → `test_leave_always_flags_for_reschedule`; becomes the canonical case |
| `test_reschedule.py::test_reassignment_skips_host_also_on_leave` [83](../../core/tests/scheduling/test_reschedule.py#L83) | delete |
| `test_reschedule.py::test_reassignment_never_crosses_tenant` [96](../../core/tests/scheduling/test_reschedule.py#L96) | **keep the tenant-isolation intent** — retarget at `eligible_free_hosts` / `available-slots` so cross-tenant coverage does not silently vanish with the reassign path |
| `test_reschedule.py::test_idempotent_reassign` [130](../../core/tests/scheduling/test_reschedule.py#L130) | rework as `test_idempotent_flag` (re-run must not double-notify) |
| `test_reschedule.py` signal test [162](../../core/tests/scheduling/test_reschedule.py#L162) | assert flagged, not reassigned |
| `test_reschedule.py::test_reassignment_resyncs_calendars` [165](../../core/tests/scheduling/test_reschedule.py#L165) | delete |
| `test_push.py::test_reassignment_pushes_new_host` [100](../../core/tests/scheduling/test_push.py#L100) | replace with the admin-push-on-flag case |
| all `handle_leave` assertions | drop the `"reassigned"` key |

### 7.3 New tests to add

- `create_booking` without `target_host_id` on a DIRECT template → `NoEligibleHostError`.
- `create_booking` with a `target_host_id` that has **no `UserEventSkill`** → `NoEligibleHostError` (regression guard for §4.2).
- `create_booking` with a `target_host_id` from **another tenant** → rejected.
- `POST /bookings` without `target_host` → `400` with a `target_host` field error.
- Migration test: an `EventTemplate` row (including a soft-deleted one) with `ROUND_ROBIN` lands on `DIRECT`.

### 7.4 Mechanical sweep

49 call sites across 12 files reference `create_booking`/`hold_slot`/`target_host`. Every one that omits `target_host_id` now raises. Files needing a pass: `test_availability.py` (7), `test_engine.py` (10), `test_calendar.py` (6), `test_notifications.py` (4), `test_push.py` (4), `test_reminders.py` (4), `test_reschedule.py` (4), `test_webhook_sync.py` (3), `test_rebooking.py` (2), `test_agent_scheduling.py`, `test_concurrency.py` (already pins a host), `test_collective.py` (unaffected).

---

## 8. Phase 6 — Docs

**Rewrite** (describe current behaviour):
`docs/apiDocs/core/booking.md`, `event_template.md`, `availability_slots.md`, `availability_exception.md` (line 5 promises ROUND_ROBIN auto-reassign); `docs/flows/01-setup.md`, `02-booking-lifecycle.md`, `03-leave-and-reschedule.md` (both mermaid diagrams have a reassign branch), `05-notifications-and-push.md` (the "Booking reassigned" notification row), `06-ai-agent-booking.md`, `booking-v4.md`.

**Do not rewrite — add a superseded note:**
`docs/scheduling-booking-implementation-plan-v4.md`, `docs/ai-agent-scheduling-plan.md`, `docs/scheduling-deployment-checklist.md`. These are historical design records; a dated "ROUND_ROBIN removed, see this plan" banner at the top preserves the record.

---

## 9. Open decisions *(all resolved — see the status block at the top)*

| # | Question | Recommendation |
|---|---|---|
| O1 | Require `target_host` on `GET /available-slots` for non-COLLECTIVE templates? | **Yes** — otherwise the endpoint returns slots the caller cannot reliably book (§6.2). Small extra step: it makes the endpoint's contract strategy-dependent. |
| O2 | Add a `list_hosts(event_template_id)` AI tool? | **Yes, blocking.** Without it the voice agent cannot produce the now-required host id (§6.4). |
| O3 | Accept `"ROUND_ROBIN"` on write for one release and coerce to `DIRECT`? | **No** — D4 already accepts a breaking change; a silent coercion hides the migration from API clients. Let it `400` on the choices validator. |
| O4 | Delete `delete_calendar_event_for_host` now? | **No** — leave one release for in-flight broker messages. |

## 10. Sequencing

1. Phase 1 (model + migration) — mergeable alone, no behaviour change beyond the default.
2. Phases 2–4 together — these are one behavioural change and should not be split across deploys.
3. Phase 5 in the same PR as 2–4 (the suite does not pass in between).
4. Phase 6 can trail by a commit.

Resolve **O2 before** Phase 4 ships, or AI booking regresses to unusable.

## 11. Rollback

The migration's data step is one-way (§3.2). Rolling back code without restoring data leaves every ex-ROUND_ROBIN template as `DIRECT` — bookings keep working (they just require a host) but pooled assignment does not return on its own. If a true rollback is a requirement, have `forwards()` write the affected template ids to a `RunPython`-created audit row or a file first.

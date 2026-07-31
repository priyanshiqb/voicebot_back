# AI Agent ↔ Scheduling Bridge — Implementation Plan

> ⚠️ **Superseded in part (2026-07-31): `ROUND_ROBIN` was removed.** Pooled
> auto-assignment and leave-time auto-reassignment no longer exist — every
> non-`COLLECTIVE` booking names its host, and a host's leave always flags the
> booking `REQUIRES_RESCHEDULE`. This document is kept as the historical design
> record; see [`implementation-plan-scratch/remove-round-robin-plan.md`](implementation-plan-scratch/remove-round-robin-plan.md).

**Status:** 📝 PLANNED
**Goal:** Let an **AI agent** book / hold / cancel meetings for hosts during a conversation —
scoped to exactly what that agent is configured to book, tenant-isolated, and reusing the
existing scheduling engine with **no changes to booking/availability core logic**.

> **Where the link lives (decision):** on the concrete **`Agent`** model, **not** `AgentTemplate`.
> An agent instance is configured with the meeting types it may book; host selection then falls
> out of each event template's `assignment_strategy` + `UserEventSkill`.

---

## 0. Design principles

1. **Agent → EventTemplate is a many-to-many**, mirroring the existing `Agent.knowledge_bases`
   and `Agent.probing_questions` M2Ms. The agent is configured with *which meeting types it can
   book* — not with hosts.
2. **Host assignment is NOT stored on the agent.** It stays in `EventTemplate.assignment_strategy`
   (`DIRECT` / `ROUND_ROBIN` / `POOLED` / `COLLECTIVE`) + `UserEventSkill` (who's eligible). The
   agent books an event template; the engine picks the host.
3. **Tenant comes from the agent.** `Agent` is a `TenantBaseModel`, so `agent.tenant` scopes every
   call — honoring the golden rule (services must pass `tenant` explicitly; there is no request
   context in the voice runtime).
4. **Guardrail:** an agent may only book event templates in its own `event_templates` set. Any
   other template id is rejected before touching the engine.
5. **No changes to** `booking_service` / `availability` internals — the bridge is a thin,
   tenant-scoped **tool layer** on top of the existing services.

---

## 1. Current state (grounding)

| Fact | Source |
|------|--------|
| `Agent` already links collections via M2M (`knowledge_bases`, `probing_questions`) | [core/models/agent.py](../core/models/agent.py) |
| `Agent` is `TenantBaseModel` → has `tenant`, audit, soft-delete | agent.py |
| **No** link exists between `Agent`/`AgentTemplate` and scheduling | verified (grep) |
| `Booking.Source` = `CUSTOMER` / `INTERNAL` / `PUBLIC_LINK` — **no AI** | [core/models/booking.py](../core/models/booking.py) |
| Engine entry points already tenant-scoped and accept explicit `tenant` + `source` | `core/services/bookings.py`, `core/services/availability.py` |

Reused as-is: `availability.get_available_slots(...)`, `booking_service.create_booking(...)`,
`hold_slot(...)`, `confirm_hold(...)`, `cancel_booking(...)`, `create_collective_booking(...)`.

---

## 2. Phase 1 — Link the agent to bookable event types

### 2a. Model (`core/models/agent.py`)
Add, next to `knowledge_bases` / `probing_questions`:
```python
event_templates = models.ManyToManyField(
    "core.EventTemplate",
    blank=True,
    related_name="agents",
    help_text="Bookable meeting types this agent is allowed to schedule.",
)
```
> **Optional (only if you want an agent pinned to specific people):** also add a
> `scheduling_hosts` M2M to the user model. Default design leaves host selection to the event
> template's strategy — add this *only* for a "personal assistant for host X" style agent.

### 2b. Migration
One additive migration (M2M join table). No backfill, reversible. `makemigrations` will also
emit the `Source.AI` field change from Phase 2 — do both in one migration.

### 2c. Serializer (`core/serializers/agent.py`)
Expose `event_templates` on the `Agent` serializer exactly like `knowledge_bases` is handled:
- **Read:** nested `EventTemplateRefSerializer` (id + name + duration + strategy).
- **Write:** accept a list of `EventTemplate` ids (`PrimaryKeyRelatedField(many=True)`), tenant-
  filtered so an agent can't be wired to another tenant's templates.

### 2d. Admin
Add `event_templates` to the `Agent` admin `filter_horizontal` (as with the other M2Ms).

---

## 3. Phase 2 — AI booking source

`core/models/booking.py`:
```python
class Source(models.TextChoices):
    CUSTOMER = "CUSTOMER", "Customer"
    INTERNAL = "INTERNAL", "Internal"
    PUBLIC_LINK = "PUBLIC_LINK", "Public Link"
    AI = "AI", "AI Agent"          # <-- new
```
Additive `AlterField` migration (fold into Phase 1's migration). Every AI-originated booking is
tagged `Source.AI` for attribution/analytics and to distinguish it from human/internal bookings.

---

## 4. Phase 3 — Actor & audit context for AI bookings (decision)

The voice runtime has **no request user**. `create_booking(actor=...)` is optional, but audit
fields (`created_by`) and notifications may want an actor. **Pick one (recommend A):**

| Option | Behaviour | Trade-off |
|--------|-----------|-----------|
| **A. `actor=None` + `source=AI`** (recommended) | No `created_by`; origin captured by `source=AI` + a `BookingEvent` log | Simplest; audit answered by `source` + event log |
| B. Per-tenant **system "AI" user** | `created_by` = that service user | Cleaner `created_by`, but needs a seeded user per tenant |
| C. Agent's `created_by` (the human who set up the agent) | Attributes to the configurer | Misleading — that human didn't make *this* booking |

Whichever is chosen, `BookingEvent` logs `source=AI` and the `agent_id` for traceability.

---

## 5. Phase 4 — The agent scheduling service (the core)

New module **`core/services/agent_scheduling.py`** — a thin, tenant-scoped, **guardrailed** wrapper
the voice runtime calls. Every function takes an `Agent` and derives `tenant = agent.tenant`.

```python
def list_bookable_event_templates(agent):
    """What this agent may book — id, name, duration, strategy. For the LLM to choose."""

def _assert_allowed(agent, event_template_id):
    """Guardrail: event_template_id MUST be in agent.event_templates (else PermissionError)."""

def list_slots(agent, event_template_id, date_from, date_to, target_host_id=None):
    """-> available slots. Wraps availability.get_available_slots(tenant=agent.tenant, ...)."""

def hold(agent, event_template_id, start_time, participant, target_host_id=None):
    """Two-phase: reserve during the live call. Wraps booking_service.hold_slot(...)."""

def confirm(agent, booking_id):
    """Confirm a held slot. Wraps booking_service.confirm_hold(...)."""

def book(agent, event_template_id, start_time, participant, target_host_id=None):
    """One-shot booking. Wraps create_booking / create_collective_booking, source=Source.AI."""

def cancel(agent, booking_id, reason=""):
    """Wraps booking_service.cancel_booking / cancel_group."""
```

**Contract each function honors:**
- Resolves `tenant = agent.tenant` and passes it **explicitly** to every service call.
- Calls `_assert_allowed` first — the agent can only act on its configured templates.
- `source=Booking.Source.AI`, `actor=<per Phase 3>`.
- Translates domain exceptions into **structured, LLM-friendly results** rather than raising:
  - `SlotConflictError` → `{ok: false, reason: "slot_taken", retry: true}` (tell the agent to re-list slots)
  - `PolicyViolationError` → `{ok: false, reason: "<past|min_notice|horizon>"}`
  - `NoEligibleHostError` → `{ok: false, reason: "no_host_available"}`
- `participant` = `{name, email?, phone?}` captured from the conversation → becomes the
  `BookingParticipant`.

### 5b. Tool schemas for LLM function-calling
Expose the above as tool definitions the model can call (names/JSON-schemas), e.g.
`list_event_types`, `check_availability`, `hold_slot`, `confirm_booking`, `book_meeting`,
`cancel_booking`. Each tool description tells the model *when* to use it and that it may only book
the agent's configured event types. (Format matches whatever the voice runtime's tool interface
expects — the service functions above are the implementation behind each tool.)

### 5c. Runtime integration point
The voice runtime resolves the active **`Agent`** (by id) → calls these service functions /
tools. If the runtime is a separate service, expose thin authenticated **internal endpoints**
wrapping the same functions; if it runs in-process, it imports the service directly. Either way,
**the service layer is the single source of truth and the only place tenant/guardrails live.**

---

## 6. Phase 5 — Conversation context resolution

The agent must turn a conversation into `(event_template, host?, start_time, participant)`:

1. **Which meeting type** → `list_bookable_event_templates(agent)`; the LLM maps caller intent
   ("I want a demo") to one template id. If the agent has exactly one template, skip the choice.
2. **Which host** →
   - `ROUND_ROBIN` / `POOLED` → **none needed**; engine auto-assigns a free eligible host.
   - `DIRECT` → resolve the target host (the template's single skilled host via `UserEventSkill`,
     or a name the caller gave) → pass `target_host_id`.
   - `COLLECTIVE` → engine books all skilled hosts; no target.
3. **Which time** → `check_availability` → offer slots → caller picks → `start_time`.
4. **Who** → capture caller `name` + `email`/`phone` → `participant`.

Recommended live-call sequence: **`check_availability` → `hold_slot` (reserve while confirming) →
`confirm_booking`**, so two simultaneous callers can't grab the same slot (the DB exclusion
constraint + hold TTL already guarantee this).

---

## 7. Phase 6 — Tests (Postgres settings)

New `core/tests/scheduling/test_agent_scheduling.py`:
1. `Agent.event_templates` M2M read/write via serializer; tenant-filtered on write.
2. `Source.AI` present; a booked meeting via `book()` has `source == AI`.
3. **Guardrail:** `book()` / `list_slots()` for a template **not** in the agent's set → rejected,
   no booking created.
4. `list_slots` returns engine slots (round-robin, no host needed).
5. `hold` → `confirm` happy path; second concurrent `hold` on the same slot → `slot_taken`.
6. `DIRECT` template → `target_host_id` honored; `COLLECTIVE` → group booked.
7. **Tenant isolation:** an agent can't book another tenant's event template or host.
8. Exception mapping: policy violation / no eligible host → structured `{ok: false, reason}`.

Run: `uv run python manage.py test core.tests.scheduling.test_agent_scheduling --settings=config.settings.test_postgres`

---

## 8. Phase 7 — Docs

- New [docs/apiDocs/core/agent.md] section: the `event_templates` field on Agent create/update.
- New flow doc `docs/flows/06-ai-agent-booking.md`: sequence (agent → check_availability → hold →
  confirm), with the guardrail and host-selection branches.
- Link from [flows/booking-v4.md](flows/booking-v4.md) and the scheduling README.

---

## 9. Cross-cutting gotchas

1. **Tenant is non-negotiable** — always `tenant = agent.tenant`, passed explicitly. Never rely on
   request/thread context (there isn't one in the voice runtime).
2. **Guardrail before engine** — validate the template belongs to the agent *first*; never let an
   agent book a type it wasn't configured for.
3. **Idempotency on retries** — a dropped call after `hold` leaves a HELD slot that auto-expires
   (existing `cleanup_expired_holds` beat). The agent should `confirm` or let it lapse; don't
   double-book on retry.
4. **Participant contactability** — confirmations/reminders need an email (or a channel). If the
   caller gives only a phone, decide whether SMS/notification exists or the booking is
   confirmation-less.
5. **DIRECT host resolution** — if the caller names a host, resolve by `UserEventSkill` for that
   template; reject if that host isn't skilled/eligible rather than silently round-robining.
6. **Exceptions must not crash the call** — every tool returns a structured result; the LLM
   narrates it. Never surface a Python traceback into the conversation.

---

## 10. Definition of done + effort

- [ ] `Agent.event_templates` M2M (+ optional `scheduling_hosts`) with serializer + admin.
- [ ] `Booking.Source.AI`; AI bookings tagged and logged with `agent_id`.
- [ ] `agent_scheduling.py` service: list types, check availability, hold, confirm, book, cancel —
      tenant-scoped, guardrailed, structured errors.
- [ ] Tool schemas wired to the voice runtime (or internal endpoints if out-of-process).
- [ ] Tests green on Postgres; `makemigrations --check` clean after the one additive migration;
      full core suite still green.
- [ ] Docs + flow diagram.

| Phase | Scope | Rough size |
|-------|-------|-----------|
| 1 | Agent↔EventTemplate M2M + migration + serializer/admin | ~½ day |
| 2 | `Source.AI` | ~¼ day |
| 3 | Actor/audit decision | ~¼ day |
| 4 | `agent_scheduling` service + tool schemas | ~1–1.5 days |
| 5 | Conversation context resolution (runtime glue) | depends on voice runtime |
| 6 | Tests | ~1 day |
| 7 | Docs | ~½ day |

> Blast radius: the `Agent` model, one migration, a new service module, and its tests. The
> booking/availability/reschedule/calendar engine is **not** modified.

"""
AI-agent scheduling bridge — the thin, guardrailed surface a voice/AI agent uses to
book on behalf of hosts. It wraps the existing booking/availability engine WITHOUT
changing it, and enforces two invariants on every call:

  1. **Tenant** is always derived from the agent (`agent.tenant`) and passed explicitly
     to the engine — there is no request context in the AI runtime.
  2. **Guardrail:** an agent may only act on event templates in its own
     `event_templates` set. Any other template id is rejected before the engine runs.

Every public function returns a structured, LLM-friendly result — `{"ok": True, ...}`
or `{"ok": False, "reason": "...", ...}` — and never raises domain exceptions into the
conversation. Bookings are tagged `Booking.Source.AI` (actor is None; origin is captured
by the source tag + the BookingEvent log).
"""

import logging
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone

from core.models.availability_profile import AvailabilityProfile
from core.models.booking import Booking
from core.models.event_template import EventTemplate
from core.services import availability, bookings as booking_service
from core.services.exceptions import (
    ExternalCalendarUnavailable,
    InvalidTransitionError,
    NoEligibleHostError,
    PolicyViolationError,
    SlotConflictError,
)

logger = logging.getLogger(__name__)

_AI = Booking.Source.AI


class TemplateNotAllowed(Exception):
    """The agent is not configured to book the requested event template."""


# --- the conversation timezone -----------------------------------------------
#
# Two clocks are in play on every AI call, and conflating them is the single most
# likely way this bridge goes wrong:
#
#   * the WIRE clock — UTC. Every instant stored, compared, or passed to the
#     booking engine. Never spoken.
#   * the SPOKEN clock — `agent.timezone`. What the caller hears and says. Never
#     stored.
#
# The agent's zone is the right one for speech because the agent is the party in
# the conversation: a caller who says "Tuesday at 3" means 3pm where the service
# operates, not where the host happens to live and certainly not UTC. So every
# value leaving this module carries BOTH forms, and every value arriving without
# an explicit offset is read as spoken time.


def conversation_zone(agent):
    """The ``ZoneInfo`` this agent speaks in: its own, else the tenant's, else UTC."""
    tz_model = agent.timezone or getattr(agent.tenant, "timezone", None)
    if tz_model is None:
        return dt_timezone.utc
    return tz_model.zone


def conversation_zone_name(agent):
    """The IANA name of the spoken zone, for prompts and result payloads."""
    tz_model = agent.timezone or getattr(agent.tenant, "timezone", None)
    return tz_model.timezone if tz_model else "UTC"


def _spoken(instant, zone):
    """Render a UTC instant the way the agent should say it out loud.

    Preformatted deliberately. Asked to convert a UTC timestamp itself, an LLM
    will occasionally get the offset wrong, and on a voice call a wrong time is
    indistinguishable from a correct one until someone misses a meeting.
    """
    local = instant.astimezone(zone)
    # "%-I" is not portable to Windows, so strip the zero by hand.
    hour12 = local.strftime("%I").lstrip("0") or "12"
    return f"{local:%A, %B} {local.day} at {hour12}:{local:%M %p}"


def _times(instant, zone):
    """The dual representation every instant leaves this module with."""
    return {
        "utc": instant.isoformat(),
        "local": instant.astimezone(zone).isoformat(),
        "spoken": _spoken(instant, zone),
    }


# --- helpers -----------------------------------------------------------------

def _resolve_template(agent, event_template_id):
    """Return an EventTemplate the agent is allowed to book, else raise.

    Scoped through `agent.event_templates`, which both enforces the guardrail
    (must be linked to this agent) and tenant isolation (the M2M only holds this
    tenant's templates)."""
    template = agent.event_templates.filter(
        id=event_template_id, is_deleted=False, is_active=True
    ).first()
    if template is None:
        raise TemplateNotAllowed(
            f"Event template {event_template_id} is not bookable by this agent."
        )
    return template


def _get_agent_booking(agent, booking_id):
    """Tenant-scoped fetch of a booking this agent may manage."""
    return Booking.objects.filter(
        tenant=agent.tenant, pk=booking_id, is_deleted=False
    ).first()


def _participant(participant):
    """Normalize a caller-supplied participant to the engine's shape. Always
    carries an `email` key (engine requires it) and a non-empty `name`."""
    if not participant:
        return None
    return {
        "name": (
            participant.get("name")
            or participant.get("email")
            or participant.get("phone")
            or "Guest"
        ),
        "email": participant.get("email") or "",
        "phone_number": participant.get("phone") or participant.get("phone_number"),
    }


def _participants(participant):
    p = _participant(participant)
    return [p] if p else []


def _error_result(exc):
    """Map a domain exception to a structured, LLM-friendly result."""
    if isinstance(exc, SlotConflictError):
        return {"ok": False, "reason": "slot_taken", "retry": True, "message": str(exc)}
    if isinstance(exc, NoEligibleHostError):
        return {"ok": False, "reason": "no_host_available", "message": str(exc)}
    if isinstance(exc, PolicyViolationError):
        return {
            "ok": False,
            "reason": "policy_violation",
            "field": getattr(exc, "field", None),
            "message": str(exc),
        }
    if isinstance(exc, InvalidTransitionError):
        return {"ok": False, "reason": "invalid_state", "message": str(exc)}
    if isinstance(exc, ExternalCalendarUnavailable):
        # Distinct from slot_taken: the time may be free, we just could not check.
        # Telling the caller "that slot is taken" would be a plain untruth.
        return {
            "ok": False,
            "reason": "calendar_unavailable",
            "retry": True,
            "message": str(exc),
        }
    if isinstance(exc, TemplateNotAllowed):
        return {"ok": False, "reason": "template_not_allowed", "message": str(exc)}
    raise exc


_ENGINE_ERRORS = (
    SlotConflictError,
    NoEligibleHostError,
    PolicyViolationError,
    InvalidTransitionError,
    ExternalCalendarUnavailable,
    TemplateNotAllowed,
)


# --- public tool surface -----------------------------------------------------

def list_bookable_event_templates(agent):
    """What this agent may book — for the LLM to choose from."""
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "duration_minutes": t.duration_minutes,
            "assignment_strategy": t.assignment_strategy,
        }
        for t in agent.event_templates.filter(is_deleted=False, is_active=True)
    ]


def list_event_hosts(agent, event_template_id):
    """Hosts who can take this meeting type. Booking a DIRECT event needs a host
    id, and the caller has no other way to obtain one."""
    from core.models.user_event_skill import UserEventSkill

    try:
        template = _resolve_template(agent, event_template_id)
    except _ENGINE_ERRORS as exc:
        return _error_result(exc)
    skills = UserEventSkill.objects.filter(
        tenant=agent.tenant, event_template=template, is_deleted=False
    ).select_related("user")
    return {
        "ok": True,
        "hosts": [
            {
                "id": str(skill.user_id),
                "name": skill.user.get_full_name() or skill.user.email,
            }
            for skill in skills
        ],
    }


def _day_range_utc(agent, date_from, date_to):
    """Resolve an inclusive spoken date range to a half-open UTC interval.

    The engine takes instants, not dates, because a date is a different 24 hours
    in every zone. This layer is the exception that still legitimately deals in
    dates: a caller on the phone says "Tuesday", not an ISO timestamp — so the
    resolution happens here, explicitly, instead of being guessed downstream.

    Whose Tuesday? The agent's. The caller is talking to the agent, not to the
    host — they neither know nor care which timezone the host sits in, so
    resolving against the host's zone would answer a question nobody asked.
    """
    zone = conversation_zone(agent)
    start = datetime.combine(date_from, time.min, tzinfo=zone)
    # date_to is inclusive to the caller, so the exclusive UTC bound is the START
    # of the following day. Using 23:59:59 would drop the last second, and any
    # slot starting in it.
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(dt_timezone.utc), end.astimezone(dt_timezone.utc)


def check_availability(agent, event_template_id, date_from, date_to, target_host_id=None, now=None):
    """Open slots for a template between two spoken dates (inclusive `date` objects).

    Dates are resolved in the agent's timezone — see `_day_range_utc`.

    Each slot carries the ids of the hosts free for it. Omitting `target_host_id`
    is allowed and answers "when is anyone free?" — but the follow-up book/hold
    call must then name one of that slot's `host_ids`, since nothing auto-assigns.

    Every slot reports its start three ways: `start` (UTC, the value to pass
    back), `start_local`, and `speak` (say this one aloud).
    """
    try:
        template = _resolve_template(agent, event_template_id)
    except _ENGINE_ERRORS as exc:
        return _error_result(exc)
    zone = conversation_zone(agent)
    range_start, range_end = _day_range_utc(agent, date_from, date_to)
    slots = availability.get_available_slots(
        agent.tenant, template, range_start, range_end,
        target_host_id=target_host_id, now=now,
    )
    return {
        "ok": True,
        "timezone": conversation_zone_name(agent),
        "slots": [
            {
                # UTC — pass this back verbatim to book_meeting / hold_slot.
                "start": s.start.isoformat(),
                "end": s.end.isoformat(),
                # Spoken — what the caller should hear.
                "start_local": s.start.astimezone(zone).isoformat(),
                "speak": _spoken(s.start, zone),
                "host_ids": [str(h) for h in s.eligible_host_ids],
                # Unverified: a host's external calendar was unreachable. Booking
                # this slot will come back as calendar_unavailable until it clears.
                "unverified": s.external_check_failed,
            }
            for s in slots
        ],
    }


_HOST_REQUIRED = {
    "ok": False,
    "reason": "host_required",
    "message": (
        "This meeting type is booked with a specific host. Call list_event_hosts "
        "(or use a host_id from check_availability) and pass target_host_id."
    ),
}


def book(agent, event_template_id, start_time, participant=None, target_host_id=None, now=None):
    """Create a booking (one-shot). COLLECTIVE templates book every skilled host;
    every other template requires `target_host_id`."""
    try:
        template = _resolve_template(agent, event_template_id)
        if template.assignment_strategy == EventTemplate.AssignmentStrategy.COLLECTIVE:
            created = booking_service.create_collective_booking(
                tenant=agent.tenant,
                event_template=template,
                start_time=start_time,
                participants=_participants(participant),
                actor=None,
                source=_AI,
                now=now,
            )
            zone = conversation_zone(agent)
            return {
                "ok": True,
                "group": True,
                "count": len(created),
                "booking_ids": [str(b.id) for b in created],
                "group_id": str(created[0].group_id) if created else None,
                "timezone": conversation_zone_name(agent),
                **(
                    {"start": created[0].start_time.isoformat(),
                     "speak": _spoken(created[0].start_time, zone)}
                    if created
                    else {}
                ),
            }
        if not target_host_id:
            return _HOST_REQUIRED
        booking = booking_service.create_booking(
            tenant=agent.tenant,
            event_template=template,
            start_time=start_time,
            participants=_participants(participant),
            actor=None,
            target_host_id=target_host_id,
            source=_AI,
            now=now,
        )
        zone = conversation_zone(agent)
        return {
            "ok": True,
            "booking_id": str(booking.id),
            "host_id": str(booking.primary_host_id),
            "start": booking.start_time.isoformat(),
            "end": booking.end_time.isoformat(),
            "timezone": conversation_zone_name(agent),
            "start_local": booking.start_time.astimezone(zone).isoformat(),
            # Read this back to confirm — never re-derive the time from `start`.
            "speak": _spoken(booking.start_time, zone),
        }
    except _ENGINE_ERRORS as exc:
        return _error_result(exc)


def hold(agent, event_template_id, start_time, participant=None, target_host_id=None, now=None):
    """Reserve a slot temporarily (status HELD) while details are confirmed on the
    call. Tagged Source.AI. Not supported for COLLECTIVE templates."""
    try:
        template = _resolve_template(agent, event_template_id)
        if template.assignment_strategy == EventTemplate.AssignmentStrategy.COLLECTIVE:
            return {
                "ok": False,
                "reason": "collective_no_hold",
                "message": "Collective events are booked directly, not held.",
            }
        if not target_host_id:
            return _HOST_REQUIRED
        booking = booking_service.create_booking(
            tenant=agent.tenant,
            event_template=template,
            start_time=start_time,
            participants=_participants(participant),
            actor=None,
            target_host_id=target_host_id,
            source=_AI,
            status=Booking.Status.HELD,
            now=now,
        )
        zone = conversation_zone(agent)
        return {
            "ok": True,
            "booking_id": str(booking.id),
            "host_id": str(booking.primary_host_id),
            "hold_expires_at": (
                booking.hold_expires_at.isoformat() if booking.hold_expires_at else None
            ),
            "timezone": conversation_zone_name(agent),
            "start": booking.start_time.isoformat(),
            "start_local": booking.start_time.astimezone(zone).isoformat(),
            "speak": _spoken(booking.start_time, zone),
        }
    except _ENGINE_ERRORS as exc:
        return _error_result(exc)


def confirm(agent, booking_id, now=None):
    """Turn a held slot into a confirmed booking."""
    booking = _get_agent_booking(agent, booking_id)
    if booking is None:
        return {"ok": False, "reason": "not_found"}
    try:
        confirmed = booking_service.confirm_hold(booking=booking, actor=None, now=now)
    except _ENGINE_ERRORS as exc:
        return _error_result(exc)
    zone = conversation_zone(agent)
    return {
        "ok": True,
        "booking_id": str(confirmed.id),
        "status": confirmed.status,
        "timezone": conversation_zone_name(agent),
        "start": confirmed.start_time.isoformat(),
        "start_local": confirmed.start_time.astimezone(zone).isoformat(),
        "speak": _spoken(confirmed.start_time, zone),
    }


def cancel(agent, booking_id, reason="", now=None):
    """Cancel a booking (or its whole COLLECTIVE group). Internal-actor semantics:
    lead-time is not enforced for AI cancellations."""
    booking = _get_agent_booking(agent, booking_id)
    if booking is None:
        return {"ok": False, "reason": "not_found"}
    try:
        if booking.group_id:
            booking_service.cancel_group(
                group_id=booking.group_id, actor=None, reason=reason,
                enforce_lead=False, now=now,
            )
        else:
            booking_service.cancel_booking(
                booking=booking, actor=None, reason=reason,
                enforce_lead=False, now=now,
            )
    except _ENGINE_ERRORS as exc:
        return _error_result(exc)
    return {"ok": True, "booking_id": str(booking_id)}

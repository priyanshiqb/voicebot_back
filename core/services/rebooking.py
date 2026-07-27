"""
Customer-facing rebooking behind the single-use reschedule token minted by the
reschedule engine. Validates the token, lists slots, and atomically books the new
slot while retiring the old REQUIRES_RESCHEDULE booking(s) and consuming the token.
"""

import logging

from django.db import transaction
from django.utils import timezone

from core.models.booking import Booking
from core.models.booking_event import BookingEvent
from core.models.booking_participant import BookingParticipant
from core.models.event_template import EventTemplate
from core.services import availability, bookings as booking_service, events
from core.services.exceptions import SchedulingError

logger = logging.getLogger(__name__)


class InvalidRescheduleToken(SchedulingError):
    """The reschedule token is missing, unknown, already used, or expired (HTTP 400)."""


def _load_by_token(token, now, *, lock=False):
    if not token:
        raise InvalidRescheduleToken("Invalid reschedule link.")
    qs = Booking.objects.select_related("event_template", "tenant").filter(
        reschedule_token=token,
        is_deleted=False,
        status=Booking.Status.REQUIRES_RESCHEDULE,
    )
    if lock:
        qs = qs.select_for_update()
    booking = qs.first()
    if booking is None:
        raise InvalidRescheduleToken(
            "This reschedule link is invalid or has already been used."
        )
    if booking.reschedule_token_expires_at and booking.reschedule_token_expires_at < now:
        raise InvalidRescheduleToken("This reschedule link has expired.")
    return booking


def get_context(token, now=None):
    """Validate the token and return the booking it refers to."""
    return _load_by_token(token, now or timezone.now())


def list_slots(token, date_from, date_to, now=None):
    now = now or timezone.now()
    booking = _load_by_token(token, now)
    slots = availability.get_available_slots(
        booking.tenant, booking.event_template, date_from, date_to, now=now
    )
    return booking, slots


def _collect_attendees(rows):
    seen = {}
    for row in rows:
        for participant in row.participants.all():
            if participant.role == BookingParticipant.Role.HOST:
                continue
            if participant.email and participant.email not in seen:
                seen[participant.email] = {
                    "name": participant.name,
                    "email": participant.email,
                    "phone_number": participant.phone_number,
                    "role": participant.role,
                }
    return list(seen.values())


def rebook(token, start_time, now=None):
    """Book the chosen slot and retire the old booking(s). Single-use: the token
    is consumed. Returns the new booking (or list for COLLECTIVE)."""
    now = now or timezone.now()
    with transaction.atomic():
        booking = _load_by_token(token, now, lock=True)
        tenant = booking.tenant
        template = booking.event_template

        if booking.group_id:
            old_rows = list(
                Booking.objects.select_for_update().filter(
                    group_id=booking.group_id,
                    is_deleted=False,
                    status=Booking.Status.REQUIRES_RESCHEDULE,
                )
            )
        else:
            old_rows = [booking]

        participants = _collect_attendees(old_rows)

        if template.assignment_strategy == EventTemplate.AssignmentStrategy.COLLECTIVE:
            new = booking_service.create_collective_booking(
                tenant=tenant,
                event_template=template,
                start_time=start_time,
                participants=participants,
                source=Booking.Source.PUBLIC_LINK,
                now=now,
            )
        else:
            target_host_id = (
                booking.primary_host_id
                if template.assignment_strategy
                == EventTemplate.AssignmentStrategy.DIRECT
                else None
            )
            new = booking_service.create_booking(
                tenant=tenant,
                event_template=template,
                start_time=start_time,
                participants=participants,
                target_host_id=target_host_id,
                source=Booking.Source.PUBLIC_LINK,
                now=now,
            )

        # Retire the old rows: mark RESCHEDULED, consume the token, drop calendar events.
        for row in old_rows:
            row.status = Booking.Status.RESCHEDULED
            row.reschedule_token = None
            row.reschedule_token_expires_at = None
            row.save(
                update_fields=[
                    "status",
                    "reschedule_token",
                    "reschedule_token_expires_at",
                    "updated_at",
                ]
            )
            events.log_event(
                row, BookingEvent.EventType.RESCHEDULED, metadata={"rebooked": True}
            )
            if row.external_event_id:
                transaction.on_commit(
                    lambda bid=row.id: booking_service._enqueue_calendar_delete(bid)
                )
    return new

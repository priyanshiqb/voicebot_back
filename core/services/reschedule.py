"""
Reschedule engine — runs when an AvailabilityException (leave/emergency) is
created. For each of that user's future active bookings overlapping the leave:

  1. ROUND_ROBIN templates: try to reassign to another eligible, free host in the
     SAME tenant at the same slot (seamless — the customer is not notified).
  2. Otherwise (or if nobody is free): flag the booking REQUIRES_RESCHEDULE, mint
     a single-use reschedule token, and notify the customer with a link.

Every query is explicitly tenant-scoped. The whole thing is idempotent: re-running
for the same leave never double-reassigns or double-notifies (NotificationLog
dedup + per-booking status guards).
"""

import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction

from core.models.availability_exception import AvailabilityException
from core.models.booking import Booking
from core.models.booking_event import BookingEvent
from core.models.booking_participant import BookingParticipant
from core.services import availability, events, notifications
from core.services.time_utils import utcnow

logger = logging.getLogger(__name__)

# Only settled future bookings are candidates for auto-resolution. Excluding
# REQUIRES_RESCHEDULE makes re-runs idempotent (an already-flagged booking is
# skipped); excluding HELD leaves transient holds to expire on their own.
PROCESSABLE_STATUSES = (Booking.Status.SCHEDULED, Booking.Status.CONFIRMED)


def _enqueue_calendar_push(booking_id):
    from core.tasks import push_booking_to_calendar

    push_booking_to_calendar.delay(str(booking_id))


def _enqueue_calendar_delete_for_host(tenant_id, host_id, external_event_id):
    from core.tasks import delete_calendar_event_for_host

    delete_calendar_event_for_host.delay(str(tenant_id), str(host_id), external_event_id)


def _enqueue_push_user(tenant_id, user_id, title, body):
    from core.tasks import send_push_to_user_task

    send_push_to_user_task.delay(str(tenant_id), str(user_id), title, body)


def _enqueue_push_admins(tenant_id, title, body):
    from core.tasks import send_push_to_admins_task

    send_push_to_admins_task.delay(str(tenant_id), title, body)


def _reassign_host_participant(booking, host):
    participant = booking.participants.filter(
        role=BookingParticipant.Role.HOST
    ).first()
    if participant:
        participant.user = host
        participant.name = (host.get_full_name() or host.email) if host else "Host"
        participant.email = host.email if host else ""
        participant.save(update_fields=["user", "name", "email", "updated_at"])
    elif host:
        BookingParticipant.objects.create(
            tenant=booking.tenant,
            booking=booking,
            user=host,
            name=host.get_full_name() or host.email,
            email=host.email,
            role=BookingParticipant.Role.HOST,
        )


def _try_reassign(tenant, booking, leaving_host_id):
    """Attempt to move the booking to another free host. On success, schedules
    calendar re-sync (remove the old host's mirrored event, push a fresh one to
    the new host). Returns the new host id or None."""
    from user.models import CustomUser

    # Captured before mutation: the event currently lives on the leaving host.
    old_external_id = booking.external_event_id

    candidates = availability.eligible_free_hosts(
        tenant,
        booking.event_template,
        booking.start_time,
        booking.end_time,
        exclude_booking_id=booking.id,
    )
    candidates = [h for h in candidates if h != leaving_host_id]

    for new_host_id in candidates:
        try:
            with transaction.atomic():
                booking.primary_host_id = new_host_id
                # Clear the mirror so the push task creates a fresh event on the
                # new host instead of updating the old host's event id.
                booking.external_event_id = None
                booking.external_provider = None
                booking.save(
                    update_fields=[
                        "primary_host",
                        "external_event_id",
                        "external_provider",
                        "updated_at",
                    ]
                )
                _reassign_host_participant(
                    booking, CustomUser.objects.filter(pk=new_host_id).first()
                )
        except IntegrityError:
            booking.refresh_from_db()
            continue

        if old_external_id:
            transaction.on_commit(
                lambda tid=tenant.id, ohid=leaving_host_id, ext=old_external_id: (
                    _enqueue_calendar_delete_for_host(tid, ohid, ext)
                )
            )
        transaction.on_commit(lambda bid=booking.id: _enqueue_calendar_push(bid))
        return new_host_id
    return None


def _process_booking(tenant, booking_id, leave, now, token_ttl):
    with transaction.atomic():
        booking = (
            Booking.objects.select_for_update()
            .select_related("event_template")
            .get(pk=booking_id)
        )
        # Re-validate under the lock: the world may have changed.
        if (
            booking.is_deleted
            or booking.status not in PROCESSABLE_STATUSES
            or booking.primary_host_id != leave.user_id
            or booking.start_time <= now
        ):
            return "skipped"

        strategy = booking.event_template.assignment_strategy
        if strategy == booking.event_template.AssignmentStrategy.ROUND_ROBIN:
            new_host_id = _try_reassign(tenant, booking, leave.user_id)
            if new_host_id:
                events.log_event(
                    booking,
                    BookingEvent.EventType.REASSIGNED,
                    from_host_id=leave.user_id,
                    to_host_id=new_host_id,
                    metadata={"reason": f"leave:{leave.type}"},
                )
                transaction.on_commit(
                    lambda bid=booking.id, th=new_host_id: notifications.send_reassignment(bid, th)
                )
                transaction.on_commit(
                    lambda tid=tenant.id, th=new_host_id, title=booking.title: (
                        _enqueue_push_user(
                            tid, th, "New booking assigned", title
                        )
                    )
                )
                return "reassigned"

        # Fallback: flag for customer-driven reschedule. For a COLLECTIVE meeting
        # (one row per host) the whole group is flagged with a shared token — if
        # one required host is out, the meeting can't happen.
        token = uuid.uuid4()
        expires = now + token_ttl
        if booking.group_id:
            members = list(
                Booking.objects.select_for_update().filter(
                    group_id=booking.group_id,
                    is_deleted=False,
                    status__in=PROCESSABLE_STATUSES,
                )
            )
        else:
            members = [booking]

        for member in members:
            member.status = Booking.Status.REQUIRES_RESCHEDULE
            member.reschedule_token = token
            member.reschedule_token_expires_at = expires
            member.save(
                update_fields=[
                    "status",
                    "reschedule_token",
                    "reschedule_token_expires_at",
                    "updated_at",
                ]
            )
            events.log_event(
                member,
                BookingEvent.EventType.RESCHEDULE_REQUESTED,
                metadata={"reason": f"leave:{leave.type}", "group": bool(booking.group_id)},
            )
        # One customer notification for the meeting (the leaving host's row carries
        # the attendees + the shared token), plus an internal push alert.
        transaction.on_commit(
            lambda bid=booking.id: notifications.send_reschedule_request(bid)
        )
        transaction.on_commit(
            lambda tid=tenant.id, title=booking.title: _enqueue_push_admins(
                tid, "Booking needs rescheduling", title
            )
        )
        return "flagged"


def handle_leave(tenant, exception_id, now=None):
    """Entry point (called from the reschedule_on_leave Celery task)."""
    now = now or utcnow()
    result = {"reassigned": 0, "flagged": 0, "skipped": 0}

    leave = AvailabilityException.objects.filter(
        pk=exception_id, tenant=tenant, is_deleted=False
    ).first()
    if leave is None:
        return result

    token_ttl = timedelta(
        hours=getattr(settings, "BOOKING_RESCHEDULE_TOKEN_TTL_HOURS", 72)
    )

    booking_ids = list(
        Booking.objects.filter(
            tenant=tenant,
            primary_host=leave.user,
            is_deleted=False,
            status__in=PROCESSABLE_STATUSES,
            effective_start_time__lt=leave.end_time,
            effective_end_time__gt=leave.start_time,
        ).values_list("pk", flat=True)
    )

    for booking_id in booking_ids:
        outcome = _process_booking(tenant, booking_id, leave, now, token_ttl)
        result[outcome] += 1
    return result

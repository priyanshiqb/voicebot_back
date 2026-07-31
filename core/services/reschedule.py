"""
Reschedule engine — runs when an AvailabilityException (leave/emergency) is
created. For each of that user's future active bookings overlapping the leave:
flag the booking REQUIRES_RESCHEDULE, mint a single-use reschedule token, and
notify the customer with a link so they pick a new time.

Hosts are never swapped silently. Bookings name their host (DIRECT) or need every
skilled host (COLLECTIVE), so there is no pool to reassign into — the customer
decides. (Pooled auto-reassignment was removed with the ROUND_ROBIN strategy.)

Every query is explicitly tenant-scoped. The whole thing is idempotent: re-running
for the same leave never double-notifies (NotificationLog dedup + per-booking
status guards).
"""

import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction

from core.models.availability_exception import AvailabilityException
from core.models.booking import Booking
from core.models.booking_event import BookingEvent
from core.services import events, notifications
from core.services.time_utils import utcnow

logger = logging.getLogger(__name__)

# Only settled future bookings are candidates for auto-resolution. Excluding
# REQUIRES_RESCHEDULE makes re-runs idempotent (an already-flagged booking is
# skipped); excluding HELD leaves transient holds to expire on their own.
PROCESSABLE_STATUSES = (Booking.Status.SCHEDULED, Booking.Status.CONFIRMED)


def _enqueue_push_admins(tenant_id, title, body):
    from core.tasks import send_push_to_admins_task

    send_push_to_admins_task.delay(str(tenant_id), title, body)


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

        # Flag for customer-driven reschedule. For a COLLECTIVE meeting (one row
        # per host) the whole group is flagged with a shared token — if one
        # required host is out, the meeting can't happen.
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
    result = {"flagged": 0, "skipped": 0}

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

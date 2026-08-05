"""
Notification service. Notifications are emitted *explicitly* from the service
layer (never via a blanket post_save signal on Booking, which would double-fire
on internal saves such as audit stamping or host reassignment).

Idempotency is guaranteed by NotificationLog's unique (tenant, dedup_key): a
second call for the same logical notification is a no-op. Each function returns
True iff it actually enqueued an email this call (used by reminder counting).
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.models.booking import Booking
from core.models.booking_event import BookingEvent
from core.models.booking_participant import BookingParticipant
from core.models.notification_log import NotificationLog
from core.services import events, ics

logger = logging.getLogger(__name__)


def _load(booking_id):
    return (
        Booking.objects.select_related("event_template", "tenant", "primary_host")
        .prefetch_related("participants")
        .filter(pk=booking_id)
        .first()
    )


def _all_recipients(booking):
    seen = []
    for participant in booking.participants.all():
        if participant.email and participant.email not in seen:
            seen.append(participant.email)
    return seen


def _customer_recipients(booking):
    seen = []
    for participant in booking.participants.all():
        if participant.role == BookingParticipant.Role.HOST:
            continue
        if participant.email and participant.email not in seen:
            seen.append(participant.email)
    return seen


def _body(booking, extra=""):
    body = (
        f"Event: {booking.title}\n"
        f"When: {booking.start_time:%Y-%m-%d %H:%M} UTC\n"
        f"Status: {booking.get_status_display()}\n"
    )
    return body + extra


# BookingEvent types that represent a revision of the meeting itself (as opposed
# to a notification or an external sync). Their count drives the iCalendar
# SEQUENCE, which per RFC 5545 must increase for a client to accept an update to
# an existing UID. It was hard-coded to 0, so a rescheduled booking could leave
# the original time sitting in the attendee's calendar.
_ICS_REVISION_EVENTS = (
    BookingEvent.EventType.CREATED,
    BookingEvent.EventType.CONFIRMED,
    BookingEvent.EventType.RESCHEDULED,
    BookingEvent.EventType.CANCELED,
)


def _ics_sequence(booking):
    """Monotonic revision number for this booking's calendar entry.

    Derived from the append-only event log rather than stored: BookingEvent rows
    are never deleted, so the count only ever grows, which is exactly the
    guarantee SEQUENCE needs.
    """
    return BookingEvent.objects.filter(
        booking=booking, event_type__in=_ICS_REVISION_EVENTS
    ).count()


def _ics_for(booking, method, ics_status, sequence=None):
    return ics.build_ics(
        uid=str(booking.id),
        start=booking.start_time,
        end=booking.end_time,
        summary=booking.title,
        description=booking.event_template.description,
        location=booking.event_template.get_location_type_display(),
        organizer_email=booking.primary_host.email if booking.primary_host_id else None,
        attendee_emails=_all_recipients(booking),
        method=method,
        status=ics_status,
        sequence=_ics_sequence(booking) if sequence is None else sequence,
    )


# How long a PENDING row is treated as "still in flight" before another call may
# re-enqueue it. Without this window, moving the SENT write into the task (so the
# status tells the truth about delivery) would let the 5-minute reminder beat
# re-enqueue a row whose task is merely still queued — a duplicate email.
# Past the window a stuck PENDING becomes re-sendable, which is how a message
# lost by the broker recovers.
PENDING_IN_FLIGHT_GRACE = timedelta(minutes=15)


def _emit(booking, notification_type, dedup_key, subject, body, recipients, ics_content=None):
    """Enqueue an email once for the given dedup_key. Returns True iff enqueued this call.

    The row is left PENDING here; the task marks it SENT on success or FAILED
    with the error after retries are exhausted. Marking SENT at enqueue time (as
    this used to) meant an email that failed every retry still reported success,
    was never retryable, and left NotificationLog.FAILED as a status no code path
    could ever write.
    """
    if not recipients:
        return False

    now = timezone.now()
    log, created = NotificationLog.objects.get_or_create(
        tenant=booking.tenant,
        dedup_key=dedup_key,
        defaults={
            "booking": booking,
            "channel": NotificationLog.Channel.EMAIL,
            "notification_type": notification_type,
            "recipient": ",".join(recipients),
            "status": NotificationLog.Status.PENDING,
        },
    )
    if not created:
        if log.status == NotificationLog.Status.SENT:
            return False
        if (
            log.status == NotificationLog.Status.PENDING
            and log.updated_at
            and log.updated_at > now - PENDING_IN_FLIGHT_GRACE
        ):
            return False  # a task for this row is still in flight

    from core.tasks import send_scheduling_email_task

    # Touch updated_at so the in-flight window above is measured from this
    # enqueue, not from row creation.
    log.status = NotificationLog.Status.PENDING
    log.error = ""
    log.save(update_fields=["status", "error", "updated_at"])

    send_scheduling_email_task.delay(
        subject=subject,
        body=body,
        recipient_list=recipients,
        ics_content=ics_content,
        ics_filename="invite.ics",
        notification_log_id=str(log.id),
    )
    events.log_event(
        booking,
        BookingEvent.EventType.NOTIFICATION_SENT,
        metadata={"type": str(notification_type)},
    )
    return True


def send_confirmation(booking_id):
    booking = _load(booking_id)
    if booking is None:
        return False
    return _emit(
        booking,
        NotificationLog.NotificationType.CONFIRMATION,
        f"{booking.id}:CONFIRMATION",
        "Your booking is confirmed",
        _body(booking),
        _all_recipients(booking),
        _ics_for(booking, "REQUEST", "CONFIRMED"),
    )


def send_cancellation(booking_id):
    booking = _load(booking_id)
    if booking is None:
        return False
    return _emit(
        booking,
        NotificationLog.NotificationType.CANCELED,
        f"{booking.id}:CANCELED",
        "Your booking has been canceled",
        _body(booking),
        _all_recipients(booking),
        _ics_for(booking, "CANCEL", "CANCELLED"),
    )


def send_reschedule_request(booking_id):
    """Customer-facing 'please pick a new time' notice with a tokenized link."""
    booking = _load(booking_id)
    if booking is None:
        return False
    base = getattr(settings, "FRONTEND_PROJECT_URL", "").rstrip("/")
    link = f"{base}/reschedule/{booking.reschedule_token}"
    body = _body(
        booking,
        extra=f"\nUnfortunately this booking needs to be rescheduled.\nPick a new time: {link}\n",
    )
    return _emit(
        booking,
        NotificationLog.NotificationType.RESCHEDULE_REQUEST,
        f"{booking.id}:RESCHEDULE_REQUEST:{booking.reschedule_token}",
        "Please reschedule your booking",
        body,
        _customer_recipients(booking),
    )


def send_reminder(booking_id, offset_minutes):
    booking = _load(booking_id)
    if booking is None:
        return False
    return _emit(
        booking,
        NotificationLog.NotificationType.REMINDER,
        f"{booking.id}:REMINDER:{offset_minutes}",
        "Reminder: upcoming booking",
        _body(booking),
        _all_recipients(booking),
        _ics_for(booking, "REQUEST", "CONFIRMED"),
    )


# ---- COLLECTIVE (one booking per host) group notifications -------------------


def _group_bookings(group_id):
    return list(
        Booking.objects.filter(group_id=group_id, is_deleted=False)
        .select_related("event_template", "tenant", "primary_host")
        .prefetch_related("participants")
    )


def _group_recipients(bookings):
    seen = []
    for booking in bookings:
        for participant in booking.participants.all():
            if participant.email and participant.email not in seen:
                seen.append(participant.email)
    return seen


def _group_emit(group_id, notification_type, subject, method, ics_status):
    bookings = _group_bookings(group_id)
    if not bookings:
        return False
    first = bookings[0]
    recipients = _group_recipients(bookings)
    ics_content = ics.build_ics(
        uid=f"group-{group_id}",
        start=first.start_time,
        end=first.end_time,
        summary=first.title,
        description=first.event_template.description,
        location=first.event_template.get_location_type_display(),
        organizer_email=first.primary_host.email if first.primary_host_id else None,
        attendee_emails=recipients,
        method=method,
        status=ics_status,
        # One shared UID for the whole meeting, so the sequence must also be
        # shared: take the highest revision across the group's rows.
        sequence=max(_ics_sequence(b) for b in bookings),
    )
    return _emit(
        first,
        notification_type,
        f"group:{group_id}:{notification_type}",
        subject,
        _body(first),
        recipients,
        ics_content,
    )


def send_group_confirmation(group_id):
    return _group_emit(
        group_id,
        NotificationLog.NotificationType.CONFIRMATION,
        "Your booking is confirmed",
        "REQUEST",
        "CONFIRMED",
    )


def send_group_cancellation(group_id):
    return _group_emit(
        group_id,
        NotificationLog.NotificationType.CANCELED,
        "Your booking has been canceled",
        "CANCEL",
        "CANCELLED",
    )

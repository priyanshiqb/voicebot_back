"""
Booking service — the single source of truth for all booking mutations.

Correctness invariants:
  * Every query/create is explicitly tenant-scoped (no request-layer scoping here).
  * Double-booking is prevented by the DB exclusion constraint, not by the
    pre-checks. Insert attempts catch IntegrityError; ROUND_ROBIN falls back to
    the next free host, and only a genuinely full slot raises SlotConflictError.
  * External side effects (notifications) are enqueued via transaction.on_commit
    so they never fire on a rolled-back booking.
"""

import logging
import uuid
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from core.models.booking import Booking
from core.models.booking_event import BookingEvent
from core.models.booking_participant import BookingParticipant
from core.services import availability, events, notifications
from core.services.exceptions import (
    InvalidTransitionError,
    NoEligibleHostError,
    PolicyViolationError,
    SlotConflictError,
)

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {
    Booking.Status.COMPLETED,
    Booking.Status.CANCELED,
    Booking.Status.NO_SHOW,
    Booking.Status.RESCHEDULED,
}


def _validate_policy(event_template, start_time, now):
    if start_time < now:
        raise PolicyViolationError("Cannot book a slot in the past.", field="start_time")
    if start_time < now + timedelta(minutes=event_template.min_notice_minutes):
        raise PolicyViolationError(
            f"This event requires at least {event_template.min_notice_minutes} minutes notice.",
            field="start_time",
        )
    if start_time > now + timedelta(days=event_template.max_horizon_days):
        raise PolicyViolationError(
            f"This event cannot be booked more than {event_template.max_horizon_days} days in advance.",
            field="start_time",
        )


def _order_hosts_by_load(tenant, host_ids, window_start, window_end):
    """Least-loaded first (fewest active bookings in the window), tie-break by id."""
    counts = Counter(
        Booking.objects.filter(
            tenant=tenant,
            primary_host_id__in=host_ids,
            is_deleted=False,
            status__in=Booking.ACTIVE_STATUSES,
            start_time__gte=window_start,
            start_time__lt=window_end,
        ).values_list("primary_host_id", flat=True)
    )
    return sorted(host_ids, key=lambda h: (counts.get(h, 0), str(h)))


def _create_participants(booking, host, participants):
    BookingParticipant.objects.create(
        tenant=booking.tenant,
        booking=booking,
        user=host,
        name=(host.get_full_name() or host.email) if host else "Host",
        email=host.email if host else "",
        role=BookingParticipant.Role.HOST,
    )
    for p in participants or []:
        BookingParticipant.objects.create(
            tenant=booking.tenant,
            booking=booking,
            user=p.get("user"),
            name=p.get("name") or p.get("email"),
            email=p["email"],
            phone_number=p.get("phone_number"),
            role=p.get("role") or BookingParticipant.Role.ATTENDEE,
        )


def _enqueue_calendar_push(booking_id):
    from core.tasks import push_booking_to_calendar

    push_booking_to_calendar.delay(str(booking_id))


def _enqueue_calendar_delete(booking_id):
    from core.tasks import delete_booking_calendar_event

    delete_booking_calendar_event.delay(str(booking_id))


_MAX_CONFLICT_RETRIES = 5


def _is_retryable_conflict(exc):
    """True for Postgres deadlock (40P01) / serialization failure (40001), which
    is how two transactions racing the overlap exclusion-constraint check are
    resolved. Distinct from IntegrityError (a committed conflicting row)."""
    cause = exc.__cause__
    pgcode = getattr(cause, "pgcode", None) or getattr(cause, "sqlstate", None)
    return pgcode in ("40P01", "40001")


def _with_conflict_retry(fn):
    """Run fn, retrying on transient Postgres deadlock/serialization failures from
    concurrent overlap checks. After exhausting retries, treat the slot as taken."""
    attempts = 0
    while True:
        attempts += 1
        try:
            return fn()
        except OperationalError as exc:
            if not _is_retryable_conflict(exc):
                raise
            if attempts >= _MAX_CONFLICT_RETRIES:
                raise SlotConflictError("The selected time is no longer available.")
            continue


def create_booking(
    *,
    tenant,
    event_template,
    start_time,
    participants=None,
    actor=None,
    target_host_id=None,
    source=Booking.Source.INTERNAL,
    status=Booking.Status.SCHEDULED,
    hold_ttl_seconds=None,
    now=None,
):
    """Create a booking, selecting a free host. Raises PolicyViolationError (400),
    NoEligibleHostError (400), or SlotConflictError (409). Transient Postgres
    deadlocks from concurrent overlap checks are retried against fresh state."""
    now = now or timezone.now()
    end_time = start_time + timedelta(minutes=event_template.duration_minutes)
    _validate_policy(event_template, start_time, now)

    return _with_conflict_retry(
        lambda: _create_once(
            tenant=tenant,
            event_template=event_template,
            start_time=start_time,
            end_time=end_time,
            participants=participants,
            actor=actor,
            target_host_id=target_host_id,
            source=source,
            status=status,
            hold_ttl_seconds=hold_ttl_seconds,
            now=now,
        )
    )


def _create_once(
    *,
    tenant,
    event_template,
    start_time,
    end_time,
    participants,
    actor,
    target_host_id,
    source,
    status,
    hold_ttl_seconds,
    now,
):
    free = availability.eligible_free_hosts(
        tenant, event_template, start_time, end_time, target_host_id=target_host_id
    )
    if not free:
        candidates = (
            [target_host_id]
            if target_host_id
            else availability.skilled_host_ids(tenant, event_template)
        )
        if not candidates:
            raise NoEligibleHostError("No host is configured to host this event type.")
        raise SlotConflictError("The selected time is no longer available.")

    horizon = now + timedelta(days=event_template.max_horizon_days)
    ordered = _order_hosts_by_load(tenant, free, now, horizon)

    eff_start = start_time - timedelta(minutes=event_template.buffer_before_minutes)
    eff_end = end_time + timedelta(minutes=event_template.buffer_after_minutes)

    hold_expires_at = None
    if status == Booking.Status.HELD:
        ttl = hold_ttl_seconds or getattr(settings, "BOOKING_HOLD_TTL_SECONDS", 300)
        hold_expires_at = now + timedelta(seconds=ttl)

    from user.models import CustomUser

    booking = None
    chosen_host_id = None
    with transaction.atomic():
        for host_id in ordered:
            try:
                # Savepoint so an exclusion-constraint IntegrityError rolls back
                # only this attempt and we can try the next host.
                with transaction.atomic():
                    booking = Booking.objects.create(
                        tenant=tenant,
                        event_template=event_template,
                        title=event_template.name,
                        primary_host_id=host_id,
                        start_time=start_time,
                        end_time=end_time,
                        effective_start_time=eff_start,
                        effective_end_time=eff_end,
                        status=status,
                        source=source,
                        hold_expires_at=hold_expires_at,
                        created_by=actor,
                        updated_by=actor,
                    )
            except IntegrityError:
                booking = None
                continue
            chosen_host_id = host_id
            break

        if booking is None:
            raise SlotConflictError("The selected time is no longer available.")

        host = CustomUser.objects.filter(pk=chosen_host_id).first()
        _create_participants(booking, host, participants)
        events.log_event(
            booking,
            BookingEvent.EventType.CREATED,
            actor=actor,
            metadata={"status": str(status), "source": str(source)},
        )

        if status != Booking.Status.HELD:
            booking_id = booking.id
            transaction.on_commit(lambda: notifications.send_confirmation(booking_id))
            transaction.on_commit(lambda: _enqueue_calendar_push(booking_id))

    return booking


def hold_slot(
    *,
    tenant,
    event_template,
    start_time,
    participants=None,
    actor=None,
    target_host_id=None,
    hold_ttl_seconds=None,
    now=None,
):
    """Reserve a slot temporarily (status HELD). The hold occupies the exclusion
    constraint, so nobody else can take the slot until it is confirmed or lapses."""
    return create_booking(
        tenant=tenant,
        event_template=event_template,
        start_time=start_time,
        participants=participants,
        actor=actor,
        target_host_id=target_host_id,
        source=Booking.Source.CUSTOMER,
        status=Booking.Status.HELD,
        hold_ttl_seconds=hold_ttl_seconds,
        now=now,
    )


def create_collective_booking(
    *,
    tenant,
    event_template,
    start_time,
    participants=None,
    actor=None,
    source=Booking.Source.INTERNAL,
    now=None,
):
    """
    Book EVERY skilled host for a COLLECTIVE event as a single meeting: one
    Booking row per host, all sharing a group_id, all-or-nothing. Returns the
    list of created bookings. Raises NoEligibleHostError (400) or
    SlotConflictError (409) if any required host is unavailable.
    """
    now = now or timezone.now()
    end_time = start_time + timedelta(minutes=event_template.duration_minutes)
    _validate_policy(event_template, start_time, now)

    required = availability.skilled_host_ids(tenant, event_template)
    if not required:
        raise NoEligibleHostError("No host is configured to host this event type.")

    return _with_conflict_retry(
        lambda: _create_collective_once(
            tenant=tenant,
            event_template=event_template,
            start_time=start_time,
            end_time=end_time,
            required_host_ids=required,
            participants=participants,
            actor=actor,
            source=source,
        )
    )


def _create_collective_once(
    *,
    tenant,
    event_template,
    start_time,
    end_time,
    required_host_ids,
    participants,
    actor,
    source,
):
    from user.models import CustomUser

    # Every required host must be free (working hours, no conflict, not on leave).
    for host_id in required_host_ids:
        if host_id not in availability.eligible_free_hosts(
            tenant, event_template, start_time, end_time, target_host_id=host_id
        ):
            raise SlotConflictError("Not all required hosts are available at this time.")

    eff_start = start_time - timedelta(minutes=event_template.buffer_before_minutes)
    eff_end = end_time + timedelta(minutes=event_template.buffer_after_minutes)
    group_id = uuid.uuid4()
    created = []

    with transaction.atomic():
        for host_id in required_host_ids:
            try:
                booking = Booking.objects.create(
                    tenant=tenant,
                    event_template=event_template,
                    title=event_template.name,
                    primary_host_id=host_id,
                    start_time=start_time,
                    end_time=end_time,
                    effective_start_time=eff_start,
                    effective_end_time=eff_end,
                    status=Booking.Status.SCHEDULED,
                    source=source,
                    group_id=group_id,
                    created_by=actor,
                    updated_by=actor,
                )
            except IntegrityError:
                # A required host was taken concurrently — abort the whole meeting.
                raise SlotConflictError(
                    "Not all required hosts are available at this time."
                )
            _create_participants(
                booking, CustomUser.objects.filter(pk=host_id).first(), participants
            )
            events.log_event(
                booking,
                BookingEvent.EventType.CREATED,
                actor=actor,
                metadata={"status": "SCHEDULED", "source": str(source), "collective": True},
            )
            created.append(booking)

        transaction.on_commit(
            lambda gid=group_id: notifications.send_group_confirmation(gid)
        )
        for member in created:
            transaction.on_commit(
                lambda bid=member.id: _enqueue_calendar_push(bid)
            )
    return created


def confirm_hold(*, booking, actor=None, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        b = Booking.objects.select_for_update().get(pk=booking.pk)
        if b.status != Booking.Status.HELD:
            raise InvalidTransitionError(
                f"Only held bookings can be confirmed (current status: {b.status})."
            )
        if b.hold_expires_at and b.hold_expires_at < now:
            raise SlotConflictError("This hold has expired.")
        b.status = Booking.Status.CONFIRMED
        b.hold_expires_at = None
        b.updated_by = actor
        b.save(update_fields=["status", "hold_expires_at", "updated_by", "updated_at"])
        events.log_event(b, BookingEvent.EventType.CONFIRMED, actor=actor)
        bid = b.id
        transaction.on_commit(lambda: notifications.send_confirmation(bid))
        transaction.on_commit(lambda: _enqueue_calendar_push(bid))
    return b


def cancel_booking(*, booking, actor=None, reason="", enforce_lead=True, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        b = Booking.objects.select_for_update().select_related("event_template").get(pk=booking.pk)
        if b.status in TERMINAL_STATUSES:
            raise InvalidTransitionError(
                f"Booking is already {b.status} and cannot be canceled."
            )
        if (
            enforce_lead
            and b.status != Booking.Status.HELD
            and b.start_time - now < timedelta(minutes=b.event_template.cancellation_lead_minutes)
        ):
            raise PolicyViolationError(
                f"This booking can no longer be canceled (requires "
                f"{b.event_template.cancellation_lead_minutes} minutes notice).",
                field="start_time",
            )
        was_hold = b.status == Booking.Status.HELD
        b.status = Booking.Status.CANCELED
        b.cancellation_reason = reason or ""
        b.updated_by = actor
        b.save(update_fields=["status", "cancellation_reason", "updated_by", "updated_at"])
        events.log_event(
            b, BookingEvent.EventType.CANCELED, actor=actor, metadata={"reason": reason or ""}
        )
        if not was_hold:
            bid = b.id
            transaction.on_commit(lambda: notifications.send_cancellation(bid))
            transaction.on_commit(lambda: _enqueue_calendar_delete(bid))
    return b


def cancel_group(*, group_id, actor=None, reason="", enforce_lead=True, now=None):
    """Cancel every non-terminal booking in a COLLECTIVE group as one action."""
    now = now or timezone.now()
    canceled = []
    with transaction.atomic():
        rows = list(
            Booking.objects.select_for_update()
            .select_related("event_template")
            .filter(group_id=group_id, is_deleted=False)
            .exclude(status__in=TERMINAL_STATUSES)
        )
        if not rows:
            return canceled
        if enforce_lead:
            earliest = min(b.start_time for b in rows)
            lead = max(b.event_template.cancellation_lead_minutes for b in rows)
            active = any(b.status != Booking.Status.HELD for b in rows)
            if active and earliest - now < timedelta(minutes=lead):
                raise PolicyViolationError(
                    "This booking can no longer be canceled.", field="start_time"
                )
        for booking in rows:
            booking.status = Booking.Status.CANCELED
            booking.cancellation_reason = reason or ""
            booking.updated_by = actor
            booking.save(
                update_fields=[
                    "status",
                    "cancellation_reason",
                    "updated_by",
                    "updated_at",
                ]
            )
            events.log_event(
                booking,
                BookingEvent.EventType.CANCELED,
                actor=actor,
                metadata={"reason": reason or "", "group": True},
            )
            canceled.append(booking)
        transaction.on_commit(
            lambda gid=group_id: notifications.send_group_cancellation(gid)
        )
        for booking in rows:
            transaction.on_commit(
                lambda bid=booking.id: _enqueue_calendar_delete(bid)
            )
    return canceled


def complete_booking(*, booking, actor=None):
    return _terminal_transition(booking, Booking.Status.COMPLETED, BookingEvent.EventType.COMPLETED, actor)


def mark_no_show(*, booking, actor=None):
    return _terminal_transition(booking, Booking.Status.NO_SHOW, BookingEvent.EventType.NO_SHOW, actor)


def _terminal_transition(booking, new_status, event_type, actor):
    with transaction.atomic():
        b = Booking.objects.select_for_update().get(pk=booking.pk)
        if b.status in TERMINAL_STATUSES or b.status == Booking.Status.HELD:
            raise InvalidTransitionError(
                f"Cannot mark a {b.status} booking as {new_status}."
            )
        b.status = new_status
        b.updated_by = actor
        b.save(update_fields=["status", "updated_by", "updated_at"])
        events.log_event(b, event_type, actor=actor)
    return b


def release_expired_holds(now=None):
    """Cancel HELD bookings whose hold has lapsed, freeing their slot. Idempotent;
    safe to run repeatedly (this is what the Beat cleanup task will call)."""
    now = now or timezone.now()
    released = 0
    expired_ids = list(
        Booking.objects.filter(
            status=Booking.Status.HELD, hold_expires_at__lt=now, is_deleted=False
        ).values_list("pk", flat=True)
    )
    for pk in expired_ids:
        with transaction.atomic():
            b = Booking.objects.select_for_update().get(pk=pk)
            if b.status == Booking.Status.HELD and b.hold_expires_at and b.hold_expires_at < now:
                b.status = Booking.Status.CANCELED
                b.cancellation_reason = "Hold expired"
                b.save(update_fields=["status", "cancellation_reason", "updated_at"])
                events.log_event(b, BookingEvent.EventType.HOLD_EXPIRED)
                released += 1
    return released

"""
Reminder scanner. Run periodically by Celery Beat. For each upcoming booking,
sends the reminder for the nearest configured offset whose trigger time has
passed. Idempotent via NotificationLog (one email per booking per offset).
"""

import logging
from datetime import timedelta

from django.conf import settings

from core.models.booking import Booking
from core.services import notifications
from core.services.time_utils import utcnow
from utils.querysets import batched_queryset

logger = logging.getLogger(__name__)


def send_due_reminders(now=None):
    now = now or utcnow()
    offsets = sorted({int(o) for o in getattr(settings, "BOOKING_REMINDER_OFFSETS_MINUTES", [1440, 60])})
    if not offsets:
        return 0

    max_offset = offsets[-1]
    upcoming = Booking.objects.filter(
        is_deleted=False,
        status__in=[Booking.Status.SCHEDULED, Booking.Status.CONFIRMED],
        start_time__gt=now,
        start_time__lte=now + timedelta(minutes=max_offset),
    )

    sent = 0
    # Paged, not materialized: this is every booking in the next 24h across every
    # tenant, and an earlier version pulled them all into memory. Paged on the primary
    # key rather than streamed with `.iterator()`, whose named cursor does not survive
    # pgbouncer's transaction pooling. Order is irrelevant: each booking is decided on
    # its own.
    for booking in batched_queryset(upcoming, batch_size=500):
        minutes_until = (booking.start_time - now).total_seconds() / 60.0
        # Nearest offset whose window we've entered (ascending offsets).
        for offset in offsets:
            if minutes_until <= offset:
                # Isolated per booking: one bad row (a deleted host, a malformed
                # address) must not abort the run and leave every later booking
                # unreminded — and the next run would hit the same row again.
                try:
                    if notifications.send_reminder(booking.id, offset):
                        sent += 1
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "reminder failed for booking %s at offset %s",
                        booking.id,
                        offset,
                    )
                break
    return sent

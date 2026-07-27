"""
Reminder scanner. Run periodically by Celery Beat. For each upcoming booking,
sends the reminder for the nearest configured offset whose trigger time has
passed. Idempotent via NotificationLog (one email per booking per offset).
"""

from datetime import timedelta

from django.conf import settings

from core.models.booking import Booking
from core.services import notifications
from core.services.time_utils import utcnow


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
    for booking in upcoming:
        minutes_until = (booking.start_time - now).total_seconds() / 60.0
        # Nearest offset whose window we've entered (ascending offsets).
        for offset in offsets:
            if minutes_until <= offset:
                if notifications.send_reminder(booking.id, offset):
                    sent += 1
                break
    return sent

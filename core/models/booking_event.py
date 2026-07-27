from django.conf import settings
from django.db import models

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


class BookingEvent(TenantBaseModelCore):
    """
    Append-only audit log of everything that happens to a Booking. Because the
    project has no history/versioning layer and bookings can change host
    automatically (reassignment), this is the trail support/debugging rely on.
    Rows are never updated or deleted. Tier 1 Core.
    """

    class EventType(models.TextChoices):
        CREATED = "CREATED", "Created"
        CONFIRMED = "CONFIRMED", "Confirmed"
        REASSIGNED = "REASSIGNED", "Reassigned"
        RESCHEDULE_REQUESTED = "RESCHEDULE_REQUESTED", "Reschedule Requested"
        RESCHEDULED = "RESCHEDULED", "Rescheduled"
        CANCELED = "CANCELED", "Canceled"
        NO_SHOW = "NO_SHOW", "No Show"
        COMPLETED = "COMPLETED", "Completed"
        EXTERNAL_SYNCED = "EXTERNAL_SYNCED", "External Synced"
        NOTIFICATION_SENT = "NOTIFICATION_SENT", "Notification Sent"
        HOLD_EXPIRED = "HOLD_EXPIRED", "Hold Expired"

    booking = models.ForeignKey(
        "core.Booking",
        on_delete=models.CASCADE,
        related_name="events",
        help_text="Booking this event describes.",
    )
    event_type = models.CharField(
        max_length=32,
        choices=EventType.choices,
        db_index=True,
        help_text="What happened.",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_events",
        help_text="User who triggered the event; null for system/automated actions.",
    )
    from_host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Previous host, for REASSIGNED events.",
    )
    to_host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="New host, for REASSIGNED events.",
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Free-form details (old/new times, provider ids, error text).",
    )

    class Meta:
        db_table = "core_booking_events"
        verbose_name = "Booking Event"
        verbose_name_plural = "Booking Events"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event_type} :: {self.booking_id}"

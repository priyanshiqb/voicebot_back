from django.conf import settings
from django.db import models

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


class BookingParticipant(TenantBaseModelCore):
    """
    A person associated with a Booking — host, attendee, or external guest.
    Tier 1 Core.
    """

    class Role(models.TextChoices):
        HOST = "HOST", "Host"
        ATTENDEE = "ATTENDEE", "Attendee"
        GUEST = "GUEST", "Guest"

    class ResponseStatus(models.TextChoices):
        NEEDS_ACTION = "NEEDS_ACTION", "Needs Action"
        ACCEPTED = "ACCEPTED", "Accepted"
        DECLINED = "DECLINED", "Declined"

    booking = models.ForeignKey(
        "core.Booking",
        on_delete=models.CASCADE,
        related_name="participants",
        help_text="Booking this participant belongs to.",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_participations",
        help_text="Internal user, if the participant is one (null for external guests).",
    )
    name = models.CharField(max_length=255, help_text="Display name of the participant.")
    email = models.EmailField(help_text="Contact email for notifications.")
    phone_number = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Optional contact phone number.",
    )
    contact = models.ForeignKey(
        "core.Contact",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="booking_participations",
        help_text=(
            "Resolved from phone_number via E.164 matching. SET_NULL so removing a "
            "contact never destroys booking history."
        ),
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        help_text="Participant role in the booking.",
    )
    response_status = models.CharField(
        max_length=20,
        choices=ResponseStatus.choices,
        default=ResponseStatus.NEEDS_ACTION,
        help_text="RSVP state for this participant.",
    )

    class Meta:
        db_table = "core_booking_participants"
        verbose_name = "Booking Participant"
        verbose_name_plural = "Booking Participants"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} <{self.email}> ({self.role})"

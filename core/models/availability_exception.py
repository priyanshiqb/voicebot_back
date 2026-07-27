from django.conf import settings
from django.db import models

from tenant.common.models.tenant_base_model import TenantBaseModel


class AvailabilityException(TenantBaseModel):
    """
    Time off / leave / emergency absence for a user. Creating one triggers the
    reschedule workflow for any of that user's bookings that overlap the window.
    Tier 2 (audited) — who logged the leave is accountability-relevant.
    """

    class ExceptionType(models.TextChoices):
        VACATION = "VACATION", "Vacation"
        SICK = "SICK", "Sick"
        EMERGENCY = "EMERGENCY", "Emergency"
        HOLIDAY = "HOLIDAY", "Holiday"
        OTHER = "OTHER", "Other"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="availability_exceptions",
        help_text="The user who is unavailable during this window.",
    )
    start_time = models.DateTimeField(help_text="Start of the unavailable window (UTC-aware).")
    end_time = models.DateTimeField(help_text="End of the unavailable window (UTC-aware).")
    type = models.CharField(
        max_length=20,
        choices=ExceptionType.choices,
        default=ExceptionType.OTHER,
        help_text="Nature of the absence.",
    )
    reason = models.CharField(
        max_length=255, blank=True, default="", help_text="Optional free-text reason."
    )
    is_all_day = models.BooleanField(
        default=False, help_text="Whether this covers whole day(s)."
    )

    class Meta:
        db_table = "core_availability_exceptions"
        verbose_name = "Availability Exception"
        verbose_name_plural = "Availability Exceptions"
        ordering = ["-start_time"]
        indexes = [
            models.Index(
                fields=["tenant", "user", "start_time", "end_time"],
                name="core_avail_exc_user_time_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_time__gt=models.F("start_time")),
                name="availability_exception_end_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.get_type_display()} {self.user_id} {self.start_time:%Y-%m-%d %H:%M}"

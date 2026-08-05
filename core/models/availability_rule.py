from django.db import models

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


class AvailabilityRule(TenantBaseModelCore):
    """
    A single weekly working block for an AvailabilityProfile. Multiple rows per
    weekday are allowed (split shifts). Tier 1 Core.

    ``start_time`` / ``end_time`` are naive wall-clock times interpreted in the
    parent profile's timezone. When ``end_time <= start_time`` the block ends on
    the NEXT day: 22:00 -> 06:00 is an overnight shift, and 00:00 -> 00:00 is a
    full 24 hours. A zero-length block is therefore not expressible, by design.

    These wall-clock values are authoring input only — the engine reads the
    materialized UTC intervals in AvailabilityWindow.
    """

    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    profile = models.ForeignKey(
        "core.AvailabilityProfile",
        on_delete=models.CASCADE,
        related_name="rules",
        help_text="Availability profile this working block belongs to.",
    )
    weekday = models.PositiveSmallIntegerField(
        choices=Weekday.choices,
        help_text="Day of week (0=Monday … 6=Sunday, matching Python's weekday()).",
    )
    start_time = models.TimeField(
        help_text="Local wall-clock start of the working block.",
    )
    end_time = models.TimeField(
        help_text=(
            "Local wall-clock end of the working block. If <= start_time the block "
            "ends on the following day (overnight shift)."
        ),
    )

    class Meta:
        db_table = "core_availability_rules"
        verbose_name = "Availability Rule"
        verbose_name_plural = "Availability Rules"
        ordering = ["weekday", "start_time"]
        # No end > start constraint: an overnight block legitimately has end < start.
        # Coherence is enforced by the circular-week overlap validator in
        # AvailabilityProfileSerializer.validate_rules.

    @property
    def wraps_midnight(self):
        """True when this block ends on the day after ``weekday``."""
        return self.end_time <= self.start_time

    def __str__(self):
        suffix = " (+1d)" if self.wraps_midnight else ""
        return f"{self.get_weekday_display()} {self.start_time}-{self.end_time}{suffix}"

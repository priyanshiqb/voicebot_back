from django.db import models

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


class AvailabilityRule(TenantBaseModelCore):
    """
    A single weekly working block for an AvailabilityProfile. Multiple rows per
    weekday are allowed (split shifts). Tier 1 Core.

    ``start_time`` / ``end_time`` are naive wall-clock times interpreted in the
    parent profile's timezone.
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
        help_text="Local wall-clock end of the working block (must be after start).",
    )

    class Meta:
        db_table = "core_availability_rules"
        verbose_name = "Availability Rule"
        verbose_name_plural = "Availability Rules"
        ordering = ["weekday", "start_time"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_time__gt=models.F("start_time")),
                name="availability_rule_end_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.get_weekday_display()} {self.start_time}-{self.end_time}"

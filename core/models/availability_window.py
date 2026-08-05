from django.conf import settings
from django.db import models

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


class AvailabilityWindow(TenantBaseModelCore):
    """
    A materialized, absolute UTC working interval derived from one AvailabilityRule
    on one local calendar date. This is what the availability engine reads — all
    interval maths is UTC-to-UTC, with DST already resolved at materialization time.

    Pure derived data: rows are HARD-deleted and regenerated, never soft-deleted.
    Rebuild with ``manage.py materialize_availability``. Tier 1 Core.
    """

    profile = models.ForeignKey(
        "core.AvailabilityProfile",
        on_delete=models.CASCADE,
        related_name="windows",
        help_text="Profile this window was materialized from.",
    )
    rule = models.ForeignKey(
        "core.AvailabilityRule",
        on_delete=models.CASCADE,
        related_name="windows",
        help_text="Rule this window is one occurrence of.",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="availability_windows",
        help_text="Denormalized from profile.user so the engine filters without a join.",
    )
    local_date = models.DateField(
        help_text=(
            "Local calendar date the block STARTS on. Regeneration key; also what "
            "makes a materialization bug diagnosable."
        ),
    )
    start_utc = models.DateTimeField(help_text="Absolute UTC start of the block.")
    end_utc = models.DateTimeField(
        help_text="Absolute UTC end. Always > start_utc: the overnight wrap is resolved here."
    )
    dst_flag = models.CharField(
        max_length=16,
        blank=True,
        default="",
        help_text=(
            "'' | 'nonexistent' | 'ambiguous' — set when a boundary landed on a DST "
            "edge and the resolution policy was applied. Diagnostic only."
        ),
    )

    class Meta:
        db_table = "core_availability_windows"
        verbose_name = "Availability Window"
        verbose_name_plural = "Availability Windows"
        ordering = ["start_utc"]
        indexes = [
            models.Index(
                fields=["tenant", "user", "start_utc", "end_utc"],
                name="core_avail_win_user_time_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["rule", "local_date"],
                name="unique_availability_window_occurrence",
            ),
            # Safe here, unlike on the rule: a materialized window is an absolute
            # interval, so the overnight wrap has already been resolved.
            models.CheckConstraint(
                condition=models.Q(end_utc__gt=models.F("start_utc")),
                name="availability_window_end_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} free {self.start_utc:%Y-%m-%d %H:%M}Z–{self.end_utc:%H:%M}Z"

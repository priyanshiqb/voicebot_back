from django.conf import settings
from django.db import models

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


class ExternalBusyBlock(TenantBaseModelCore):
    """
    Cache of a host's external-calendar busy intervals, kept fresh by webhook
    incremental sync. Availability reads these instead of a live free/busy call
    while a watch channel is active. Tier 1 Core; rows are hard-deleted (pure
    cache, no audit trail needed).
    """

    integration = models.ForeignKey(
        "core.CalendarIntegration",
        on_delete=models.CASCADE,
        related_name="busy_blocks",
        help_text="Integration this cached block came from.",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="external_busy_blocks",
        help_text="Host the busy block belongs to.",
    )
    external_event_id = models.CharField(
        max_length=255, db_index=True, help_text="Provider event id (upsert key)."
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()

    class Meta:
        db_table = "core_external_busy_blocks"
        verbose_name = "External Busy Block"
        verbose_name_plural = "External Busy Blocks"
        ordering = ["start_time"]
        indexes = [
            models.Index(
                fields=["tenant", "user", "start_time", "end_time"],
                name="core_ext_busy_user_time_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["integration", "external_event_id"],
                name="unique_external_busy_block",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} busy {self.start_time:%Y-%m-%d %H:%M}"

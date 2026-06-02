from django.db import models
from tenant.common.models.tenant_base_model import TenantBaseModel

class KBETLTracker(TenantBaseModel):
    """
    Highly optimized aggregate tracking model for Knowledge Base ETL pipeline executions.
    Stores absolute pipeline state, audits, stats, and failures in a single row.
    """
    class OverallStatus(models.TextChoices):
        PENDING = "PENDING", "Pending Ingestion"
        PROCESSING = "PROCESSING", "Currently Processing"
        COMPLETED = "COMPLETED", "Successfully Processed"
        FAILED = "FAILED", "Processing Failed"

    kb = models.ForeignKey(
        "core.KnowledgeBase",
        on_delete=models.CASCADE,
        related_name="etl_runs",
        help_text="The parent Knowledge Base executing this pipeline."
    )
    task_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        db_index=True,
        help_text="Celery Task ID executing this run."
    )
    status = models.CharField(
        max_length=20,
        choices=OverallStatus.choices,
        default=OverallStatus.PENDING
    )
    
    kb_json_snapshot = models.JSONField(
        blank=True,
        null=True,
        help_text="Complete serialized snapshot of the KB aggregate (all files, text, sites) at run initiation."
    )
    to_process_ids = models.JSONField(
        blank=True,
        null=True,
        help_text="Structured dict mapping content types to the list of UUIDs queued to process."
    )
    processed_ids = models.JSONField(
        blank=True,
        null=True,
        help_text="List of UUIDs that successfully vectorized/indexed."
    )
    processing_stats = models.JSONField(
        blank=True,
        null=True,
        help_text="Consolidated counts: {'total_items': int, 'success_count': int, 'failure_count': int}."
    )
    error_details = models.JSONField(
        blank=True,
        null=True,
        help_text="Detailed error payloads mapping failed UUIDs directly to stack traces or error logs."
    )

    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta(TenantBaseModel.Meta):
        db_table = "core_kb_etl_trackers"
        ordering = ["-created_at"]
        verbose_name = "KB ETL Tracker"
        verbose_name_plural = "KB ETL Trackers"

    def __str__(self):
        return f"Run {self.id} | KB: {self.kb.name} | Status: {self.status}"

from django.db import models
from tenant.common.models.tenant_base_model import TenantBaseModel

class KBBaseModel(TenantBaseModel):
    class ProcessingStatusChoices(models.TextChoices):
        PENDING = "PENDING", "Pending Ingestion"
        PROCESSING = "PROCESSING", "Currently Processing"
        COMPLETED = "COMPLETED", "Successfully Processed"
        FAILED = "FAILED", "Processing Failed"

    # Ingestion / ETL status tracking per object
    is_processed = models.BooleanField(
        default=False,
        help_text="True if the item has been successfully processed/embedded"
    )
    processing_status = models.CharField(
        max_length=20,
        choices=ProcessingStatusChoices.choices,
        default=ProcessingStatusChoices.PENDING,
        help_text="Ingestion status of this individual object"
    )
    processing_error = models.TextField(
        blank=True,
        null=True,
        help_text="Error message if the ingestion cycle failed"
    )
    last_processed_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Timestamp when the object was last processed"
    )

    # Ingested status-with-time timeline log
    processing_history = models.JSONField(
        default=list,
        blank=True,
        help_text="Historical record of all status changes with timestamps"
    )

    class Meta:
        abstract = True

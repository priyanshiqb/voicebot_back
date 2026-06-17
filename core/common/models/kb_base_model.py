import logging
from django.db import models
from tenant.common.models.tenant_base_model import TenantBaseModel

logger = logging.getLogger(__name__)

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
    
    # New ETL state tracking fields
    etl_id = models.UUIDField(
        blank=True,
        null=True,
        help_text="Current ETL task unique identifier"
    )
    pending_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Timestamp when ingestion became pending"
    )
    processing_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Timestamp when ingestion started processing"
    )
    completed_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Timestamp when ingestion completed successfully"
    )
    failed_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Timestamp when ingestion failed"
    )

    class Meta:
        abstract = True

    def update_etl_status(self, etl_id, status, error_message=None, timestamp=None):
        from django.utils import timezone

        # If current etl_id is set and does not match the incoming etl_id, do not proceed.
        if self.etl_id and str(self.etl_id) != str(etl_id):
            logger.warning(
                f"ETL status update rejected for {self.__class__.__name__} (ID: {self.id}). "
                f"Current etl_id '{self.etl_id}' does not match requested etl_id '{etl_id}'."
            )
            return False

        if not timestamp:
            timestamp = timezone.now()

        # Update the etl_id and processing_status
        self.etl_id = etl_id
        self.processing_status = status

        # Update specific timestamp fields based on status
        if status == self.ProcessingStatusChoices.PENDING:
            self.pending_at = timestamp
            self.is_processed = False
        elif status == self.ProcessingStatusChoices.PROCESSING:
            self.processing_at = timestamp
            self.is_processed = False
        elif status == self.ProcessingStatusChoices.COMPLETED:
            self.completed_at = timestamp
            self.is_processed = True
            self.processing_error = None
        elif status == self.ProcessingStatusChoices.FAILED:
            self.failed_at = timestamp
            self.is_processed = False
            self.processing_error = error_message

        self.save()
        return True

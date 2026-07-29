from django.db import models
from django.utils.translation import gettext_lazy as _
from tenant.common.models.tenant_base_model_core import TenantBaseModelCore

def agent_call_recording_upload_path(instance, filename):
    return f"t-{instance.tenant.id}/call_recordings/{instance.id}_{filename}"

class AgentCallRecording(TenantBaseModelCore):
    class UploadStatus(models.TextChoices):
        PENDING = "PENDING", "Awaiting upload"
        CONFIRMED = "CONFIRMED", "Upload confirmed"
        FAILED = "FAILED", "Upload failed"

    file = models.FileField(
        upload_to=agent_call_recording_upload_path,
        max_length=500,
        help_text=_("The MP3 recording file stored in the S3 bucket.")
    )
    upload_status = models.CharField(
        max_length=10,
        choices=UploadStatus.choices,
        default=UploadStatus.CONFIRMED,
        db_index=True,
        help_text=_(
            "PENDING while a presigned upload is outstanding. Defaults to CONFIRMED so "
            "existing rows and any direct-write path stay valid."
        ),
    )
    original_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Clean original name of the uploaded file.")
    )
    file_size_in_bytes = models.BigIntegerField(
        default=0,
        help_text=_("Size of the MP3 file in bytes.")
    )
    extension = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        help_text=_("File extension (e.g., '.mp3').")
    )
    is_processed = models.BooleanField(
        default=False,
        help_text=_("Whether this recording has been successfully processed by the system.")
    )

    class Meta:
        db_table = "core_agent_call_recording"
        verbose_name = _("Agent Call Recording")
        verbose_name_plural = _("Agent Call Recordings")
        ordering = ["-created_at"]

    def __str__(self):
        return self.original_name or str(self.id)

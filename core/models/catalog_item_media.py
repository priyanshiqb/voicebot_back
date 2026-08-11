import logging
import uuid

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore

logger = logging.getLogger(__name__)


def catalog_media_upload_path(instance, filename):
    """Tenant-and-item scoped key, mirroring ``kb_file_upload_path``.

    The stored name is a fresh UUID plus the extension, never the uploaded
    filename: the original is kept separately in ``original_name`` for display, and
    interpolating a client-supplied string into an object key is how the Knowledge
    Base flow ended up accepting ``../../t-{victim}/...``.
    """
    extension = ""
    if "." in filename:
        extension = f".{filename.rsplit('.', 1)[-1].lower()[:10]}"
    return f"t-{instance.tenant_id}/catalog/{instance.item_id}/{uuid.uuid4().hex}{extension}"


class CatalogItemMedia(TenantBaseModelCore):
    """
    A photo, video or document attached to a :class:`core.CatalogItem`. Tier 1 Core.

    Tier 1 rather than Tier 2 because this is derivative of an audited parent — a
    mis-uploaded photo is not an incident, and the item itself records who changed
    what.

    ``upload_status`` exists for one reason: the upload flow is **stateful**.
    ``request-upload-url`` creates the row ``PENDING`` and hands back
    ``{media_id, upload_url}``; ``confirm-upload`` takes the ``media_id`` and reads
    the object key from this tenant-scoped row. The key never crosses the wire, so a
    client cannot name one. The stateless ``object_key`` alternative is what let one
    tenant read *and delete* another's Knowledge Base files, and that is not being
    repeated in a new module.
    """

    class MediaType(models.TextChoices):
        IMAGE = "IMAGE", _("Image")
        VIDEO = "VIDEO", _("Video")
        DOCUMENT = "DOCUMENT", _("Document")

    class UploadStatus(models.TextChoices):
        PENDING = "PENDING", _("Awaiting upload")
        CONFIRMED = "CONFIRMED", _("Upload confirmed")
        FAILED = "FAILED", _("Upload failed")

    item = models.ForeignKey(
        "core.CatalogItem",
        on_delete=models.CASCADE,
        related_name="media",
        help_text=_("The item this illustrates."),
    )
    media_type = models.CharField(
        max_length=20,
        choices=MediaType.choices,
        default=MediaType.IMAGE,
        help_text=_("DOCUMENT covers spec sheets, brochures and lab reports."),
    )
    file = models.FileField(
        upload_to=catalog_media_upload_path,
        max_length=500,
        blank=True,
        help_text=_("Blank while the presigned upload is still outstanding."),
    )
    original_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_(
            "As uploaded, for display and Content-Disposition. Sanitised to a "
            "basename before it is stored."
        ),
    )
    extension = models.CharField(max_length=10, blank=True, default="")
    file_size_in_bytes = models.BigIntegerField(default=0)
    upload_status = models.CharField(
        max_length=10,
        choices=UploadStatus.choices,
        default=UploadStatus.PENDING,
        db_index=True,
        help_text=_(
            "PENDING until the client confirms. Only CONFIRMED rows should ever be "
            "served."
        ),
    )
    alt_text = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_("Accessibility text, and the caption a chat channel would show."),
    )
    is_primary = models.BooleanField(
        default=False,
        help_text=_("The thumbnail. At most one per item."),
    )
    sort_order = models.PositiveIntegerField(default=0, help_text=_("Gallery order."))

    class Meta:
        db_table = "core_catalog_item_media"
        verbose_name = _("Catalog Item Media")
        verbose_name_plural = _("Catalog Item Media")
        ordering = ["sort_order", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["item"],
                condition=models.Q(is_deleted=False, is_primary=True),
                name="unique_primary_catalog_item_media",
            ),
        ]
        indexes = [
            models.Index(fields=["item", "sort_order"]),
            models.Index(fields=["tenant", "upload_status"]),
        ]

    def __str__(self):
        return self.original_name or f"{self.media_type} for {self.item_id}"

    def soft_delete(self, user=None):
        """Clear ``is_primary`` on the way out.

        The partial unique constraint ignores deleted rows, but leaving the flag set
        means a restore later collides with whatever was made primary in the
        meantime — and the admin surfaces that as a raw IntegrityError.
        """
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.is_primary = False
        self.save(update_fields=["is_deleted", "deleted_at", "is_primary"])

    def delete(self, *args, **kwargs):
        """Remove the stored object along with the row.

        Only reached by a *hard* delete — the API soft-deletes, and a soft delete
        deliberately keeps the object so a restore still has its file. The paths that
        get here are the abandoned-upload sweep, the admin and a shell session, and in
        each of those the row is the only record of the object key: drop it without
        the blob and the bytes are orphaned in the bucket forever.

        Blob failures are logged rather than raised, so an unreachable bucket cannot
        make cleanup impossible.
        """
        object_key = self.file.name if self.file else None
        if object_key:
            try:
                self.file.delete(save=False)
            except Exception:  # noqa: BLE001 — never block the row delete
                logger.warning(
                    "Could not delete catalog media object %s for %s; it is now orphaned.",
                    object_key,
                    self.pk,
                )
        super().delete(*args, **kwargs)

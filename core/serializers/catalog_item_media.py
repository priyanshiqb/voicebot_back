import logging
import os

from django.conf import settings
from rest_framework import serializers

from core.models.catalog_item import CatalogItem
from core.models.catalog_item_media import CatalogItemMedia
from core.serializers.catalog_common import resolve_tenant_id
from utils.files.sizes import format_size_limit
from tenant.common.serializers.tenant_base_model_core_serializer import (
    TenantBaseModelCoreSerializer,
)

logger = logging.getLogger(__name__)

#: Extension allowlist per media type. Checked at `request-upload-url` time, before a
#: presigned URL is issued, so an unwanted type never reaches the bucket at all.
ALLOWED_EXTENSIONS = {
    CatalogItemMedia.MediaType.IMAGE: {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"},
    CatalogItemMedia.MediaType.VIDEO: {".mp4", ".webm", ".mov"},
    CatalogItemMedia.MediaType.DOCUMENT: {".pdf", ".docx", ".xlsx", ".txt", ".csv"},
}


def presigned_media_url(instance):
    """A time-limited GET URL, or the raw name when blob storage is not in play.

    Failures are swallowed to the stored name rather than raised: a catalog listing
    must not 500 because the object store hiccupped on one thumbnail.
    """
    if not (instance.file and instance.file.name):
        return None
    if not getattr(settings, "USE_AZURE_STORAGE", False):
        return instance.file.name

    from utils.blob_storage.service import AzureBlobStorageService

    try:
        return AzureBlobStorageService().get_presigned_url_for_object(
            instance.file.name, expiry_hours=24
        )
    except Exception:
        logger.warning(
            "Could not presign catalog media %s; returning the stored key.",
            instance.pk,
        )
        return instance.file.name


class CatalogItemMediaRefSerializer(serializers.ModelSerializer):
    """Nested reference — what an item's `media` array renders as."""

    file_url = serializers.SerializerMethodField()

    class Meta:
        model = CatalogItemMedia
        fields = ("id", "media_type", "file_url", "alt_text", "is_primary", "sort_order")
        read_only_fields = fields

    def get_file_url(self, obj) -> str | None:
        return presigned_media_url(obj)


class CatalogItemMediaSerializer(TenantBaseModelCoreSerializer):
    """
    Full media record.

    `file`, `upload_status`, `extension` and `file_size_in_bytes` are all read-only:
    they are written by the two-step upload flow, and `upload_status` in particular is
    the gate on whether the object is served at all. A client able to flip it to
    CONFIRMED could publish a key with nothing behind it.
    """

    item = serializers.PrimaryKeyRelatedField(read_only=True)
    file_url = serializers.SerializerMethodField()
    media_type_label = serializers.CharField(
        source="get_media_type_display", read_only=True
    )

    class Meta(TenantBaseModelCoreSerializer.Meta):
        model = CatalogItemMedia
        fields = TenantBaseModelCoreSerializer.Meta.fields + [
            "item",
            "media_type",
            "media_type_label",
            "file_url",
            "original_name",
            "extension",
            "file_size_in_bytes",
            "upload_status",
            "alt_text",
            "is_primary",
            "sort_order",
            "is_active",
        ]
        read_only_fields = TenantBaseModelCoreSerializer.Meta.read_only_fields + (
            "item",
            "media_type",
            "media_type_label",
            "file_url",
            "original_name",
            "extension",
            "file_size_in_bytes",
            "upload_status",
        )

    def get_file_url(self, obj) -> str | None:
        return presigned_media_url(obj)

    def validate_alt_text(self, value):
        return (value or "").strip()


class CatalogItemMediaListSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = CatalogItemMedia
        fields = (
            "id",
            "item",
            "media_type",
            "file_url",
            "original_name",
            "file_size_in_bytes",
            "upload_status",
            "is_primary",
            "sort_order",
            "created_at",
        )
        read_only_fields = fields

    def get_file_url(self, obj) -> str | None:
        return presigned_media_url(obj)


class CatalogMediaUploadRequestSerializer(serializers.Serializer):
    """Body of `POST /catalog-item-media/request-upload-url/`.

    The response carries `media_id` and an upload URL — **never an object key**. The
    key is built server-side and read back from this tenant-scoped row at confirm
    time, so a client cannot name one. The stateless alternative is what let one
    tenant both read and delete another's Knowledge Base files; see §21 of the contact
    module plan.
    """

    item = serializers.PrimaryKeyRelatedField(queryset=CatalogItem.objects.all())
    media_type = serializers.ChoiceField(
        choices=CatalogItemMedia.MediaType.choices,
        default=CatalogItemMedia.MediaType.IMAGE,
    )
    filename = serializers.CharField(max_length=255)
    content_type = serializers.CharField(max_length=100)
    file_size_in_bytes = serializers.IntegerField(min_value=1)
    alt_text = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    is_primary = serializers.BooleanField(required=False, default=False)

    def validate_filename(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("A filename is required.")
        return value

    def validate(self, attrs):
        item = attrs["item"]
        tenant_id = resolve_tenant_id(self)

        if tenant_id is not None and item.tenant_id != tenant_id:
            # 404-shaped message, not 403: confirming that another tenant's item exists
            # is itself a leak. The view returns 404 for this.
            raise serializers.ValidationError({"item": "No such item."})

        media_type = attrs["media_type"]
        extension = os.path.splitext(attrs["filename"])[1].lower()
        allowed = ALLOWED_EXTENSIONS[media_type]
        if extension not in allowed:
            raise serializers.ValidationError(
                {
                    "filename": (
                        f"{extension or 'That file'} is not a supported {media_type.lower()} "
                        f"format. Allowed: {', '.join(sorted(allowed))}."
                    )
                }
            )

        max_bytes = getattr(settings, "CATALOG_MAX_MEDIA_BYTES", 10 * 1024 * 1024)
        if attrs["file_size_in_bytes"] > max_bytes:
            raise serializers.ValidationError(
                {
                    "file_size_in_bytes": (
                        f"At most {format_size_limit(max_bytes)} per file."
                    )
                }
            )

        max_media = getattr(settings, "CATALOG_MAX_MEDIA_PER_ITEM", 10)
        # PENDING rows count. Otherwise requesting a hundred upload URLs and never
        # confirming them is an unmetered way past the cap.
        existing = CatalogItemMedia.objects.filter(item_id=item.pk).count()
        if existing >= max_media:
            raise serializers.ValidationError(
                {
                    "item": (
                        f"'{item.name}' already has {existing} media files; the maximum "
                        f"is {max_media}. Delete one first — unconfirmed uploads count "
                        "and are swept on request."
                    )
                }
            )

        attrs["extension"] = extension
        return attrs


class CatalogMediaUploadConfirmSerializer(serializers.Serializer):
    """Body of `POST /catalog-item-media/confirm-upload/`. `media_id` only.

    No object key, by design — see :class:`CatalogMediaUploadRequestSerializer`.
    """

    media_id = serializers.UUIDField()

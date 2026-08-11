import logging
import os

from django.db import transaction
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action

from core.models.catalog_item_media import CatalogItemMedia
from core.serializers.catalog_item_media import (
    CatalogItemMediaListSerializer,
    CatalogItemMediaSerializer,
    CatalogMediaUploadConfirmSerializer,
    CatalogMediaUploadRequestSerializer,
)
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from utils.s3_storage.keys import build_tenant_object_key
from utils.s3_storage.service import S3StorageService

logger = logging.getLogger(__name__)


@extend_schema(tags=["Catalog"])
@extend_schema_view(
    list=extend_schema(
        summary="List catalog media",
        description="Filter with `?item=<id>` and `?upload_status=CONFIRMED`.",
    ),
    retrieve=extend_schema(summary="Get one media record"),
    partial_update=extend_schema(
        summary="Update caption, order or primary flag",
        description=(
            "Only `alt_text`, `is_primary`, `sort_order` and `is_active` are writable. "
            "The file, its key and `upload_status` belong to the upload flow."
        ),
    ),
    destroy=extend_schema(summary="Delete a media record (soft)"),
)
class CatalogItemMediaViewSet(TenantBaseViewSet):
    """
    Photos, video and documents for catalog items.

    **Uploads are two-step and stateful.** `request-upload-url` creates a `PENDING`
    row and returns `{media_id, upload_url}`; the client PUTs the bytes straight to
    the object store and then calls `confirm-upload` with the `media_id`.

    The object key never crosses the wire. That is the whole point: the stateless
    alternative — hand the client a key and accept one back — is what allowed one
    tenant to confirm *and delete* another tenant's Knowledge Base files. A client
    cannot name a key it was never given.

    There is no plain `POST /` — a media row without a file is not a thing worth
    creating, and the only way to get a file here is the two-step flow.
    """

    queryset = CatalogItemMedia.objects.all()
    serializer_class = CatalogItemMediaSerializer
    list_serializer_class = CatalogItemMediaListSerializer

    http_method_names = ["get", "patch", "delete", "post", "head", "options"]

    filterset_fields = ["item", "media_type", "upload_status", "is_primary", "is_active"]
    search_fields = ["original_name", "alt_text", "item__name"]
    ordering_fields = ["sort_order", "created_at", "file_size_in_bytes"]
    ordering = ["item", "sort_order"]

    select_related_fields = ["tenant", "item"]

    def create(self, request, *args, **kwargs):
        return self.error(
            message=(
                "Media is created through the upload flow. POST to "
                "/catalog-item-media/request-upload-url/, PUT the file to the URL it "
                "returns, then POST to /catalog-item-media/confirm-upload/."
            ),
            status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def perform_update(self, serializer, **kwargs):
        """Demote the current primary when a new one is set.

        Same reasoning as the default variant: a partial unique constraint permits one
        primary per item, and making the client clear the old one first turns an
        ordinary edit into a race.
        """
        with transaction.atomic():
            if serializer.validated_data.get("is_primary"):
                CatalogItemMedia.objects.filter(
                    item_id=serializer.instance.item_id, is_primary=True
                ).exclude(pk=serializer.instance.pk).update(is_primary=False)
            super().perform_update(serializer, **kwargs)

    @extend_schema(
        summary="Step 1 — get a presigned upload URL",
        description=(
            "Creates a PENDING media row and returns `{media_id, upload_url, "
            "expires_in_minutes}`. PUT the file to `upload_url` with the same "
            "`Content-Type` you declared, then call `confirm-upload`.\n\n"
            "**No object key is returned.** It is built server-side from the tenant "
            "and item ids and read back from this row at confirm time."
        ),
        request=CatalogMediaUploadRequestSerializer,
        responses={200: None},
    )
    @action(detail=False, methods=["post"], url_path="request-upload-url")
    def request_upload_url(self, request):
        serializer = CatalogMediaUploadRequestSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        item = data["item"]

        media = CatalogItemMedia(
            tenant_id=item.tenant_id,
            item=item,
            media_type=data["media_type"],
            original_name=data["filename"][:255],
            extension=data["extension"],
            file_size_in_bytes=data["file_size_in_bytes"],
            alt_text=data["alt_text"],
            upload_status=CatalogItemMedia.UploadStatus.PENDING,
        )
        # The filename is sanitised to a basename inside the key builder, so a crafted
        # value cannot steer the key outside this tenant's prefix.
        object_key = build_tenant_object_key(
            item.tenant_id,
            f"catalog/{item.pk}",
            data["filename"],
            unique_id=media.id,
        )
        media.file.name = object_key
        media.save()

        try:
            upload_url = S3StorageService().get_presigned_put_url(
                object_key, data["content_type"], data["file_size_in_bytes"]
            )
        except Exception:
            logger.exception("Could not presign catalog media upload for item %s", item.pk)
            media.upload_status = CatalogItemMedia.UploadStatus.FAILED
            media.save(update_fields=["upload_status"])
            return self.error(
                message="Could not generate an upload URL. Try again.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return self.success(
            data={
                "media_id": str(media.id),
                "upload_url": upload_url,
                "expires_in_minutes": 15,
            },
            message="Upload URL generated successfully",
        )

    @extend_schema(
        summary="Step 2 — confirm the upload",
        description=(
            "Body is `{\"media_id\": \"…\"}` and nothing else. Verifies the object "
            "actually arrived, records its real size, and flips the row to CONFIRMED. "
            "Only CONFIRMED media is served."
        ),
        request=CatalogMediaUploadConfirmSerializer,
        responses={200: CatalogItemMediaSerializer},
    )
    @action(detail=False, methods=["post"], url_path="confirm-upload")
    def confirm_upload(self, request):
        serializer = CatalogMediaUploadConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Resolved through the tenant-scoped queryset, so another tenant's media_id is
        # simply not found. 404 rather than 403 — a 403 would confirm it exists.
        media = (
            self.get_queryset()
            .filter(pk=serializer.validated_data["media_id"])
            .first()
        )
        if media is None:
            return self.error(
                message="No such upload.", status_code=status.HTTP_404_NOT_FOUND
            )

        if media.upload_status == CatalogItemMedia.UploadStatus.CONFIRMED:
            # Idempotent: a retried confirm after a dropped response must not fail.
            return self.success(
                data=self.get_serializer(media).data,
                message="Upload already confirmed",
            )

        storage = S3StorageService()
        try:
            exists = storage.verify_object_exists(media.file.name)
        except Exception:
            logger.exception("Could not verify catalog media object %s", media.pk)
            return self.error(
                message="Could not verify the upload. Try again.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if not exists:
            media.upload_status = CatalogItemMedia.UploadStatus.FAILED
            media.save(update_fields=["upload_status"])
            return self.error(
                message="The file was not found in storage. Upload it and try again.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            actual_size = storage.get_file_disk_usage(media.file.name)
            if isinstance(actual_size, dict):
                actual_size = actual_size.get("size_in_bytes", media.file_size_in_bytes)
            media.file_size_in_bytes = int(actual_size or media.file_size_in_bytes)
        except Exception:
            # The declared size is a fallback, not a guarantee — worth recording, not
            # worth failing a confirmed upload over.
            logger.warning("Could not read the stored size of catalog media %s", media.pk)

        with transaction.atomic():
            if request.data.get("is_primary") or media.is_primary:
                CatalogItemMedia.objects.filter(
                    item_id=media.item_id, is_primary=True
                ).exclude(pk=media.pk).update(is_primary=False)
                media.is_primary = True
            media.upload_status = CatalogItemMedia.UploadStatus.CONFIRMED
            media.extension = media.extension or os.path.splitext(media.original_name)[1].lower()
            media.save(
                update_fields=[
                    "upload_status",
                    "file_size_in_bytes",
                    "is_primary",
                    "extension",
                ]
            )

        return self.success(
            data=self.get_serializer(media).data, message="Upload confirmed"
        )

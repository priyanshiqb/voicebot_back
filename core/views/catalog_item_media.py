import logging
import os

from django.conf import settings
from django.db import transaction
from drf_spectacular.utils import extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers, status
from rest_framework.decorators import action

from core.models.catalog_item_media import CatalogItemMedia
from core.serializers.catalog_item_media import (
    CatalogItemMediaListSerializer,
    CatalogItemMediaSerializer,
    CatalogMediaUploadConfirmSerializer,
    CatalogMediaUploadRequestSerializer,
)
from utils.files.sizes import format_size_limit
from core.serializers.uploads import upload_ticket_fields
from core.services import uploads as upload_service
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from utils.blob_storage.keys import build_tenant_object_key
from utils.blob_storage.service import AzureBlobStorageService

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
        summary="Step 1 — get a presigned upload ticket",
        description=(
            "Creates a PENDING media row and returns `{media_id, upload_method, "
            "upload_url, upload_fields, expires_in_minutes, max_bytes}`. Then call "
            "`confirm-upload` with the `media_id`.\n\n"
            "**Follow `upload_method`, do not assume it** — it is deployment "
            "configuration and can change without an API version bump:\n\n"
            "- `POST` — send `upload_fields` as `multipart/form-data` with the file "
            "part **last**; success is `204`. `max_bytes` is signed into the policy, "
            "so the store itself rejects anything larger.\n"
            "- `PUT` — send the body raw with the `Content-Type` you declared; success "
            "is `200`. `max_bytes` is advisory here and enforced at confirm.\n\n"
            "**No object key is returned in either mode.** It is built server-side "
            "from the tenant and item ids and read back from this row at confirm time."
        ),
        request=CatalogMediaUploadRequestSerializer,
        responses={
            200: inline_serializer(
                name="CatalogMediaUploadTicketResponse",
                fields={
                    "status": serializers.BooleanField(),
                    "message": serializers.CharField(),
                    "data": inline_serializer(
                        name="CatalogMediaUploadTicket",
                        fields={
                            "media_id": serializers.UUIDField(),
                            **upload_ticket_fields(),
                        },
                    ),
                },
            )
        },
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

        # The ceiling is handed to the ticket, not just checked against the declaration:
        # under POST it becomes a signed condition the object store enforces itself, so
        # an oversize body is refused mid-upload rather than discovered at confirm.
        max_bytes = getattr(settings, "CATALOG_MAX_MEDIA_BYTES", 10 * 1024 * 1024)
        try:
            ticket = upload_service.build_upload_ticket(
                object_key,
                data["content_type"],
                max_bytes=max_bytes,
                storage=AzureBlobStorageService(),
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
            data={"media_id": str(media.id), **ticket},
            message="Upload URL generated successfully",
        )

    @extend_schema(
        summary="Step 2 — confirm the upload",
        description=(
            "Body is `{\"media_id\": \"…\"}` and nothing else. Verifies the object "
            "actually arrived, measures it, and flips the row to CONFIRMED. Only "
            "CONFIRMED media is served.\n\n"
            "The **measured** size is what is stored and what the size limit is applied "
            "to. `file_size_in_bytes` at step 1 is only a declaration — a presigned PUT "
            "carries no signed length — so an object that overruns the limit is deleted "
            "here and the row marked FAILED, with a `400`."
        ),
        request=CatalogMediaUploadConfirmSerializer,
        responses={200: CatalogItemMediaSerializer},
    )
    @staticmethod
    def _purge(storage, media):
        """Delete a rejected upload's object.

        Safe because `media` was resolved through the tenant-scoped queryset, so the key
        came off our own row and was never client-supplied. Deleting a key the caller
        handed in is precisely the shape of the old Knowledge Base cross-tenant delete;
        the ordering — authorize, then purge — is what keeps them different.

        A purge failure must not change the outcome: an orphaned object is a storage
        bill, a confirmed bad upload is a bypassed control.
        """
        try:
            storage.delete_blob_direct(media.file.name)
        except Exception:
            logger.warning(
                "Rejected catalog media %s could not be purged; object %s is now "
                "orphaned.",
                media.pk,
                media.file.name,
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

        storage = AzureBlobStorageService()
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

        # This is where the size ceiling is actually enforced. The serializer checked
        # `file_size_in_bytes` at step 1, but that is the client's *claim*: the presigned
        # PUT does not carry a signed ContentLength, so nothing stops a URL issued for
        # 1 MB from accepting 500 MB.
        #
        # Which means a failure to read the real size has to fail closed. Recording the
        # declared value and carrying on — what this used to do — turns "make the HEAD
        # request fail" into the way around the limit. Nothing is lost by returning 503:
        # the row is still PENDING and the object is still there, so a retry succeeds.
        try:
            stats = storage.get_file_disk_usage(media.file.name)
            actual_size = int(stats.get("total_size_bytes") or 0)
        except Exception:
            logger.exception("Could not read the stored size of catalog media %s", media.pk)
            return self.error(
                message="Could not verify the upload. Try again.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        max_bytes = getattr(settings, "CATALOG_MAX_MEDIA_BYTES", 10 * 1024 * 1024)
        if actual_size > max_bytes:
            self._purge(storage, media)
            media.upload_status = CatalogItemMedia.UploadStatus.FAILED
            media.save(update_fields=["upload_status"])
            return self.error(
                message=f"At most {format_size_limit(max_bytes)} per file.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        # What the file claims to be versus what it is. Both `content_type` and the
        # extension are client strings the allowlist checked as strings; neither has
        # ever been compared against the bytes. Fails closed for the same reason the
        # size check does — otherwise breaking the read is the way around it.
        extension = media.extension or os.path.splitext(media.original_name)[1].lower()
        try:
            mismatch, detected = upload_service.content_mismatch(
                storage, media.file.name, extension
            )
        except Exception:
            logger.exception("Could not read the head bytes of catalog media %s", media.pk)
            return self.error(
                message="Could not verify the upload. Try again.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if mismatch:
            self._purge(storage, media)
            media.upload_status = CatalogItemMedia.UploadStatus.FAILED
            media.save(update_fields=["upload_status"])
            return self.error(
                message=(
                    f"That file is not {extension or 'the declared type'} — its "
                    f"contents look like {detected}. Upload the real file."
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        media.file_size_in_bytes = actual_size or media.file_size_in_bytes

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

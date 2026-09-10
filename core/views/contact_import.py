import logging
import os

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.throttling import UserRateThrottle

from core.models.contact_import import (
    ContactImportFile,
    ContactImportRow,
    ContactImportSession,
)
from core.serializers.contact_import import (
    BulkResolveSerializer,
    CommitContactImportSerializer,
    ConfirmContactUploadSerializer,
    ContactImportFileSerializer,
    ContactImportRowSerializer,
    ContactImportRowUpdateSerializer,
    ContactImportSessionSerializer,
    RequestContactUploadUrlSerializer,
    StartContactImportSerializer,
)
from core.serializers.uploads import upload_ticket_fields
from core.services import contact_import as import_service
from core.services import uploads as upload_service
from core.tasks import commit_contact_import, parse_contact_import
from onboard.serializers.onboarding import GeneralResponseSerializer
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from utils.files.sizes import format_size_limit
from utils.blob_storage.keys import build_tenant_object_key
from utils.blob_storage.service import AzureBlobStorageService
from utils.views.base_model_viewset import BaseViewSet

logger = logging.getLogger(__name__)


class ContactImportThrottle(UserRateThrottle):
    """Parsing and committing are the expensive endpoints; keep them bounded."""

    scope = "contact_import"

    def get_rate(self):
        return "30/min"


@extend_schema(tags=["Contact Import"])
@extend_schema_view(
    list=extend_schema(
        summary="List Import Sessions",
        description="All import sessions for the tenant. Sessions are shared, not private.",
    ),
    retrieve=extend_schema(
        summary="Get Import Session",
        description=(
            "Session detail with row counts. Poll this after starting an import or a "
            "commit — both run asynchronously and report progress through `status`."
        ),
    ),
)
class ContactImportViewSet(TenantBaseViewSet):
    """
    Staged contact import: upload -> parse -> review -> resolve -> commit.

    Sessions are tenant-wide. Any colleague can review and commit one, so every
    transition is attributed (`created_by`, `committed_by`, `discarded_by`) and guarded
    by a status check under a row lock.
    """

    queryset = ContactImportSession.objects.all()
    serializer_class = ContactImportSessionSerializer
    filterset_fields = ["status"]
    ordering_fields = ["created_at", "updated_at", "status"]
    ordering = ["-created_at"]
    select_related_fields = ["import_file", "created_by", "committed_by"]
    http_method_names = ["get", "post", "head", "options"]

    def get_throttles(self):
        if self.action in {"create", "commit"}:
            return [ContactImportThrottle()]
        return super().get_throttles()

    def _tenant(self):
        user = self.request.user
        try:
            return user.tenant_mapping.tenant
        except Exception:
            return None

    def _get_session(self):
        """
        Fetch the session without running the list filterset.

        DRF's get_object() calls filter_queryset(), so a `?status=` intended for the
        rows endpoint would be applied to the session queryset instead and 400 on an
        unrecognised session status.
        """
        from django.shortcuts import get_object_or_404

        session = get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])
        self.check_object_permissions(self.request, session)
        return session

    # ------------------------------------------------------------------
    # Template
    # ------------------------------------------------------------------

    @extend_schema(
        summary="Download the contact template",
        description=(
            "Headers plus one example row. Tags are pipe-delimited (`vip|lead`); "
            "phone numbers should be in international format unless you pass a region. "
            "Returns CSV by default, or an Excel workbook if `?file_format=xlsx` is "
            "passed. The filename always matches the bytes."
        ),
        parameters=[
            OpenApiParameter(name="file_format", required=False, type=str, description="csv or xlsx. Default is csv."),
        ],
        responses={200: bytes},
    )
    @action(detail=False, methods=["get"], url_path="template")
    def template(self, request, *args, **kwargs):
        """
        CSV is the default, and the format decides the filename.

        The default was briefly xlsx while clients still saved the download as
        `contacts_template.csv`, which put a zip container behind a `.csv` name — Excel
        sniffs the content, sees the mismatch, and refuses to open it without the
        "format and extension don't match" warning. Anything that varies here has to
        vary in both places at once, so the two are derived from one value below.
        """
        file_format = (request.query_params.get("file_format") or "csv").lower()

        if file_format == "xlsx":
            body = import_service.build_template_xlsx()
            content_type = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            extension = "xlsx"
        else:
            body = import_service.build_template_csv()
            # charset is explicit so the BOM the body carries is not second-guessed.
            content_type = "text/csv; charset=utf-8"
            extension = "csv"

        response = HttpResponse(body, content_type=content_type)
        response["Content-Disposition"] = (
            f'attachment; filename="contacts_template.{extension}"'
        )
        return response

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    @extend_schema(
        summary="Request a secure upload URL",
        description=(
            "Creates a pending import file and returns a presigned PUT URL.\n\n"
            "The object key is held server-side and is never accepted back from the "
            "client — confirm the upload with the returned `file_id`."
        ),
        request=RequestContactUploadUrlSerializer,
        responses={
            200: inline_serializer(
                name="ContactUploadUrlResponse",
                fields={
                    "status": serializers.BooleanField(),
                    "message": serializers.CharField(),
                    "data": inline_serializer(
                        name="ContactUploadUrlData",
                        fields={
                            "file_id": serializers.UUIDField(),
                            **upload_ticket_fields(),
                            "object_key": serializers.CharField(),
                        },
                    ),
                },
            ),
            400: OpenApiResponse(response=GeneralResponseSerializer),
        },
    )
    @action(detail=False, methods=["post"], url_path="request-upload-url")
    def request_upload_url(self, request, *args, **kwargs):
        serializer = RequestContactUploadUrlSerializer(data=request.data)
        if not serializer.is_valid():
            first_error = next(iter(serializer.errors.values()))[0]
            return self.error(
                message=str(first_error), status_code=status.HTTP_400_BAD_REQUEST
            )

        tenant = self._tenant()
        if tenant is None:
            return self.error(
                message="Your user account is not associated with any active tenant.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

        filename = serializer.validated_data["filename"]
        content_type = serializer.validated_data["content_type"]
        size = serializer.validated_data["file_size_in_bytes"]

        import_file = ContactImportFile(
            tenant=tenant,
            original_name=filename[:255],
            extension=os.path.splitext(filename)[1].lower()[:10],
            file_size_in_bytes=size,
            upload_status=ContactImportFile.UploadStatus.PENDING,
            created_by=request.user,
        )
        # The key is derived from the row's own id and a sanitized filename, then
        # stored. Nothing about it is client-controlled.
        object_key = build_tenant_object_key(
            tenant.id, "contact_imports", filename, unique_id=import_file.id
        )
        import_file.file.name = object_key
        import_file.save()

        # Same ceiling confirm-upload applies, handed to the ticket so that under POST
        # the object store enforces it during the upload instead of after it.
        max_bytes = getattr(
            settings, "CONTACT_IMPORT_MAX_FILE_BYTES", 10 * 1024 * 1024
        )
        try:
            ticket = upload_service.build_upload_ticket(
                object_key,
                content_type,
                max_bytes=max_bytes,
                storage=AzureBlobStorageService(),
            )
        except Exception:
            logger.exception("Failed to generate contact import upload URL")
            import_file.upload_status = ContactImportFile.UploadStatus.FAILED
            import_file.save(update_fields=["upload_status"])
            return self.error(
                message="Failed to generate upload URL.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return self.success(
            data={
                "file_id": str(import_file.id),
                **ticket,
                # Deprecated: kept so existing clients that echo it back keep working.
                "object_key": object_key
            },
            message="Upload URL generated successfully.",
        )

    @extend_schema(
        summary="Confirm an upload",
        description="Verifies the object landed in storage and marks the file ready to import.",
        request=ConfirmContactUploadSerializer,
        responses={
            200: ContactImportFileSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer),
            404: OpenApiResponse(response=GeneralResponseSerializer),
        },
    )
    @action(detail=False, methods=["post"], url_path="confirm-upload")
    def confirm_upload(self, request, *args, **kwargs):
        serializer = ConfirmContactUploadSerializer(data=request.data)
        if not serializer.is_valid():
            first_error = next(iter(serializer.errors.values()))[0]
            return self.error(
                message=str(first_error), status_code=status.HTTP_400_BAD_REQUEST
            )

        tenant = self._tenant()
        if tenant is None:
            return self.error(
                message="Your user account is not associated with any active tenant.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

        # Tenant-scoped lookup: another tenant's file_id simply does not resolve.
        import_file = ContactImportFile.objects.filter(
            id=serializer.validated_data["file_id"], tenant=tenant
        ).first()
        if import_file is None:
            return self.error(
                message="Import file not found.", status_code=status.HTTP_404_NOT_FOUND
            )

        storage = AzureBlobStorageService()
        object_key = import_file.file.name

        try:
            if not storage.verify_object_exists(object_key):
                return self.error(
                    message="File not found in storage. Did the upload succeed?",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            stats = storage.get_file_disk_usage(object_key)
            actual_size = stats.get("total_size_bytes", 0)
            max_size = getattr(
                settings, "CONTACT_IMPORT_MAX_FILE_BYTES", 10 * 1024 * 1024
            )
            if actual_size > max_size:
                storage.delete_blob_direct(object_key)
                import_file.upload_status = ContactImportFile.UploadStatus.FAILED
                import_file.save(update_fields=["upload_status"])
                return self.error(
                    message=(
                        f"File size exceeds the limit of {format_size_limit(max_size)}."
                    ),
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
        except Exception:
            logger.exception("Failed to verify contact import upload")
            return self.error(
                message="Failed to verify file upload.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # What the file claims to be versus what it is. Only `.csv` is accepted here and
        # CSV has no signature, so this fires only when the bytes positively identify
        # something else — a spreadsheet, an archive, an executable renamed to .csv.
        # Fails closed, as the size check above does.
        try:
            mismatch, detected = upload_service.content_mismatch(
                storage, object_key, import_file.extension
            )
        except Exception:
            logger.exception("Could not read the head bytes of import file %s", object_key)
            return self.error(
                message="Failed to verify file upload.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if mismatch:
            # Authorization already ran above, so this key is ours to delete.
            try:
                storage.delete_blob_direct(object_key)
            except Exception:
                logger.warning("Rejected import file %s could not be purged.", object_key)
            import_file.upload_status = ContactImportFile.UploadStatus.FAILED
            import_file.save(update_fields=["upload_status"])
            return self.error(
                message=(
                    f"That file is not {import_file.extension or 'the declared type'} — "
                    f"its contents look like {detected}. Upload the real file."
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        import_file.file_size_in_bytes = actual_size
        import_file.upload_status = ContactImportFile.UploadStatus.CONFIRMED
        import_file.save(update_fields=["file_size_in_bytes", "upload_status"])

        return self.success(
            data=ContactImportFileSerializer(import_file).data,
            message="Upload confirmed.",
        )

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    @extend_schema(
        summary="Start an import",
        description=(
            "Creates a session and queues parsing. Returns 202 — poll the session "
            "until `status` becomes READY, then review the rows.\n\n"
            "A region is required: pass `default_region`, or configure "
            "`CONTACT_DEFAULT_PHONE_REGION` on the server."
        ),
        request=StartContactImportSerializer,
        responses={
            202: ContactImportSessionSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer),
        },
    )
    def create(self, request, *args, **kwargs):
        serializer = StartContactImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        tenant = self._tenant()
        if tenant is None:
            return self.error(
                message="Your user account is not associated with any active tenant.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

        import_file = ContactImportFile.objects.filter(
            id=data["file_id"], tenant=tenant
        ).first()
        if import_file is None:
            return self.error(
                message="Import file not found.", status_code=status.HTTP_404_NOT_FOUND
            )

        if import_file.upload_status != ContactImportFile.UploadStatus.CONFIRMED:
            return self.error(
                message="Confirm the upload before starting the import.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        # One file backs one session, or two parses race on the same staging rows.
        if ContactImportSession.objects.filter(import_file=import_file).exists():
            return self.error(
                message="An import has already been started for this file.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        max_open = getattr(settings, "CONTACT_IMPORT_MAX_OPEN_SESSIONS", 3)
        open_count = ContactImportSession.objects.filter(
            tenant=tenant, status__in=ContactImportSession.OPEN_STATUSES
        ).count()
        if open_count >= max_open:
            return self.error(
                message=(
                    f"You already have {open_count} import(s) in progress. "
                    "Finish or discard one before starting another."
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        session = ContactImportSession.objects.create(
            tenant=tenant,
            import_file=import_file,
            default_region=data["default_region"],
            conflict_strategy=data.get(
                "conflict_strategy", ContactImportSession.ConflictStrategy.SKIP
            ),
            column_map=data.get("column_map") or {},
            created_by=request.user,
        )

        # Queue only once the row is committed, or the worker can outrun the write.
        transaction.on_commit(lambda: parse_contact_import.delay(str(session.id)))

        return self.success(
            data=ContactImportSessionSerializer(session).data,
            message="Import started. Poll this session until it is ready for review.",
            status_code=status.HTTP_202_ACCEPTED,
        )

    # ------------------------------------------------------------------
    # Review
    # ------------------------------------------------------------------

    @extend_schema(
        summary="List rows in a session",
        description="Staged rows, filterable by status so the UI can show conflicts first.",
        parameters=[
            OpenApiParameter(name="status", required=False, type=str),
            OpenApiParameter(name="search", required=False, type=str),
        ],
        responses={200: ContactImportRowSerializer(many=True)},
    )
    @action(detail=True, methods=["get"], url_path="rows")
    def rows(self, request, *args, **kwargs):
        session = self._get_session()
        queryset = ContactImportRow.all_objects.filter(session=session).order_by(
            "row_number"
        )

        row_status = request.query_params.get("status")
        if row_status:
            queryset = queryset.filter(status=row_status.upper())

        search = request.query_params.get("search")
        if search:
            queryset = queryset.filter(phone_e164__icontains=search)

        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(
                ContactImportRowSerializer(page, many=True).data
            )

        return self.success(
            data=ContactImportRowSerializer(queryset, many=True).data,
            message="Listed successfully",
        )

    @extend_schema(
        summary="Bulk-resolve rows",
        description=(
            "Apply one resolution to every row with the given status. Resolving "
            "hundreds of conflicts one PATCH at a time is not workable."
        ),
        request=BulkResolveSerializer,
        responses={200: OpenApiResponse(response=GeneralResponseSerializer)},
    )
    @action(detail=True, methods=["post"], url_path="bulk-resolve")
    def bulk_resolve(self, request, *args, **kwargs):
        session = self._get_session()
        if session.status != ContactImportSession.Status.READY:
            return self.error(
                message="This import is not open for review.",
                status_code=status.HTTP_409_CONFLICT,
            )

        serializer = BulkResolveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        target_status = serializer.validated_data["status"]
        resolution = serializer.validated_data["resolution"]

        rows = ContactImportRow.all_objects.filter(
            session=session, status=target_status
        )

        updated = 0
        for row in rows:
            row.resolution = resolution
            import_service.revalidate_row(row, actor=request.user)
            updated += 1

        _refresh_session_counts(session)

        return self.success(
            data={"updated": updated},
            message=f"{updated} row(s) updated.",
        )

    # ------------------------------------------------------------------
    # Commit / discard
    # ------------------------------------------------------------------

    @extend_schema(
        summary="Commit an import",
        description=(
            "Claims the session and queues the write. Returns 202 — poll until "
            "`status` is COMPLETED or PARTIAL.\n\n"
            "Refuses while conflicts remain unless `ignore_unresolved` is true."
        ),
        request=CommitContactImportSerializer,
        responses={
            202: ContactImportSessionSerializer,
            409: OpenApiResponse(response=GeneralResponseSerializer),
        },
    )
    @action(detail=True, methods=["post"], url_path="commit")
    def commit(self, request, *args, **kwargs):
        session = self._get_session()
        serializer = CommitContactImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            import_service.begin_commit(
                session.id,
                actor=request.user,
                ignore_unresolved=serializer.validated_data["ignore_unresolved"],
            )
        except import_service.CommitRejected as exc:
            return self.error(
                message=str(exc), status_code=status.HTTP_409_CONFLICT
            )

        transaction.on_commit(lambda: commit_contact_import.delay(str(session.id)))

        session.refresh_from_db()
        return self.success(
            data=ContactImportSessionSerializer(session).data,
            message="Commit started. Poll this session for the result.",
            status_code=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        summary="Discard an import",
        description="Deletes the staged rows and the uploaded file. The session remains as an audit record.",
        request=None,
        responses={
            200: ContactImportSessionSerializer,
            409: OpenApiResponse(response=GeneralResponseSerializer),
        },
    )
    @action(detail=True, methods=["post"], url_path="discard")
    def discard(self, request, *args, **kwargs):
        session = self._get_session()
        try:
            session = import_service.discard_session(session.id, actor=request.user)
        except import_service.CommitRejected as exc:
            return self.error(message=str(exc), status_code=status.HTTP_409_CONFLICT)

        return self.success(
            data=ContactImportSessionSerializer(session).data,
            message="Import discarded.",
        )


def _refresh_session_counts(session):
    """Recompute the denormalized counts the UI reads."""
    from django.db.models import Count

    counts = dict(
        ContactImportRow.all_objects.filter(session=session)
        .values_list("status")
        .annotate(total=Count("id"))
    )

    conflict = sum(
        counts.get(value, 0) for value in ContactImportRow.CONFLICT_STATUSES
    )
    session.conflict_rows = conflict
    session.invalid_rows = counts.get(ContactImportRow.Status.INVALID, 0)
    session.valid_rows = counts.get(ContactImportRow.Status.PENDING, 0) + counts.get(
        ContactImportRow.Status.RESOLVED, 0
    )
    session.save(update_fields=["conflict_rows", "invalid_rows", "valid_rows", "updated_at"])


@extend_schema(tags=["Contact Import"])
class ContactImportRowViewSet(BaseViewSet):
    """
    Edit a single staged row.

    Flat route rather than nested under the session: drf-nested-routers is not a
    dependency and every other url module here is a flat DefaultRouter.
    """

    queryset = ContactImportRow.all_objects.all()
    serializer_class = ContactImportRowSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return ContactImportRow.all_objects.all()
        try:
            tenant = user.tenant_mapping.tenant
        except Exception:
            return ContactImportRow.all_objects.none()
        return ContactImportRow.all_objects.filter(tenant=tenant)

    @extend_schema(exclude=True)
    def bulk_delete(self, request, *args, **kwargs):
        from rest_framework.exceptions import MethodNotAllowed

        raise MethodNotAllowed("POST")

    @extend_schema(exclude=True)
    def dropdown(self, request, *args, **kwargs):
        from rest_framework.exceptions import MethodNotAllowed

        raise MethodNotAllowed("GET")

    @extend_schema(
        summary="Edit a staged row",
        description=(
            "Update the row's data and/or choose a resolution.\n\n"
            "`status` is not writable — it is re-derived from validation after the "
            "edit, so a row can only become RESOLVED when the conflict is genuinely "
            "gone or a resolution has been chosen."
        ),
        request=ContactImportRowUpdateSerializer,
        responses={
            200: ContactImportRowSerializer,
            409: OpenApiResponse(response=GeneralResponseSerializer),
        },
    )
    def partial_update(self, request, *args, **kwargs):
        row = self.get_object()
        session = row.session

        if session.status != ContactImportSession.Status.READY:
            return self.error(
                message="This import is not open for review.",
                status_code=status.HTTP_409_CONFLICT,
            )

        serializer = ContactImportRowUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if "parsed_data" in data:
            merged = dict(row.parsed_data or {})
            merged.update(data["parsed_data"])
            row.parsed_data = merged

        if "resolution" in data:
            row.resolution = data["resolution"] or ""

        import_service.revalidate_row(row, actor=request.user)
        _refresh_session_counts(session)

        return self.success(
            data=ContactImportRowSerializer(row).data, message="Row updated."
        )

    def update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.partial_update(request, *args, **kwargs)

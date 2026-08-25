from onboard.constants.entitlement_resource import EntitlementResource
from tenant.common.views.entitlement_guard import EntitlementGuardMixin
import logging

from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework import status
from drf_spectacular.utils import extend_schema, OpenApiExample, extend_schema_view, OpenApiResponse, OpenApiParameter, inline_serializer
from drf_spectacular.types import OpenApiTypes
from rest_framework import serializers
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.knowledge_base import KnowledgeBase, KBFiles, KBWebsite
import os
from core.serializers.uploads import upload_ticket_fields
from core.serializers.kb_files import KBFilesSerializer, RequestUploadUrlSerializer, ConfirmUploadSerializer
from core.services import uploads as upload_service
from utils.s3_storage.service import S3StorageService
from utils.s3_storage.keys import (
    build_tenant_object_key,
    is_object_key_owned_by_tenant,
)
from core.serializers.knowledge_base import (
    KnowledgeBaseSerializer,
    KnowledgeBaseListSerializer,
    KnowledgeBaseDropdownSerializer,
    KnowledgeBaseNameCheckSerializer,
    KnowledgeBaseUrlCheckSerializer,
    UniquenessCheckResponseSerializer,
)
from core.serializers.kb_etl import UpdateETLStatusSerializer
from onboard.serializers.onboarding import GeneralResponseSerializer
from user.models import CustomUser
from user.authentication import ETLSystemTokenAuthentication
from utils.permissions.is_etl_user import IsETLUser
from rest_framework.exceptions import PermissionDenied
from core.models.agent import Agent
from django.conf import settings

logger = logging.getLogger(__name__)



@extend_schema(tags=["Knowledge Base"])
@extend_schema_view(
    list=extend_schema(
        summary="List Knowledge Bases",
        description="Get paginated list of knowledge bases under the active tenant context.",
        responses={
            200: KnowledgeBaseListSerializer(many=True),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    ),
    retrieve=extend_schema(
        summary="Get Knowledge Base",
        description="Get specific knowledge base details by ID.",
        responses={
            200: KnowledgeBaseSerializer,
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Knowledge base not found.")
        }
    ),
    destroy=extend_schema(
        summary="Delete Knowledge Base",
        description="Soft-delete an existing knowledge base by ID. Requires ?force=true if connected to an agent.",
        parameters=[
            OpenApiParameter(name="force", type=str, location=OpenApiParameter.QUERY, description="Pass 'true' to force deletion if connected to an agent.", required=False)
        ],
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Knowledge base soft-deleted successfully."),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Warning: Connected to agent (needs force=true)."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Knowledge base not found.")
        }
    ),
    dropdown=extend_schema(
        summary="Get Dropdown Options",
        description="Get lightweight label/value selection pairs representing active knowledge bases.",
        responses={
            200: KnowledgeBaseDropdownSerializer(many=True)
        }
    )
)
class KnowledgeBaseViewSet(EntitlementGuardMixin, TenantBaseViewSet):
    """
    Knowledge Base Management API.
    Provides complete row-level isolation and multi-tenant scoping for aggregate Knowledge Bases.
    """
    entitlement_resource = EntitlementResource.KNOWLEDGE_BASE

    def entitlement_requests(self, serializer):
        """A knowledge base plus every website crawled into it.

        Websites arrive nested in the same payload, so checking only the container
        would let a tenant hold any number of crawled pages by putting them all in
        one knowledge base.
        """
        websites = serializer.validated_data.get("websites") or []
        return {
            EntitlementResource.KNOWLEDGE_BASE: 1,
            EntitlementResource.CRAWL_PAGE: len(websites),
        }

    queryset = KnowledgeBase.objects.all()
    serializer_class = KnowledgeBaseSerializer
    list_serializer_class = KnowledgeBaseListSerializer
    dropdown_serializer_class = KnowledgeBaseDropdownSerializer

    filterset_fields = ["is_processed", "processing_status"]
    search_fields = ["name"]
    ordering_fields = ["created_at", "updated_at", "name", "is_processed", "processing_status"]
    ordering = ["-created_at"]
    select_related_fields = ["tenant"]
    prefetch_related_fields = ["texts", "files", "websites", "question_answers"]

    def get_queryset(self):
        user = self.request.user
        if getattr(user, "user_type", None) == CustomUser.UserType.ETL:
            return KnowledgeBase.objects.all()
        return super().get_queryset()

    def get_authenticators(self):
        action = None
        if hasattr(self, "request") and self.request:
            action = getattr(self, "action_map", {}).get(self.request.method.lower())
        if not action:
            action = getattr(self, "action", None)

        if action == "update_etl_status":
            return [ETLSystemTokenAuthentication()]
        return super().get_authenticators()

    def get_permissions(self):
        action = getattr(self, "action", None)
        if action == "update_etl_status":
            return [IsETLUser()]
        return super().get_permissions()

    @extend_schema(
        summary="Upload a Knowledge Base document file",
        description="""
        Uploads a raw binary document file scoped under the caller's tenant.
        This represents the first step of the **Two-Step Ingestion Pattern**:
        1. **Upload File**: Call this endpoint with a multipart form payload (`file`).
        2. **Response**: Capture the returned `"id"` (UUID).
        3. **Link File**: Pass the captured `"id"` in the `files` array when creating or updating a Knowledge Base.
        """,
        responses={
            201: KBFilesSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed, file parameter missing or invalid."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    )
    @action(
        detail=False,
        methods=["post"],
        parser_classes=[MultiPartParser, FormParser],
        serializer_class=KBFilesSerializer,
    )
    def upload_file(self, request, *args, **kwargs):
        try:
            tenant = request.user.tenant_mapping.tenant
        except:
            raise PermissionDenied("User must have an active tenant to upload files to a knowledge base.")
        serializer = KBFilesSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        # Force tenant scoping
        instance = serializer.save(tenant=tenant)
        return Response(
            {
                "status": True,
                "message": "File uploaded successfully",
                "data": serializer.data
            },
            status=status.HTTP_201_CREATED
        )

    @extend_schema(
        summary="Request a secure S3 upload URL",
        description="Generates a temporary Presigned PUT URL allowing the frontend to upload a file directly to the storage bucket.",
        request=RequestUploadUrlSerializer,
        responses={
            200: inline_serializer(
                name="RequestUploadUrlResponse",
                fields={
                    "status": serializers.BooleanField(),
                    "message": serializers.CharField(),
                    "data": inline_serializer(
                        name="RequestUploadUrlData",
                        fields={
                            **upload_ticket_fields(),
                            "object_key": serializers.CharField()
                        }
                    )
                }
            ),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed.")
        }
    )
    @action(detail=False, methods=["post"], url_path="request-upload-url")
    def request_upload_url(self, request, *args, **kwargs):
        serializer = RequestUploadUrlSerializer(data=request.data)
        if not serializer.is_valid():
            first_error = next(iter(serializer.errors.values()))[0]
            return Response({"status": False, "message": str(first_error)}, status=status.HTTP_400_BAD_REQUEST)
        
        filename = serializer.validated_data["filename"]
        content_type = serializer.validated_data["content_type"]
        file_size_in_bytes = serializer.validated_data["file_size_in_bytes"]
        tenant = request.user.tenant_mapping.tenant

        # Create the record up front so the object key can live server-side and be
        # looked up by id at confirm time. The client never needs to hand it back.
        kb_file = KBFiles(
            tenant=tenant,
            original_name=filename[:255],
            extension=os.path.splitext(filename)[1].lower(),
            upload_status=KBFiles.UploadStatus.PENDING,
            is_processed=False,
            processing_status='PENDING',
            created_by=request.user,
            updated_by=request.user,
        )
        # The filename is sanitized to a basename so a crafted value cannot steer the
        # key outside this tenant's prefix.
        object_key = build_tenant_object_key(
            tenant.id, "kb_files", filename, unique_id=kb_file.id
        )
        kb_file.file.name = object_key
        kb_file.save()

        # The same ceiling confirm-upload applies, handed to the ticket so that under
        # POST the object store enforces it during the upload instead of after it.
        max_bytes = getattr(settings, "DATA_UPLOAD_MAX_MEMORY_SIZE", 11 * 1024 * 1024)
        try:
            ticket = upload_service.build_upload_ticket(
                object_key,
                content_type,
                max_bytes=max_bytes,
                storage=S3StorageService(),
            )
        except Exception:
            logger.exception("Could not presign KB upload for tenant %s", tenant.id)
            kb_file.upload_status = KBFiles.UploadStatus.FAILED
            kb_file.save(update_fields=["upload_status"])
            return Response({"status": False, "message": "Failed to generate upload URL."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            "status": True,
            "message": "Upload URL generated successfully.",
            "data": {
                "file_id": str(kb_file.id),
                **ticket,
                # Deprecated: kept so existing clients that echo it back keep working.
                "object_key": object_key
            }
        }, status=status.HTTP_200_OK)

    @extend_schema(
        summary="Confirm a direct S3 upload",
        description="Verifies the file arrived in S3 and creates the Knowledge Base File record.",
        request=ConfirmUploadSerializer,
        responses={
            201: inline_serializer(
                name="ConfirmUploadResponse",
                fields={
                    "status": serializers.BooleanField(),
                    "message": serializers.CharField(),
                    "data": KBFilesSerializer()
                }
            ),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed or file missing in S3.")
        }
    )
    @action(detail=False, methods=["post"], url_path="confirm-upload")
    def confirm_upload(self, request, *args, **kwargs):
        serializer = ConfirmUploadSerializer(data=request.data)
        if not serializer.is_valid():
            first_error = next(iter(serializer.errors.values()))[0]
            return Response({"status": False, "message": str(first_error)}, status=status.HTTP_400_BAD_REQUEST)
            
        file_id = serializer.validated_data.get("file_id")
        object_key = serializer.validated_data.get("object_key")
        original_name = serializer.validated_data.get("original_name")

        # Security: resolve the tenant BEFORE touching S3. The oversize branch below
        # deletes the object it was handed, so an unauthorized key must never reach it.
        try:
            tenant = request.user.tenant_mapping.tenant
        except Exception:
            return Response({"status": False, "message": "Your user account is not associated with any active tenant."}, status=status.HTTP_403_FORBIDDEN)

        kb_file = None
        if file_id:
            # Preferred path: the key is read from a tenant-scoped row, so it is not
            # possible to name a file belonging to another tenant.
            kb_file = KBFiles.objects.filter(id=file_id, tenant=tenant).first()
            if kb_file is None:
                return Response({"status": False, "message": "File not found."}, status=status.HTTP_404_NOT_FOUND)
            object_key = kb_file.file.name
            original_name = original_name or kb_file.original_name
        else:
            # Deprecated path: still fully authorized, but log it so the transition
            # window can be closed once clients stop using it.
            logger.info(
                "confirm-upload called with deprecated object_key by user %s; "
                "clients should send file_id instead.",
                request.user.pk,
            )
            # Security: the key must live inside this tenant's namespace. Report 404
            # rather than 403 so the response cannot probe for another tenant's objects.
            if not is_object_key_owned_by_tenant(object_key, tenant.id, folder="kb_files"):
                return Response({"status": False, "message": "File not found in storage bucket. Did the upload succeed?"}, status=status.HTTP_404_NOT_FOUND)

        extension = serializer.validated_data.get("extension")
        if not extension:
            extension = os.path.splitext(original_name or "")[1].lower()

        storage_service = S3StorageService()

        # Security: verify the file actually exists in S3 before recording it
        try:
            exists = storage_service.verify_object_exists(object_key)
            if not exists:
                return Response({"status": False, "message": "File not found in storage bucket. Did the upload succeed?"}, status=status.HTTP_400_BAD_REQUEST)

            # Security: verify actual file size to enforce limit on direct S3 uploads
            file_stats = storage_service.get_file_disk_usage(object_key)
            max_size = getattr(settings, "DATA_UPLOAD_MAX_MEMORY_SIZE", 11 * 1024 * 1024)
            if file_stats.get("total_size_bytes", 0) > max_size:
                storage_service.delete_blob_direct(object_key)
                if kb_file is not None:
                    kb_file.upload_status = KBFiles.UploadStatus.FAILED
                    kb_file.save(update_fields=["upload_status"])
                return Response({"status": False, "message": "File size exceeds the limit of 10 MB."}, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            return Response({"status": False, "message": "Failed to verify file upload."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # What the file claims to be versus what it is. Matters more here than anywhere
        # else in the codebase: these documents are parsed by the ETL/RAG service, so a
        # .pdf that is actually something else is handed to a parser that did not expect
        # it. Fails closed — otherwise breaking the read is the way around the check.
        try:
            mismatch, detected = upload_service.content_mismatch(
                storage_service, object_key, extension
            )
        except Exception:
            logger.exception("Could not read the head bytes of KB object %s", object_key)
            return Response(
                {"status": False, "message": "Failed to verify file upload."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if mismatch:
            # Authorization already ran above, so this key is ours to delete.
            try:
                storage_service.delete_blob_direct(object_key)
            except Exception:
                logger.warning("Rejected KB object %s could not be purged.", object_key)
            if kb_file is not None:
                kb_file.upload_status = KBFiles.UploadStatus.FAILED
                kb_file.save(update_fields=["upload_status"])
            return Response(
                {
                    "status": False,
                    "message": (
                        f"That file is not {extension or 'the declared type'} — its "
                        f"contents look like {detected}. Upload the real file."
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if kb_file is None:
            # Legacy object_key path: no row was reserved up front, so create one now.
            kb_file = KBFiles(
                tenant=tenant,
                is_processed=False,
                processing_status='PENDING',
                created_by=request.user,
            )
            # We assign the raw object key to the file field. Django Storages handles the
            # resolution transparently, which prevents it from attempting to re-upload.
            kb_file.file.name = object_key

        kb_file.original_name = original_name or kb_file.original_name
        kb_file.extension = extension or kb_file.extension
        kb_file.upload_status = KBFiles.UploadStatus.CONFIRMED
        kb_file.file_size_in_bytes = file_stats.get("total_size_bytes", 0)
        kb_file.updated_by = request.user
        kb_file.save()

        response_serializer = KBFilesSerializer(kb_file)
        
        return Response({
            "status": True,
            "message": "File confirmed and saved successfully.",
            "data": response_serializer.data
        }, status=status.HTTP_201_CREATED)
        
    @extend_schema(
        summary="Discard uploaded Knowledge Base files",
        description="Permanently deletes an array of uploaded KB files and their associated S3 objects. Use this when a user uploads files but then discards the KB creation process.",
        request=inline_serializer(
            name="DiscardFilesRequest",
            fields={
                "file_ids": serializers.ListField(child=serializers.UUIDField(), required=True)
            }
        ),
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Files discarded successfully."),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="file_ids array is required or invalid."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    )
    @action(detail=False, methods=["post"], url_path="discard-files")
    def discard_files(self, request, *args, **kwargs):
        file_ids = request.data.get("file_ids", [])
        if not isinstance(file_ids, list) or not file_ids:
            return Response({"status": False, "message": "A valid list of file_ids is required."}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            tenant = request.user.tenant_mapping.tenant
            files = KBFiles.objects.filter(id__in=file_ids, tenant=tenant)
            deleted_count = 0
            for f in files:
                f.delete()
                deleted_count += 1
                
            return Response({"status": True, "message": f"{deleted_count} file(s) discarded successfully."}, status=status.HTTP_200_OK)
        except Exception:
            return Response({"status": False, "message": "Permission denied or error occurred."}, status=status.HTTP_403_FORBIDDEN)

    @extend_schema(
        summary="Update ETL status of a Knowledge Base component",
        description="Updates the ETL status of the knowledge base itself or one of its nested elements (file, website, text, qa).",
        request=UpdateETLStatusSerializer,
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="ETL status updated successfully."),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed or etl_id mismatch."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Knowledge Base or target component not found.")
        }
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="update-etl-status",
        serializer_class=UpdateETLStatusSerializer,
    )
    def update_etl_status(self, request, pk=None, *args, **kwargs):
        kb_instance = self.get_object()
        serializer = self.get_serializer(data=request.data, context={"kb_instance": kb_instance})
        serializer.is_valid(raise_exception=True)
        success = serializer.save()
        if not success:
            return Response(
                {
                    "status": False,
                    "message": "Failed to update ETL status. The etl_id does not match the current tracking etl_id."
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        return Response(
            {
                "status": True,
                "message": "ETL status updated successfully."
            },
            status=status.HTTP_200_OK
        )

    @extend_schema(
        summary="Create a new Knowledge Base",
        description="""
        ### Complete payload schemas & constraints per component type:
        
        1. **Texts (`texts` array)**:
           * `title` (string, **Required**): The main heading/title for this text block (Max 255 characters).
           * `description` (string, Optional): The detailed text content or guidelines body.
        
        2. **Files (`files` array)**:
           * `id` (UUID, **Required**): The UUID of the pre-uploaded document file (uploaded via `POST /api/v1/core/knowledge-base/upload_file/`).
        
        3. **Websites (`websites` array)**:
           * `url` (string, **Required**): Valid domain crawling target webpage URL. Scoped uniquely per tenant to prevent crawlers duplication.
        
        4. **Question Answers (`question_answers` array)**:
           * `answer` (string, **Required**): The primary answer block to return for associated queries.
           * `questions` (array of dicts, Optional): List of associated trigger question variations.
             * `question` (string, **Required**): Individual variation phrasing of the question.
        """,
        examples=[
            OpenApiExample(
                "Nested Creation Payload Example",
                value={
                    "name": "Acme Setup Guides",
                    "texts": [
                        {
                            "title": "Config Instructions",
                            "description": "Configure API credentials."
                        }
                    ],
                    "files": [
                        {
                            "id": "uuid-preuploaded-file-id"
                        }
                    ],
                    "websites": [
                        {
                            "url": "https://acme-setup.org/faq"
                        }
                    ],
                    "question_answers": [
                        {
                            "answer": "Yes, standard deep nested structures are supported.",
                            "questions": [
                                {
                                    "question": "Is nested creation supported?"
                                }
                            ]
                        }
                    ]
                },
                request_only=True
            ),
            OpenApiExample(
                "Successful Creation Response Example",
                value={
                    "status": True,
                    "message": "Created successfully",
                    "data": {
                        "id": "e4b2d561-71fb-48eb-bf2e-0a5682855cf8",
                        "tenant": "uuid-tenant-id",
                        "name": "Acme Setup Guides",
                        "texts": [
                            {
                                "id": "uuid-text-1",
                                "title": "Config Instructions",
                                "description": "Configure API credentials."
                            }
                        ],
                        "files": [
                            {
                                "id": "uuid-preuploaded-file-id",
                                "original_name": "setup.pdf"
                            }
                        ],
                        "websites": [
                            {
                                "id": "uuid-web-1",
                                "url": "https://acme-setup.org/faq"
                            }
                        ],
                        "question_answers": [
                            {
                                "id": "uuid-qa-1",
                                "answer": "Yes, standard deep nested structures are supported.",
                                "questions": [
                                    {
                                        "id": "uuid-q-1",
                                        "question": "Is nested creation supported?"
                                    }
                                ]
                            }
                        ],
                        "is_processed": False,
                        "processing_status": "PENDING"
                    },
                    "errors": None
                },
                response_only=True
            )
        ],
        responses={
            201: KnowledgeBaseSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @extend_schema(
        summary="Update a Knowledge Base (PUT)",
        description="""
        Performs a full update on the Knowledge Base.
        Supports explicit, granular transactional operations on M2M lists using the `"flag"` parameter:
        * **`create`**: Instantiates and links a new record (requires necessary fields).
        * **`update`**: Updates the existing record in-place by `id`.
        * **`delete`**: Safely soft-deletes and disassociates the record by `id`.
        * **No Flag Fallback**: Keeps the record associated and unchanged.
        """,
        examples=[
            OpenApiExample(
                "PUT Flag-based Update Payload Example",
                value={
                    "name": "Acme Updated Guides",
                    "texts": [
                        {
                            "id": "uuid-text-1",
                            "flag": "update",
                            "description": "Updated creds configurations."
                        },
                        {
                            "flag": "create",
                            "title": "CLI Guide",
                            "description": "Command line interface instructions."
                        },
                        {
                            "id": "uuid-text-2",
                            "flag": "delete"
                        }
                    ],
                    "files": [
                        {
                            "id": "uuid-another-preuploaded-file-id",
                            "flag": "create"
                        },
                        {
                            "id": "uuid-old-file-id",
                            "flag": "delete"
                        }
                    ]
                },
                request_only=True
            )
        ],
        responses={
            200: KnowledgeBaseSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Knowledge base not found.")
        }
    )
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @extend_schema(
        summary="Partial Update a Knowledge Base (PATCH)",
        description="""
        Performs a partial update on the Knowledge Base.
        Supports explicit, granular transactional operations on M2M lists using the `"flag"` parameter:
        * **`create`**: Instantiates and links a new record (requires necessary fields).
        * **`update`**: Updates the existing record in-place by `id`.
        * **`delete`**: Safely soft-deletes and disassociates the record by `id`.
        * **No Flag Fallback**: Keeps the record associated and unchanged.
        """,
        examples=[
            OpenApiExample(
                "PATCH Flag-based Update Payload Example",
                value={
                    "name": "Acme Updated Guides",
                    "texts": [
                        {
                            "id": "uuid-text-1",
                            "flag": "update",
                            "description": "Updated creds configurations."
                        },
                        {
                            "flag": "create",
                            "title": "CLI Guide",
                            "description": "Command line interface instructions."
                        },
                        {
                            "id": "uuid-text-2",
                            "flag": "delete"
                        }
                    ],
                    "files": [
                        {
                            "id": "uuid-another-preuploaded-file-id",
                            "flag": "create"
                        },
                        {
                            "id": "uuid-old-file-id",
                            "flag": "delete"
                        }
                    ]
                },
                request_only=True
            )
        ],
        responses={
            200: KnowledgeBaseSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Knowledge base not found.")
        }
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(
        summary="Check Name Uniqueness",
        description="Checks if a knowledge base name is unique under the active tenant context.",
        request=KnowledgeBaseNameCheckSerializer,
        responses={
            200: OpenApiResponse(response=UniquenessCheckResponseSerializer, description="Uniqueness check result."),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    )
    @action(detail=False, methods=["post"], url_path="check-name", url_name="check-name")
    def check_name_uniqueness(self, request):
        serializer = KnowledgeBaseNameCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        name = serializer.validated_data["name"]
        exclude_id = serializer.validated_data.get("exclude_id")

        user = request.user
        if user.is_superuser:
            qs = KnowledgeBase.objects.filter(name=name)
        else:
            try:
                active_tenant = user.tenant_mapping.tenant
            except Exception:
                raise PermissionDenied("Your user account is not associated with any active tenant.")
            qs = KnowledgeBase.objects.filter(name=name, tenant=active_tenant)

        if exclude_id:
            qs = qs.exclude(id=exclude_id)

        is_unique = not qs.exists()
        return self.success(data={"is_unique": is_unique})

    @extend_schema(
        summary="Check URL Uniqueness",
        description="Checks if a website URL is unique under the active tenant context.",
        request=KnowledgeBaseUrlCheckSerializer,
        responses={
            200: OpenApiResponse(response=UniquenessCheckResponseSerializer, description="Uniqueness check result."),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    )
    @action(detail=False, methods=["post"], url_path="check-url", url_name="check-url")
    def check_url_uniqueness(self, request):
        serializer = KnowledgeBaseUrlCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        url = serializer.validated_data["url"]
        exclude_id = serializer.validated_data.get("exclude_id")

        user = request.user
        if user.is_superuser:
            qs = KBWebsite.objects.filter(url=url)
        else:
            try:
                active_tenant = user.tenant_mapping.tenant
            except Exception:
                raise PermissionDenied("Your user account is not associated with any active tenant.")
            qs = KBWebsite.objects.filter(url=url, tenant=active_tenant)

        if exclude_id:
            qs = qs.exclude(id=exclude_id)

        is_unique = not qs.exists()
        return self.success(data={"is_unique": is_unique})

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        
        # Check if KB is connected to any active agent
        linked_agents = instance.agents.filter(is_deleted=False).values_list('bot_name', flat=True)
        if linked_agents:
            force = request.query_params.get("force", "").lower() == "true"
            if not force:
                agent_names = ", ".join(list(linked_agents)[:3])
                suffix = "..." if len(linked_agents) > 3 else ""
                return Response(
                    {
                        "status": False,
                        "warning": True,
                        "message": f"This knowledge base is connected to the following agents: {agent_names}{suffix}. Are you sure you want to delete it?",
                        "errors": {"detail": f"This knowledge base is currently in use by agents: {agent_names}{suffix}. Please confirm to force delete."}
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )
                
        return super().destroy(request, *args, **kwargs)

    @extend_schema(
        summary="Bulk Delete Knowledge Bases",
        description="Soft-delete multiple knowledge bases by their ID list. Requires ?force=true if any KB is connected to an agent.",
        parameters=[
            OpenApiParameter(name="force", type=str, location=OpenApiParameter.QUERY, description="Pass 'true' to force deletion if any KB is connected to an agent.", required=False)
        ],
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Bulk deleted successfully."),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Warning: Connected to agent (needs force=true)."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    )
    @action(detail=False, methods=["post"], url_path="bulk-delete", url_name="bulk-delete")
    def bulk_delete(self, request):
        ids = request.data.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return Response(
                {"status": False, "message": "ids must be a non-empty list", "errors": None},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        force = request.query_params.get("force", "").lower() == "true"
        if not force:
            # Scoped to what this caller can see, not to the raw `ids` they sent.
            # Filtering by `ids` answered an id from another tenant with "connected to
            # an agent" instead of the "Some ids are invalid" every other unknown id
            # gets, confirming both that the row exists and that it is in use.
            visible_ids = list(
                self.get_queryset().filter(id__in=ids, is_deleted=False)
                .values_list("id", flat=True)
            )
            used_kb_ids = Agent.objects.filter(
                knowledge_bases__id__in=visible_ids,
                is_deleted=False
            ).values_list('knowledge_bases__id', flat=True)

            if used_kb_ids:
                return Response(
                    {
                        "status": False,
                        "warning": True,
                        "message": "One or more selected knowledge bases are connected to an agent. Are you sure you want to delete them?",
                        "errors": {"detail": "Connected to agent. Pass force=true to proceed."}
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )
                
        return super().bulk_delete(request)


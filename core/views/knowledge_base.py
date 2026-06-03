from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework import status
from drf_spectacular.utils import extend_schema, OpenApiExample, extend_schema_view, OpenApiResponse
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.knowledge_base import KnowledgeBase, KBFiles
from core.serializers.kb_files import KBFilesSerializer
from core.serializers.knowledge_base import (
    KnowledgeBaseSerializer,
    KnowledgeBaseListSerializer,
    KnowledgeBaseDropdownSerializer
)
from onboard.serializers.onboarding import GeneralResponseSerializer


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
        description="Soft-delete an existing knowledge base by ID.",
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Knowledge base soft-deleted successfully."),
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
class KnowledgeBaseViewSet(TenantBaseViewSet):
    """
    Knowledge Base Management API.
    Provides complete row-level isolation and multi-tenant scoping for aggregate Knowledge Bases.
    """
    queryset = KnowledgeBase.objects.all()
    serializer_class = KnowledgeBaseSerializer
    list_serializer_class = KnowledgeBaseListSerializer
    dropdown_serializer_class = KnowledgeBaseDropdownSerializer

    filterset_fields = ["is_processed", "processing_status"]
    search_fields = ["name"]
    ordering_fields = ["created_at", "updated_at", "name"]
    ordering = ["-created_at"]
    select_related_fields = ["tenant"]
    prefetch_related_fields = ["texts", "files", "websites", "question_answers"]

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
        tenant = request.user.tenant_mapping.tenant
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

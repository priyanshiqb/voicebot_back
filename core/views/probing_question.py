from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiResponse
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.probing_question import ProbingQuestion
from core.serializers.probing_question import (
    ProbingQuestionSerializer,
    ProbingQuestionListSerializer,
    ProbingQuestionDropdownSerializer,
)
from onboard.serializers.onboarding import GeneralResponseSerializer


@extend_schema(tags=["Probing Question"])
@extend_schema_view(
    list=extend_schema(
        summary="List Probing Questions",
        description="Get paginated list of probing questions under the active tenant context.",
        responses={
            200: ProbingQuestionListSerializer(many=True),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    ),
    retrieve=extend_schema(
        summary="Get Probing Question",
        description="Get specific probing question details by ID.",
        responses={
            200: ProbingQuestionSerializer,
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Probing question not found.")
        }
    ),
    create=extend_schema(
        summary="Create Probing Question",
        description="Create a new probing question under the active tenant context.",
        request=ProbingQuestionSerializer,
        responses={
            201: ProbingQuestionSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid input values or validation error."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    ),
    update=extend_schema(
        summary="Update Probing Question",
        description="Full update of an existing probing question by ID.",
        request=ProbingQuestionSerializer,
        responses={
            200: ProbingQuestionSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid input values or validation error."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Probing question not found.")
        }
    ),
    partial_update=extend_schema(
        summary="Patch Probing Question",
        description="Partial update of an existing probing question by ID.",
        request=ProbingQuestionSerializer,
        responses={
            200: ProbingQuestionSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid input values or validation error."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Probing question not found.")
        }
    ),
    destroy=extend_schema(
        summary="Delete Probing Question",
        description="Soft-delete an existing probing question by ID.",
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Probing question soft-deleted successfully."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Probing question not found.")
        }
    ),
    dropdown=extend_schema(
        summary="Get Dropdown Options",
        description="Get lightweight label/value selection pairs representing active probing questions.",
        responses={
            200: ProbingQuestionDropdownSerializer(many=True)
        }
    ),
    bulk_delete=extend_schema(
        summary="Bulk Delete Probing Questions",
        description="Soft-delete multiple probing questions by their ID list.",
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Bulk deleted successfully."),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid ID list parameter."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    )
)
class ProbingQuestionViewSet(TenantBaseViewSet):
    """
    Probing Question Management API.
    Provides complete isolation and multi-tenant scoping for Probing Questions.
    """
    queryset = ProbingQuestion.objects.all()
    serializer_class = ProbingQuestionSerializer
    list_serializer_class = ProbingQuestionListSerializer
    dropdown_serializer_class = ProbingQuestionDropdownSerializer

    filterset_fields = ["is_active", "is_mandatory", "agent_template"]
    search_fields = ["question"]
    ordering_fields = ["priority", "score", "created_at"]
    ordering = ["agent_template", "priority", "-created_at"]

    select_related_fields = ["agent_template"]
    prefetch_related_fields = []

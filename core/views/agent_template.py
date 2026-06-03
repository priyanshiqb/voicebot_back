from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiResponse
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.agent_template import AgentTemplate
from core.serializers.agent_template import (
    AgentTemplateSerializer,
    AgentTemplateListSerializer,
    AgentTemplateDropdownSerializer,
)
from onboard.serializers.onboarding import GeneralResponseSerializer


@extend_schema(tags=["Agent Template"])
@extend_schema_view(
    list=extend_schema(
        summary="List Agent Templates",
        description="Get paginated list of agent templates under the active tenant context.",
        responses={
            200: AgentTemplateListSerializer(many=True),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    ),
    retrieve=extend_schema(
        summary="Get Agent Template",
        description="Get specific agent template details by ID.",
        responses={
            200: AgentTemplateSerializer,
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Agent template not found.")
        }
    ),
    create=extend_schema(
        summary="Create Agent Template",
        description="Create a new agent template under the active tenant context.",
        request=AgentTemplateSerializer,
        responses={
            201: AgentTemplateSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid input values or validation error."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    ),
    update=extend_schema(
        summary="Update Agent Template",
        description="Full update of an existing agent template by ID.",
        request=AgentTemplateSerializer,
        responses={
            200: AgentTemplateSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid input values or validation error."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Agent template not found.")
        }
    ),
    partial_update=extend_schema(
        summary="Patch Agent Template",
        description="Partial update of an existing agent template by ID.",
        request=AgentTemplateSerializer,
        responses={
            200: AgentTemplateSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid input values or validation error."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Agent template not found.")
        }
    ),
    destroy=extend_schema(
        summary="Delete Agent Template",
        description="Soft-delete an existing agent template by ID.",
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Agent template soft-deleted successfully."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Agent template not found.")
        }
    ),
    dropdown=extend_schema(
        summary="Get Dropdown Options",
        description="Get lightweight label/value selection pairs representing active agent templates.",
        responses={
            200: AgentTemplateDropdownSerializer(many=True)
        }
    ),
    bulk_delete=extend_schema(
        summary="Bulk Delete Agent Templates",
        description="Soft-delete multiple agent templates by their ID list.",
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Bulk deleted successfully."),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid ID list parameter."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    )
)
class AgentTemplateViewSet(TenantBaseViewSet):
    """
    Agent Template Management API.
    Provides complete row-level isolation and multi-tenant scoping for Agent Templates.
    """
    queryset = AgentTemplate.objects.all()
    serializer_class = AgentTemplateSerializer
    list_serializer_class = AgentTemplateListSerializer
    dropdown_serializer_class = AgentTemplateDropdownSerializer

    filterset_fields = ["is_active", "strict_facts", "can_user_edit_flow"]
    search_fields = ["name", "short_description"]
    ordering_fields = ["name", "created_at"]
    ordering = ["-created_at"]

    select_related_fields = []
    prefetch_related_fields = []

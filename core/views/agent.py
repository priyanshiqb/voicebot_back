from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiResponse
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.agent import Agent
from core.serializers.agent import (
    AgentSerializer,
    AgentListSerializer,
    AgentDropdownSerializer,
)
from onboard.serializers.onboarding import GeneralResponseSerializer


@extend_schema(tags=["Agent"])
@extend_schema_view(
    list=extend_schema(
        summary="List Agents",
        description="Get paginated list of agents under the active tenant context.",
        responses={
            200: AgentListSerializer(many=True),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    ),
    retrieve=extend_schema(
        summary="Get Agent",
        description="Get specific agent details by ID.",
        responses={
            200: AgentSerializer,
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Agent not found.")
        }
    ),
    create=extend_schema(
        summary="Create Agent",
        description="Create a new agent under the active tenant context.",
        request=AgentSerializer,
        responses={
            201: AgentSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid input values or validation error."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    ),
    update=extend_schema(
        summary="Update Agent",
        description="Full update of an existing agent by ID.",
        request=AgentSerializer,
        responses={
            200: AgentSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid input values or validation error."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Agent not found.")
        }
    ),
    partial_update=extend_schema(
        summary="Patch Agent",
        description="Partial update of an existing agent by ID.",
        request=AgentSerializer,
        responses={
            200: AgentSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid input values or validation error."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Agent not found.")
        }
    ),
    destroy=extend_schema(
        summary="Delete Agent",
        description="Soft-delete an existing agent by ID.",
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Agent soft-deleted successfully."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied."),
            404: OpenApiResponse(response=GeneralResponseSerializer, description="Agent not found.")
        }
    ),
    dropdown=extend_schema(
        summary="Get Dropdown Options",
        description="Get lightweight label/value selection pairs representing active agents.",
        responses={
            200: AgentDropdownSerializer(many=True)
        }
    ),
    bulk_delete=extend_schema(
        summary="Bulk Delete Agents",
        description="Soft-delete multiple agents by their ID list.",
        responses={
            200: OpenApiResponse(response=GeneralResponseSerializer, description="Bulk deleted successfully."),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid ID list parameter."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    )
)
class AgentViewSet(TenantBaseViewSet):
    """
    Agent Management API.
    Provides complete row-level isolation and multi-tenant scoping for Agents.
    """
    queryset = Agent.objects.all()
    serializer_class = AgentSerializer
    list_serializer_class = AgentListSerializer
    dropdown_serializer_class = AgentDropdownSerializer

    filterset_fields = ["is_active", "tone", "industry", "agent_template"]
    search_fields = ["bot_name", "persona_name"]
    ordering_fields = ["bot_name", "created_at"]
    ordering = ["-created_at"]

    select_related_fields = ["agent_template"]
    prefetch_related_fields = ["knowledge_bases"]

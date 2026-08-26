from onboard.constants.entitlement_resource import EntitlementResource
from tenant.common.views.entitlement_guard import EntitlementGuardMixin
from rest_framework.decorators import action
from rest_framework import filters
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiResponse
import django_filters
from django.db.models import Func, IntegerField
from django.db.models.functions import Coalesce

from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from tenant.common.views.agent_tenant_scope import (
    VBAgentTenantScopedMixin,
    is_vb_agent,
)
from core.models.agent import Agent
from core.serializers.agent import (
    AgentSerializer,
    AgentListSerializer,
    AgentDropdownSerializer,
    AgentBotNameCheckSerializer,
)
from core.serializers.knowledge_base import UniquenessCheckResponseSerializer
from onboard.serializers.onboarding import GeneralResponseSerializer
from utils.views.base_model_viewset import BaseViewSet
from core.views.mixins import ImportExportMixin
from core.resources import AgentResource

class JsonbArrayLength(Func):
    function = 'jsonb_array_length'
    output_field = IntegerField()

class AgentOrderingFilter(filters.OrderingFilter):
    def get_ordering(self, request, queryset, view):
        ordering = super().get_ordering(request, queryset, view)
        if ordering:
            new_ordering = []
            for field in ordering:
                if field == "agent_template":
                    new_ordering.append("agent_template__name")
                elif field == "-agent_template":
                    new_ordering.append("-agent_template__name")
                elif field in ("languages", "total_languages_count"):
                    new_ordering.append("total_languages_count")
                elif field in ("-languages", "-total_languages_count"):
                    new_ordering.append("-total_languages_count")
                else:
                    new_ordering.append(field)
            return new_ordering
        return ordering

class AgentFilter(django_filters.FilterSet):
    industry = django_filters.CharFilter(field_name="agent_template__industry")

    class Meta:
        model = Agent
        fields = ["is_active", "tone", "industry", "agent_template", "service_tier"]


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
class AgentViewSet(EntitlementGuardMixin, ImportExportMixin, VBAgentTenantScopedMixin, TenantBaseViewSet):
    """
    Agent Management API.
    Provides complete row-level isolation and multi-tenant scoping for Agents.
    """
    entitlement_resource = EntitlementResource.AGENT

    resource_class = AgentResource
    queryset = Agent.objects.annotate(
        total_languages_count=Coalesce(JsonbArrayLength('languages'), 0, output_field=IntegerField())
    )
    serializer_class = AgentSerializer
    list_serializer_class = AgentListSerializer
    dropdown_serializer_class = AgentDropdownSerializer

    filter_backends = [
        django_filters.rest_framework.DjangoFilterBackend,
        filters.SearchFilter,
        AgentOrderingFilter,
    ]
    filterset_class = AgentFilter
    search_fields = ["bot_name", "persona_name"]
    ordering_fields = [
        "bot_name",
        "created_at",
        "tone",
        "agent_template",
        "agent_template__name",
        "languages",
        "total_languages_count",
    ]
    ordering = ["-created_at"]

    select_related_fields = ["agent_template"]
    prefetch_related_fields = ["knowledge_bases", "probing_questions"]

    # Retrieve only. `list` and `dropdown` were previously open to machine callers and
    # are deliberately not here: a runner is handed the agent id it is acting for, so
    # enumerating every tenant's agents was reach it never needed.
    vb_agent_allowed_actions = frozenset({"retrieve"})

    def get_queryset(self):
        user = self.request.user

        # VB_AGENT reads are scoped to the tenant named in the mandatory `X-Tenant-ID`
        # header; an agent in any other tenant is a 404.
        if is_vb_agent(user):
            if self.action == "retrieve":
                # Bypass TenantBaseViewSet tenant scoping and call BaseViewSet directly
                # This applies is_deleted=False and select/prefetch fields globally
                return self.scope_to_agent_tenant(BaseViewSet.get_queryset(self))

        return super().get_queryset()

    @extend_schema(
        summary="Check Bot Name Uniqueness",
        description="Checks if a bot name is unique under the active tenant context.",
        request=AgentBotNameCheckSerializer,
        responses={
            200: OpenApiResponse(response=UniquenessCheckResponseSerializer, description="Uniqueness check result."),
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Validation failed."),
            401: OpenApiResponse(response=GeneralResponseSerializer, description="Unauthenticated user request."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied.")
        }
    )
    @action(detail=False, methods=["post"], url_path="check-bot-name", url_name="check-bot-name")
    def check_bot_name_uniqueness(self, request):
        serializer = AgentBotNameCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        bot_name = serializer.validated_data["bot_name"]
        exclude_id = serializer.validated_data.get("exclude_id")

        user = request.user
        if user.is_superuser:
            qs = Agent.objects.filter(bot_name__iexact=bot_name, is_deleted=False)
        else:
            try:
                active_tenant = user.tenant_mapping.tenant
            except Exception:
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied("Your user account is not associated with any active tenant.")
            qs = Agent.objects.filter(bot_name__iexact=bot_name, tenant=active_tenant, is_deleted=False)

        if exclude_id:
            qs = qs.exclude(id=exclude_id)

        is_unique = not qs.exists()
        return self.success(data={"is_unique": is_unique})

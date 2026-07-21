from rest_framework.decorators import action
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiResponse
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.agent_template import AgentTemplate
from core.serializers.agent_template import (
    AgentTemplateSerializer,
    AgentTemplateListSerializer,
    AgentTemplateDropdownSerializer,
)
from onboard.serializers.onboarding import GeneralResponseSerializer
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework.response import Response
from core.views.mixins import ImportExportMixin
from core.resources import AgentTemplateResource
from rest_framework import status
from django.db.models import Q
from core.models.agent import Agent


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
class AgentTemplateViewSet(ImportExportMixin, TenantBaseViewSet):
    """
    Agent Template Management API.
    Provides complete row-level isolation and multi-tenant scoping for Agent Templates.
    """
    resource_class = AgentTemplateResource
    queryset = AgentTemplate.objects.all()
    serializer_class = AgentTemplateSerializer
    list_serializer_class = AgentTemplateListSerializer
    dropdown_serializer_class = AgentTemplateDropdownSerializer

    filterset_fields = ["is_active", "strict_facts", "can_user_edit_flow", "is_default", "industry"]
    search_fields = ["name", "short_description"]
    ordering_fields = ["name", "created_at"]
    ordering = ["-created_at"]

    select_related_fields = []
    prefetch_related_fields = ["probing_questions"]

    def get_queryset(self):
        # Retrieve all agent templates bypassing TenantBaseViewSet's single tenant filter
        queryset = super(TenantBaseViewSet, self).get_queryset()
        user = self.request.user

        # Django Superusers view all records globally
        if user.is_superuser:
            return queryset

        try:
            active_tenant = user.tenant_mapping.tenant
            # Return tenant-specific templates OR global default templates
            return queryset.filter(Q(tenant=active_tenant) | Q(is_default=True))
        except Exception:
            raise PermissionDenied("Your user account is not associated with any active tenant.")

    def perform_destroy(self, instance):
        if instance.is_default:
            raise ValidationError({"detail": "This is a default agent template and cannot be deleted."})
            
        linked_agents = Agent.objects.filter(agent_template=instance, is_deleted=False).values_list('bot_name', flat=True)
        if linked_agents:
            agent_names = ", ".join(list(linked_agents)[:3])
            suffix = "..." if len(linked_agents) > 3 else ""
            raise ValidationError({"detail": f"This agent template is currently selected in the following agents: {agent_names}{suffix} and cannot be deleted."})
            
        super().perform_destroy(instance)

    @action(detail=False, methods=["post"])
    def bulk_delete(self, request):
        ids = request.data.get("ids", [])
        if not isinstance(ids, list) or not ids:
            return Response(
                {"status": False, "message": "ids must be a non-empty list", "errors": None},
                status=status.HTTP_400_BAD_REQUEST
            )
        queryset = self.get_queryset().filter(id__in=ids, is_deleted=False)
        if queryset.filter(is_default=True).exists():
            raise ValidationError({"detail": "One or more selected agent templates are default and cannot be deleted."})
            
        used_template_ids = Agent.objects.filter(
            agent_template_id__in=ids, 
            is_deleted=False
        ).values_list('agent_template_id', flat=True)
        if used_template_ids:
            raise ValidationError({"detail": "One or more selected agent templates are currently selected in an agent and cannot be deleted."})
            
        return super().bulk_delete(request)

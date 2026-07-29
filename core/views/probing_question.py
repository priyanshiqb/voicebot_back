from rest_framework.decorators import action
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiResponse
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.probing_question import ProbingQuestion
from core.serializers.probing_question import (
    ProbingQuestionSerializer,
    ProbingQuestionListSerializer,
    ProbingQuestionDropdownSerializer,
)
from onboard.serializers.onboarding import GeneralResponseSerializer
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework.response import Response
from core.views.mixins import ImportExportMixin
from core.resources import ProbingQuestionResource
from rest_framework import status
from django.db.models import Q
from core.models.agent import Agent
from core.models.agent_template import AgentTemplate



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
class ProbingQuestionViewSet(ImportExportMixin, TenantBaseViewSet):
    """
    Probing Question Management API.
    Provides complete isolation and multi-tenant scoping for Probing Questions.
    """
    resource_class = ProbingQuestionResource
    queryset = ProbingQuestion.objects.all()
    serializer_class = ProbingQuestionSerializer
    list_serializer_class = ProbingQuestionListSerializer
    dropdown_serializer_class = ProbingQuestionDropdownSerializer

    filterset_fields = ["is_active", "is_mandatory", "can_user_edit", "is_default"]
    search_fields = ["question"]
    ordering_fields = ["priority", "score", "created_at", "is_mandatory", "is_active", "question"]
    ordering = ["priority", "-created_at"]

    select_related_fields = []
    prefetch_related_fields = []

    def get_queryset(self):
        # Retrieve all probing questions bypassing TenantBaseViewSet's single tenant filter
        queryset = super(TenantBaseViewSet, self).get_queryset()
        user = self.request.user

        # Django Superusers view all records globally
        if user.is_superuser:
            return queryset

        try:
            active_tenant = user.tenant_mapping.tenant
            # Return tenant-specific questions OR global default questions
            return queryset.filter(Q(tenant=active_tenant) | Q(is_default=True))
        except Exception:
            raise PermissionDenied("Your user account is not associated with any active tenant.")

    def perform_destroy(self, instance):
        if instance.is_default:
            raise ValidationError({"detail": "This is a default probing question and cannot be deleted."})
            
        linked_agents = instance.agents.filter(is_deleted=False).values_list('bot_name', flat=True)
        if linked_agents:
            agent_names = ", ".join(list(linked_agents)[:3])
            suffix = "..." if len(linked_agents) > 3 else ""
            raise ValidationError({"detail": f"This probing question is currently selected in the following agents: {agent_names}{suffix} and cannot be deleted."})
            
        linked_templates = instance.agent_templates.filter(is_deleted=False).values_list('name', flat=True)
        if linked_templates:
            template_names = ", ".join(list(linked_templates)[:3])
            suffix = "..." if len(linked_templates) > 3 else ""
            raise ValidationError({"detail": f"This probing question is currently selected in the following agent templates: {template_names}{suffix} and cannot be deleted."})
            
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
            raise ValidationError({"detail": "One or more selected probing questions are default and cannot be deleted."})
            
        used_pq_ids = Agent.objects.filter(
            probing_questions__id__in=ids,
            is_deleted=False
        ).values_list('probing_questions__id', flat=True)
        if used_pq_ids:
            raise ValidationError({"detail": "One or more selected probing questions are currently selected in an agent and cannot be deleted."})
            
        used_template_pq_ids = AgentTemplate.objects.filter(
            probing_questions__id__in=ids,
            is_deleted=False
        ).values_list('probing_questions__id', flat=True)
        if used_template_pq_ids:
            raise ValidationError({"detail": "One or more selected probing questions are currently selected in an agent template and cannot be deleted."})
            
        return super().bulk_delete(request)

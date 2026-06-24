from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiResponse, OpenApiExample
from rest_framework.permissions import IsAuthenticated
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from user.permissions import IsVoicebotAgent
from core.models.agent_call_log import AgentCallLog
from core.serializers.agent_call_log import (
    AgentCallLogSerializer,
    AgentCallLogListSerializer,
    AgentCallLogDropdownSerializer,
)
from onboard.serializers.onboarding import GeneralResponseSerializer
import django_filters
from utils.views.base_model_viewset import BaseViewSet
from user.models.CustomUser import CustomUser

class AgentCallLogFilter(django_filters.FilterSet):
    class Meta:
        model = AgentCallLog
        fields = ["direction", "outcome", "agent", "campaign_id"]

CALL_LOG_EXAMPLE = {
    "provider_call_id": "call-001",
    "agent": "agent-uuid-string",
    "caller_number": "+91 93880 25735",
    "caller_name": "Nisha Patel",
    "direction": "inbound",
    "duration_seconds": 326,
    "outcome": "order_placed",
    "mood": "neutral",
    "mood_confidence": 0.85,
    "pii_redacted": False,
    "ai_summary": "Prospect interested in 2BHK property.",
    "preferred_language": "Spanish",
    "probing_questions_count": 3,
    "probing_questions": [
        {
            "question": "What is your monthly estimated call volume?",
            "answerSummary": "Over 50,000 calls per month.",
            "isPositive": True
        }
    ],
    "follow_up_detail": "Follow-up with alternative plans after contract review.",
    "human_transfer_detail": None,
    "transcript": [
        {
            "id": "tl-0",
            "speaker": "bot",
            "text": "Hello! Thank you for calling.",
            "timestamp": 0,
            "isRedacted": False
        }
    ],
    "recording_url": "/recordings/call-1.mp3",
    "started_at": "2026-06-23T17:33:31.671Z",
    "ended_at": "2026-06-23T17:38:57.671Z",
    "tags": ["urgent"]
}

@extend_schema(tags=["Agent Call Log"])
@extend_schema_view(
    list=extend_schema(
        summary="List Call Logs",
        description="Get paginated list of agent call logs under the active tenant context.",
        responses={200: AgentCallLogListSerializer(many=True)}
    ),
    retrieve=extend_schema(
        summary="Get Call Log",
        description="Get specific call log details by ID.",
        responses={200: AgentCallLogSerializer}
    ),
    create=extend_schema(
        summary="Create Call Log",
        description="Create a new call log. STRICTLY RESTRICTED to Voicebot Agent tokens.",
        request=AgentCallLogSerializer,
        examples=[
            OpenApiExample(
                name="Standard Call Telemetry",
                description="A complete call log telemetry payload",
                value=CALL_LOG_EXAMPLE,
                request_only=True
            )
        ],
        responses={
            201: AgentCallLogSerializer,
            400: OpenApiResponse(response=GeneralResponseSerializer, description="Invalid input values or validation error."),
            403: OpenApiResponse(response=GeneralResponseSerializer, description="Permission denied. Only VB_AGENT can access this.")
        }
    ),
    dropdown=extend_schema(
        summary="Get Dropdown Options",
        description="Get lightweight label/value selection pairs representing call logs.",
        responses={200: AgentCallLogDropdownSerializer(many=True)}
    )
)
class AgentCallLogViewSet(TenantBaseViewSet):
    """
    Agent Call Log API.
    Provides read-only access to standard users and write access to Voicebot Agents.
    """
    http_method_names = ["get", "post", "options"]
    
    queryset = AgentCallLog.objects.all()
    serializer_class = AgentCallLogSerializer
    list_serializer_class = AgentCallLogListSerializer
    dropdown_serializer_class = AgentCallLogDropdownSerializer

    filterset_class = AgentCallLogFilter
    search_fields = ["provider_call_id", "caller_number", "caller_name"]
    ordering_fields = ["started_at", "duration_seconds"]
    ordering = ["-started_at"]

    select_related_fields = ["agent"]

    def get_permissions(self):
        """
        Instantiates and returns the list of permissions that this view requires.
        Only VB_AGENT can 'create'. Standard Authenticated users can 'list'/'retrieve'.
        """
        if self.action == "create":
            return [IsAuthenticated(), IsVoicebotAgent()]
        return [IsAuthenticated()]

    def get_queryset(self):
        user = self.request.user
        
        # VB_AGENT users can globally read all call logs across tenants
        if getattr(user, "user_type", None) == CustomUser.UserType.VB_AGENT:
            if self.action in ["list", "retrieve", "dropdown"]:
                # Bypass TenantBaseViewSet tenant scoping and call BaseViewSet directly
                return BaseViewSet.get_queryset(self)
                
        return super().get_queryset()

    def perform_create(self, serializer):
        user = self.request.user
        
        # VB_AGENT bypasses the standard user tenant mapping constraint
        if getattr(user, "user_type", None) == CustomUser.UserType.VB_AGENT:
            # Derive the tenant from the associated Voicebot Agent instead
            agent = serializer.validated_data.get('agent')
            if not agent:
                from rest_framework.exceptions import ValidationError
                raise ValidationError({"agent": "Agent must be specified."})
            serializer.save(tenant=agent.tenant)
            return

        super().perform_create(serializer)

    @extend_schema(exclude=True)
    def bulk_delete(self, request, *args, **kwargs):
        from rest_framework.exceptions import MethodNotAllowed
        raise MethodNotAllowed("POST")

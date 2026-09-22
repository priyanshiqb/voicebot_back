from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiResponse
from rest_framework import status
from rest_framework.decorators import action
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.custom_voice import CustomVoice
from core.serializers.custom_voice import (
    CustomVoiceNameCheckSerializer,
    CustomVoiceSerializer,
)
from core.serializers.knowledge_base import UniquenessCheckResponseSerializer
from onboard.serializers.onboarding import GeneralResponseSerializer


@extend_schema(tags=["Custom Voice"])
@extend_schema_view(
    list=extend_schema(
        summary="List Custom Voice Records",
        description="Get paginated list of custom voice records.",
    ),
    retrieve=extend_schema(
        summary="Get Custom Voice Record",
        description="Get specific custom voice record details by ID.",
    ),
    create=extend_schema(
        summary="Create Custom Voice Record",
        description="Create a new custom voice record.",
    ),
    destroy=extend_schema(
        summary="Delete Custom Voice Record",
        description="Soft-delete an existing custom voice record by ID.",
    ),
)
class CustomVoiceViewSet(TenantBaseViewSet):
    """
    Custom Voice Management API.
    Provides standard REST actions. The payload structure is dynamic and stored as JSON.
    """
    queryset = CustomVoice.objects.all()
    serializer_class = CustomVoiceSerializer

    # We do not provide specific field filters for the JSON block 
    # to maintain strict backend simplicity, but model filters apply.
    filterset_fields = ["is_active"]
    search_fields = []
    ordering_fields = ["created_at"]
    ordering = ["-created_at"]

    # Limit to List, Retrieve, Create, Destroy
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    @extend_schema(
        summary="Check Custom Voice Name Availability",
        description=(
            "Whether a voice name is free in your organisation. Names are unique per "
            "tenant, ignoring case and surrounding spaces; deleted voices release "
            "theirs. The same rule create enforces."
        ),
        request=CustomVoiceNameCheckSerializer,
        responses={200: UniquenessCheckResponseSerializer},
    )
    @action(
        detail=False, methods=["post"], url_path="check-name", url_name="check-name"
    )
    def check_name(self, request, *args, **kwargs):
        serializer = CustomVoiceNameCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Checked here because this action never reaches get_queryset, where the tenant
        # guard usually runs. A superuser is answered across every tenant.
        tenant = None
        if not request.user.is_superuser:
            try:
                tenant = request.user.tenant_mapping.tenant
            except Exception:
                return self.error(
                    message=(
                        "Your user account is not associated with any active tenant."
                    ),
                    status_code=status.HTTP_403_FORBIDDEN,
                )

        taken = CustomVoice.name_in_use(
            serializer.validated_data["name"],
            tenant=tenant,
            exclude_pk=serializer.validated_data.get("exclude_id"),
        )
        return self.success(data={"is_unique": not taken})

    def perform_destroy(self, instance):
        from core.models.agent import Agent
        from rest_framework.exceptions import ValidationError

        agents_using_voice = Agent.objects.filter(
            voice_name=str(instance.id),
            is_deleted=False
        ).count()
        if agents_using_voice > 0:
            raise ValidationError(
                f"This custom voice is used by {agents_using_voice} bot(s). "
                "Please assign a different voice to those bots before deleting."
            )
        super().perform_destroy(instance)

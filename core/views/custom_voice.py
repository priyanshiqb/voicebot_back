from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiResponse
from rest_framework import status
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.custom_voice import CustomVoice
from core.serializers.custom_voice import CustomVoiceSerializer
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

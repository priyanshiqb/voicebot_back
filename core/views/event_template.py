from tenant.common.views.agent_tenant_scope import VBAgentTenantScopedMixin
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.event_template import EventTemplate
from core.serializers.event_template import (
    EventTemplateDropdownSerializer,
    EventTemplateListSerializer,
    EventTemplateSerializer,
)

from drf_spectacular.utils import extend_schema

@extend_schema(tags=["Event Template"])
class EventTemplateViewSet(VBAgentTenantScopedMixin, TenantBaseViewSet):
    """CRUD for bookable event types, tenant-scoped.

    Machine (`VB_AGENT`) callers get read access only — a voicebot needs to find the
    event type it is booking, but creating and deleting bookable types is human
    configuration work.
    """

    queryset = EventTemplate.objects.all()
    serializer_class = EventTemplateSerializer
    list_serializer_class = EventTemplateListSerializer
    dropdown_serializer_class = EventTemplateDropdownSerializer

    filterset_fields = ["is_active", "assignment_strategy", "location_type"]
    search_fields = ["name", "description"]
    ordering_fields = ["name", "created_at", "updated_at", "duration_minutes"]
    ordering = ["-created_at"]

    vb_agent_allowed_actions = frozenset({"list", "retrieve", "dropdown"})

    def get_queryset(self):
        scoped = self.agent_scoped_queryset()
        if scoped is not None:
            return scoped
        return super().get_queryset()

    # No perform_destroy override: EventTemplate.soft_delete() refuses while active
    # bookings reference the type, so bulk_delete — which calls soft_delete directly
    # and never reaches this viewset method — is covered by the same guard.

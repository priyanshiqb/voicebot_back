from tenant.common.views.agent_tenant_scope import VBAgentTenantScopedMixin
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.user_event_skill import UserEventSkill
from core.serializers.user_event_skill import (
    UserEventSkillDropdownSerializer,
    UserEventSkillListSerializer,
    UserEventSkillSerializer,
)

from drf_spectacular.utils import extend_schema

@extend_schema(tags=["Event Skill"])
class UserEventSkillViewSet(VBAgentTenantScopedMixin, TenantBaseViewSet):
    """CRUD for host-to-event-type eligibility, tenant-scoped.

    Machine (`VB_AGENT`) callers get read access only: a voicebot needs to know which
    host can take an event type in order to offer a slot, but who is eligible is human
    configuration.
    """

    queryset = UserEventSkill.objects.all()
    serializer_class = UserEventSkillSerializer
    list_serializer_class = UserEventSkillListSerializer
    dropdown_serializer_class = UserEventSkillDropdownSerializer

    filterset_fields = ["user", "event_template", "is_active"]
    search_fields = [
        "user__email",
        "user__first_name",
        "user__last_name",
        "event_template__name",
    ]
    ordering_fields = [
        "created_at",
        "updated_at",
        "user__email",
        "user__first_name",
        "user__last_name",
        "event_template__name",
    ]
    ordering = ["-created_at"]
    select_related_fields = ["user", "event_template"]

    vb_agent_allowed_actions = frozenset({"list", "retrieve", "dropdown"})

    def get_queryset(self):
        scoped = self.agent_scoped_queryset()
        if scoped is not None:
            return scoped
        return super().get_queryset()

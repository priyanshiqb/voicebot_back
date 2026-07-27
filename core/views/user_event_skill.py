from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.user_event_skill import UserEventSkill
from core.serializers.user_event_skill import (
    UserEventSkillDropdownSerializer,
    UserEventSkillListSerializer,
    UserEventSkillSerializer,
)


class UserEventSkillViewSet(TenantBaseViewSet):
    """CRUD for host-to-event-type eligibility, tenant-scoped."""

    queryset = UserEventSkill.objects.all()
    serializer_class = UserEventSkillSerializer
    list_serializer_class = UserEventSkillListSerializer
    dropdown_serializer_class = UserEventSkillDropdownSerializer

    filterset_fields = ["user", "event_template", "is_active"]
    ordering_fields = ["created_at", "updated_at"]
    ordering = ["-created_at"]
    select_related_fields = ["user", "event_template"]

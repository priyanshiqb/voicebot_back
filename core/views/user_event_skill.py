from django.db.models import CharField, Value
from django.db.models.functions import Coalesce, Concat, Lower, NullIf, Trim

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
        # The three columns the Event Skill table actually renders.
        "user_name",
        "template_name",
        "is_active",
    ]
    # The listing shows a user object and a template object, not the columns they are
    # stored in, so the names the client sorts by are not queryset paths. `user_name`
    # resolves to the annotation below; `template_name` is just the related column
    # under the name the table gives it.
    ordering_field_map = {
        "user_name": "_user_name",
        "template_name": "event_template__name",
    }
    # `is_active` is a boolean: sorting on it puts every row in one of two ties.
    ordering_tiebreaker = ("-created_at", "id")
    ordering = ["-created_at"]
    select_related_fields = ["user", "event_template"]

    # What the row displays for the host: their full name, falling back to the email
    # when neither name is set — which is what the UI shows in that case too, so the
    # sort matches the column instead of grouping every unnamed user under "".
    USER_NAME_ORDER_EXPR = Lower(
        Coalesce(
            NullIf(
                Trim(Concat("user__first_name", Value(" "), "user__last_name")),
                Value(""),
            ),
            "user__email",
            # Explicit because the two arms are a CharField and an EmailField, and
            # Coalesce refuses to guess between them.
            output_field=CharField(),
        )
    )

    vb_agent_allowed_actions = frozenset({"list", "retrieve", "dropdown"})

    def get_queryset(self):
        scoped = self.agent_scoped_queryset()
        queryset = super().get_queryset() if scoped is None else scoped
        if self.action == "list":
            queryset = queryset.annotate(_user_name=self.USER_NAME_ORDER_EXPR)
        return queryset

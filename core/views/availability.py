from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ViewSet

from tenant.common.views.agent_tenant_scope import (
    VBAgentActionPermission,
    require_agent_tenant,
)
from utils.views.base_model_viewset import ResponseMixin
from core.serializers.availability import AvailableSlotsQuerySerializer, SlotSerializer
from core.services import availability as availability_service


from drf_spectacular.utils import extend_schema

@extend_schema(tags=["Availability"])
class AvailabilityViewSet(ResponseMixin, ViewSet):
    """Read-only availability endpoint. GET available-slots computes bookable
    slots for an event type over a date range, tenant-scoped.

    Machine (`VB_AGENT`) callers may use this — checking slots is half of booking —
    but must name their tenant with `X-Tenant-ID`.
    """

    permission_classes = (IsAuthenticated, VBAgentActionPermission)
    vb_agent_allowed_actions = frozenset({"available_slots"})

    def _resolve_tenant(self, request, event_template):
        if request.user.is_superuser:
            return event_template.tenant

        # Machine callers name their tenant in a header. Note the ownership check below
        # still applies to them: the header says which tenant they claim to act for, and
        # the template must actually be in it. Deriving the tenant from the template
        # instead would let the caller pick its own scope.
        tenant = require_agent_tenant(request)

        if tenant is None:
            try:
                tenant = request.user.tenant_mapping.tenant
            except Exception:
                raise PermissionDenied(
                    "Your user account is not associated with any active tenant."
                )

        if event_template.tenant_id != tenant.id:
            raise PermissionDenied("Event template does not belong to your tenant.")
        return tenant

    @extend_schema(
        parameters=[AvailableSlotsQuerySerializer],
        responses={200: SlotSerializer(many=True)},
        summary="Get Available Slots",
        description=(
            "Behaviour by strategy:\n"
            "DIRECT: target_host is required; a slot is returned when that host is free.\n"
            "COLLECTIVE: omit target_host; a slot is returned only if every skilled host is free at that time."
        )
    )
    @action(detail=False, methods=["get"], url_path="available-slots")
    def available_slots(self, request):
        query = AvailableSlotsQuerySerializer(
            data=request.query_params, context={"request": request}
        )
        query.is_valid(raise_exception=True)
        data = query.validated_data

        event_template = data["event_template"]
        tenant = self._resolve_tenant(request, event_template)
        target = data.get("target_host")

        slots = availability_service.get_available_slots(
            tenant=tenant,
            event_template=event_template,
            date_from=data["date_from"],
            date_to=data["date_to"],
            target_host_id=target.id if target else None,
        )
        payload = SlotSerializer(
            [
                {
                    "start": s.start,
                    "end": s.end,
                    "eligible_host_ids": list(s.eligible_host_ids),
                    "external_check_failed": s.external_check_failed,
                }
                for s in slots
            ],
            many=True,
        )
        return self.success(
            data=payload.data, message="Available slots retrieved successfully"
        )

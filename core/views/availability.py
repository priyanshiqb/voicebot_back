from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ViewSet

from tenant.common.views.agent_tenant_scope import (
    VBAgentActionPermission,
    require_agent_tenant,
)
from utils.views.base_model_viewset import ResponseMixin
from core.serializers.availability import (
    AvailableSlotsQuerySerializer,
    AvailableSlotsResponseSerializer,
)
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
        responses={200: AvailableSlotsResponseSerializer},
        summary="Get Available Slots",
        description=(
            "Bounds are UTC instants, not dates: `start_utc` is inclusive and "
            "`end_utc` exclusive, so consecutive windows tile. Resolve the user's "
            "chosen day in the timezone they are viewing before calling.\n\n"
            "Every slot is returned twice — `start`/`end` in UTC (book with these) "
            "and `start_local`/`end_local` rendered in the response's `timezone`. "
            "Pass `timezone` to choose that zone; it defaults to the target host's.\n\n"
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
        target_host_id = target.id if target else None

        slots = availability_service.get_available_slots(
            tenant=tenant,
            event_template=event_template,
            range_start=data["start_utc"],
            range_end=data["end_utc"],
            target_host_id=target_host_id,
        )
        # Resolved after the engine ran, and from its own inputs: the display zone
        # is presentation only and must not be able to change which slots exist.
        zone, zone_name = availability_service.display_zone(
            tenant, target_host_id=target_host_id, requested=data.get("timezone")
        )
        payload = AvailableSlotsResponseSerializer(
            {
                "timezone": zone_name,
                "slots": [
                    {
                        "start": s.start,
                        "end": s.end,
                        "start_local": s.start.astimezone(zone).isoformat(),
                        "end_local": s.end.astimezone(zone).isoformat(),
                        "eligible_host_ids": list(s.eligible_host_ids),
                        "external_check_failed": s.external_check_failed,
                    }
                    for s in slots
                ],
            }
        )
        return self.success(
            data=payload.data, message="Available slots retrieved successfully"
        )

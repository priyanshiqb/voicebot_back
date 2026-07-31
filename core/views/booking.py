from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied

from tenant.common.views.agent_tenant_scope import VBAgentTenantScopedMixin
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.booking import Booking
from core.models.event_template import EventTemplate
from core.serializers.booking import (
    BookingCancelSerializer,
    BookingCreateSerializer,
    BookingListSerializer,
    BookingSerializer,
)
from core.services import bookings as booking_service
from core.services.exceptions import (
    InvalidTransitionError,
    NoEligibleHostError,
    PolicyViolationError,
    SlotConflictError,
)

from drf_spectacular.utils import extend_schema

@extend_schema(tags=["Booking"])
class BookingViewSet(VBAgentTenantScopedMixin, TenantBaseViewSet):
    """
    Booking management. Bookings are created/mutated only through the booking
    service (never direct serializer.save), so PUT/PATCH are disabled and DELETE
    maps to cancellation. Domain errors are mapped to precise HTTP codes.

    Machine (`VB_AGENT`) callers have full access here — booking, holding, confirming
    and cancelling is the voicebot's job — but must name their tenant with
    `X-Tenant-ID`. They have no tenant mapping, and a booking has to land somewhere.
    """

    queryset = Booking.objects.all()
    serializer_class = BookingSerializer
    list_serializer_class = BookingListSerializer
    http_method_names = ["get", "post", "delete", "head", "options"]

    filterset_fields = ["status", "primary_host", "event_template", "source"]
    ordering_fields = ["start_time", "created_at"]
    ordering = ["-start_time"]
    select_related_fields = ["event_template", "primary_host"]
    prefetch_related_fields = ["participants"]

    # Every action this viewset exposes. `destroy` is included: DELETE here is a
    # cancellation, not a data deletion — see perform_destroy.
    vb_agent_allowed_actions = frozenset({
        "list",
        "retrieve",
        "create",
        "hold",
        "confirm",
        "cancel",
        "complete",
        "no_show",
        "destroy",
    })

    def get_queryset(self):
        scoped = self.agent_scoped_queryset()
        if scoped is not None:
            return scoped
        return super().get_queryset()

    def _tenant(self, request):
        # Machine callers resolve their tenant from the header; everyone else from
        # their mapping. Both paths must agree with get_queryset above, or a create
        # would land in a tenant the caller cannot then read back.
        agent_tenant = self.get_agent_tenant()
        if agent_tenant is not None:
            return agent_tenant

        try:
            return request.user.tenant_mapping.tenant
        except Exception:
            raise PermissionDenied(
                "Your user account is not associated with any active tenant."
            )

    def _run(self, fn):
        """Execute a service call, mapping domain exceptions to HTTP responses.
        Returns (result, error_response); exactly one is non-None."""
        try:
            return fn(), None
        except PolicyViolationError as exc:
            return None, self.error(
                message=str(exc),
                errors={(exc.field or "detail"): [str(exc)]},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        except (NoEligibleHostError, InvalidTransitionError) as exc:
            return None, self.error(
                message=str(exc), status_code=status.HTTP_400_BAD_REQUEST
            )
        except SlotConflictError as exc:
            return None, self.error(
                message=str(exc), status_code=status.HTTP_409_CONFLICT
            )

    def _serialized(self, booking):
        return BookingSerializer(booking, context=self.get_serializer_context()).data

    def create(self, request, *args, **kwargs):
        return self._create(request, status_field=Booking.Status.SCHEDULED, hold=False)

    @action(detail=False, methods=["post"])
    def hold(self, request):
        return self._create(request, status_field=Booking.Status.HELD, hold=True)

    def _create(self, request, status_field, hold):
        serializer = BookingCreateSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        tenant = self._tenant(request)
        template = data["event_template"]
        target = data.get("target_host")
        target_host_id = target.id if target else None

        # COLLECTIVE: one booking per host, returned as a list.
        if (
            not hold
            and template.assignment_strategy
            == EventTemplate.AssignmentStrategy.COLLECTIVE
        ):
            bookings_list, err = self._run(
                lambda: booking_service.create_collective_booking(
                    tenant=tenant,
                    event_template=template,
                    start_time=data["start_time"],
                    participants=data["participants"],
                    actor=request.user,
                    source=Booking.Source.INTERNAL,
                )
            )
            if err:
                return err
            payload = BookingSerializer(
                bookings_list, many=True, context=self.get_serializer_context()
            )
            return self.success(
                data=payload.data,
                message="Collective booking created",
                status_code=status.HTTP_201_CREATED,
            )

        if hold:
            booking, err = self._run(
                lambda: booking_service.hold_slot(
                    tenant=tenant,
                    event_template=template,
                    start_time=data["start_time"],
                    participants=data["participants"],
                    actor=request.user,
                    target_host_id=target_host_id,
                )
            )
            message = "Slot held successfully"
        else:
            booking, err = self._run(
                lambda: booking_service.create_booking(
                    tenant=tenant,
                    event_template=template,
                    start_time=data["start_time"],
                    participants=data["participants"],
                    actor=request.user,
                    target_host_id=target_host_id,
                    source=Booking.Source.INTERNAL,
                )
            )
            message = "Booked successfully"

        if err:
            return err
        return self.success(
            data=self._serialized(booking),
            message=message,
            status_code=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        booking = self.get_object()
        result, err = self._run(
            lambda: booking_service.confirm_hold(booking=booking, actor=request.user)
        )
        if err:
            return err
        return self.success(data=self._serialized(result), message="Booking confirmed")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        booking = self.get_object()
        payload = BookingCancelSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        reason = payload.validated_data.get("reason", "")
        if booking.group_id:
            _, err = self._run(
                lambda: booking_service.cancel_group(
                    group_id=booking.group_id, actor=request.user, reason=reason
                )
            )
            if err:
                return err
            booking.refresh_from_db()
            return self.success(
                data=self._serialized(booking), message="Collective booking canceled"
            )
        result, err = self._run(
            lambda: booking_service.cancel_booking(
                booking=booking, actor=request.user, reason=reason
            )
        )
        if err:
            return err
        return self.success(data=self._serialized(result), message="Booking canceled")

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        booking = self.get_object()
        result, err = self._run(
            lambda: booking_service.complete_booking(booking=booking, actor=request.user)
        )
        if err:
            return err
        return self.success(data=self._serialized(result), message="Booking completed")

    @action(detail=True, methods=["post"], url_path="no-show")
    def no_show(self, request, pk=None):
        booking = self.get_object()
        result, err = self._run(
            lambda: booking_service.mark_no_show(booking=booking, actor=request.user)
        )
        if err:
            return err
        return self.success(
            data=self._serialized(result), message="Booking marked as no-show"
        )

    def perform_destroy(self, instance):
        # DELETE == cancel (with notification); internal ops bypass lead-time.
        if instance.group_id:
            booking_service.cancel_group(
                group_id=instance.group_id, actor=self.request.user, enforce_lead=False
            )
        else:
            booking_service.cancel_booking(
                booking=instance, actor=self.request.user, enforce_lead=False
            )

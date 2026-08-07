from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import django_filters
from django.db.models import Count
from django.db.models.functions import TruncDate
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError

from tenant.common.views.agent_tenant_scope import VBAgentTenantScopedMixin, is_vb_agent
from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.booking import Booking
from core.models.event_template import EventTemplate
from core.serializers.booking import (
    BookingCancelSerializer,
    BookingCreateSerializer,
    BookingDailyCountQuerySerializer,
    BookingDailyCountResponseSerializer,
    BookingListSerializer,
    BookingSerializer,
)
from core.services import bookings as booking_service
from core.services.exceptions import (
    ExternalCalendarUnavailable,
    InvalidTransitionError,
    NoEligibleHostError,
    PolicyViolationError,
    SlotConflictError,
)

from drf_spectacular.utils import extend_schema

# Alias for "the calendar date this booking's start_time falls on, locally".
# Shared, because both the list filter and daily-count need it and re-annotating
# the same name is a ValueError.
LOCAL_DATE_ALIAS = "local_date"


def resolve_booking_zone(request, requested=None):
    """The zone booking dates are resolved in, as ``(ZoneInfo, iana_name)``.

    Unlike the display zone on available-slots, this is **not** presentational.
    `start_time` is a UTC instant, and which calendar date it belongs to depends
    entirely on where you stand: a 21:00 Asia/Kolkata appointment is 15:30 UTC the
    same day, but a 02:00 one is the *previous* UTC day. Resolving dates in UTC
    would quietly move a slice of every tenant's evening bookings to the day
    before, and nothing about the response would look wrong.

    Order, most specific first: the caller's explicit `timezone`, then the user's
    own preference, then the tenant's base zone, then UTC.
    """
    if requested:
        try:
            return ZoneInfo(requested), requested
        except Exception as exc:  # noqa: BLE001 — any unusable key is the same 400
            # 400 rather than falling back to UTC. A silent fallback would return
            # a well-formed set of bookings for the wrong days.
            raise ValidationError(
                {"timezone": f"{requested!r} is not a valid IANA timezone identifier."}
            ) from exc

    user_tz = getattr(getattr(request, "user", None), "timezone", None)
    if user_tz is not None:
        return user_tz.zone, user_tz.timezone

    try:
        tenant_tz = request.user.tenant_mapping.tenant.timezone
    except Exception:  # noqa: BLE001 — superusers have no mapping; UTC is fine
        tenant_tz = None
    if tenant_tz is not None:
        return tenant_tz.zone, tenant_tz.timezone

    return ZoneInfo("UTC"), "UTC"


def filter_local_date_range(queryset, zone, start_date=None, end_date=None):
    """Narrow to bookings whose LOCAL start date falls in ``[start_date, end_date]``.

    Both bounds inclusive, either may be None.

    Shared by the list filter and daily-count so that a day's count and that day's
    list can never disagree. Converting the local dates to UTC instants instead
    would be simpler and index-friendly, but the two would drift on the handful of
    zones whose DST transition happens at midnight (America/Santiago,
    America/Havana): there local midnight does not exist twice a year, and the
    boundary lands an hour off. Deriving both from the same TruncDate makes them
    equal by construction rather than by agreement.
    """
    if start_date is None and end_date is None:
        return queryset

    # Index-friendly pre-filter on the raw column so this can use
    # core_booking_status_start_idx instead of scanning the tenant's history.
    # Deliberately sloppy — no zone is more than 14 hours off UTC, and the
    # LOCAL_DATE_ALIAS comparison below is what actually decides membership, so
    # the margin only has to be generous.
    if start_date is not None:
        queryset = queryset.filter(
            start_time__gte=datetime.combine(start_date, time.min, tzinfo=zone)
            - timedelta(days=1)
        )
    if end_date is not None:
        queryset = queryset.filter(
            start_time__lt=datetime.combine(end_date, time.min, tzinfo=zone)
            + timedelta(days=2)
        )

    if LOCAL_DATE_ALIAS not in queryset.query.annotations:
        queryset = queryset.annotate(
            **{LOCAL_DATE_ALIAS: TruncDate("start_time", tzinfo=zone)}
        )
    if start_date is not None:
        queryset = queryset.filter(**{f"{LOCAL_DATE_ALIAS}__gte": start_date})
    if end_date is not None:
        queryset = queryset.filter(**{f"{LOCAL_DATE_ALIAS}__lte": end_date})
    return queryset


class BookingFilter(django_filters.FilterSet):
    """List filters for bookings.

    The date filters work on **local calendar dates** — the day a human would say
    the appointment is on — while `start_time_after`/`start_time_before` take UTC
    instants for sub-day precision. Both are offered because "show me Tuesday" and
    "show me 14:00–17:00" are different questions and answering the first with
    instants makes every client reimplement timezone maths.
    """

    date = django_filters.DateFilter(
        method="filter_on_date",
        label="Bookings on this local date (YYYY-MM-DD).",
    )
    start_date = django_filters.DateFilter(
        method="filter_from_date",
        label="Local date to start from, inclusive (YYYY-MM-DD).",
    )
    end_date = django_filters.DateFilter(
        method="filter_to_date",
        label="Local date to end at, INCLUSIVE (YYYY-MM-DD).",
    )
    start_time_after = django_filters.IsoDateTimeFilter(
        field_name="start_time",
        lookup_expr="gte",
        label="Starts at or after this UTC instant (ISO-8601).",
    )
    start_time_before = django_filters.IsoDateTimeFilter(
        field_name="start_time",
        lookup_expr="lt",
        label="Starts strictly before this UTC instant (ISO-8601).",
    )
    timezone = django_filters.CharFilter(
        method="filter_noop",
        label=(
            "IANA zone the date filters are read in, e.g. Asia/Kolkata. Defaults "
            "to the caller's timezone, then the tenant's, then UTC."
        ),
    )

    class Meta:
        model = Booking
        fields = ["status", "primary_host", "event_template", "source"]

    @property
    def local_zone(self):
        zone, _ = resolve_booking_zone(self.request, self.data.get("timezone"))
        return zone

    def filter_noop(self, queryset, name, value):
        """`timezone` is an input to the other filters, not a filter itself.

        Declared anyway so it appears in the schema — an undeclared param is
        accepted silently, which is indistinguishable from being ignored.
        """
        return queryset

    def filter_on_date(self, queryset, name, value):
        return filter_local_date_range(queryset, self.local_zone, value, value)

    def filter_from_date(self, queryset, name, value):
        return filter_local_date_range(queryset, self.local_zone, start_date=value)

    def filter_to_date(self, queryset, name, value):
        return filter_local_date_range(queryset, self.local_zone, end_date=value)


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

    filterset_class = BookingFilter
    ordering_fields = [
        "start_time", "end_time", "created_at", "updated_at", "status", 
        "source", "title", "event_template", "event_template__name", 
        "primary_host", "primary_host__first_name", "primary_host__last_name", 
        "primary_host__email", "external_provider", "cancellation_reason"
    ]
    ordering = ["-start_time"]
    select_related_fields = ["event_template", "primary_host"]
    prefetch_related_fields = ["participants"]

    # Every booking action a machine caller needs. `destroy` is included: DELETE
    # here is a cancellation, not a data deletion — see perform_destroy.
    # `daily_count` is deliberately absent: it is dashboard reporting, and the
    # voicebot books appointments rather than reads statistics about them.
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

    def _source(self, request, default):
        """Provenance for a booking, derived from the authenticated caller.

        Deliberately NOT read from the request body. `source` answers "how did
        this booking come to exist", and a value the caller asserts about itself
        is not provenance — any human JWT could post `source: "AI"` and quietly
        inflate "the agent booked X% of appointments".

        The caller is already identified: a `VB_AGENT` token is the voicebot, so
        anything it books through REST is `AI`. Everyone else keeps the default
        for the endpoint they called, which differs — a create is `INTERNAL`, a
        hold is `CUSTOMER`.

        Bookings made by the in-process AI bridge (`core.services.agent_scheduling`,
        used during a live call) already tag themselves `AI`. This closes the same
        gap for the REST surface, which the agent also reaches.
        """
        return Booking.Source.AI if is_vb_agent(request.user) else default

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
        except ExternalCalendarUnavailable as exc:
            # 503, not 409: the slot may well be free — the host's calendar simply
            # could not be read, so booking would be unverified. Retryable.
            return None, self.error(
                message=str(exc), status_code=status.HTTP_503_SERVICE_UNAVAILABLE
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
                    source=self._source(request, Booking.Source.INTERNAL),
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
                    source=self._source(request, Booking.Source.CUSTOMER),
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
                    source=self._source(request, Booking.Source.INTERNAL),
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

    @extend_schema(
        parameters=[BookingDailyCountQuerySerializer],
        responses={200: BookingDailyCountResponseSerializer},
        summary="Date-wise booking counts",
        description=(
            "How many bookings fall on each date in a range — one number per day, "
            "for a calendar heat map or a volume chart.\n\n"
            "Counts by **`start_time`** (when the appointment happens), not when it "
            "was created. Both bounds are **inclusive** dates and the range may span "
            "at most **61 days**.\n\n"
            "Every date in the range is returned, including empty ones as `count: 0`.\n\n"
            "Scoped exactly like `GET /bookings/` — same tenant, and the same "
            "`status`, `primary_host`, `event_template` and `source` filters apply. "
            "All statuses count unless you narrow them: pass `?status=CONFIRMED` for "
            "confirmed only, or `?primary_host=<user id>` for one host's calendar."
        ),
    )
    @action(detail=False, methods=["get"], url_path="daily-count")
    def daily_count(self, request):
        query = BookingDailyCountQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        start_date, end_date = data["start_date"], data["end_date"]
        zone, zone_name = resolve_booking_zone(request, data.get("timezone"))

        # BookingFilter reads start_date/end_date too, so the range is usually
        # already applied by the time this runs. Applying it again is a no-op —
        # same helper, same zone, and the annotation is only added once — and it
        # keeps this action correct on its own terms rather than depending on the
        # filter backend having recognised the same parameters.
        queryset = filter_local_date_range(
            self.filter_queryset(self.get_queryset()), zone, start_date, end_date
        )

        rows = (
            queryset
            # Clears Meta.ordering. Without it `-start_time` survives into the
            # GROUP BY and the query returns one row per booking, not per day —
            # every count would be 1 and the response would still look plausible.
            .order_by()
            .values(LOCAL_DATE_ALIAS)
            .annotate(count=Count("id"))
        )
        counts = {row[LOCAL_DATE_ALIAS]: row["count"] for row in rows}

        days = []
        for offset in range((end_date - start_date).days + 1):
            day = start_date + timedelta(days=offset)
            days.append({"date": day, "count": counts.get(day, 0)})

        payload = BookingDailyCountResponseSerializer(
            {
                "start_date": start_date,
                "end_date": end_date,
                "timezone": zone_name,
                "total": sum(counts.values()),
                "days": days,
            }
        )
        return self.success(
            data=payload.data, message="Booking counts retrieved successfully"
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

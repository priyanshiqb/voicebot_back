from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from tenant.common import TenantBaseModelSerializer
from tenant.common.views.agent_tenant_scope import require_agent_tenant
from user.models import CustomUser
from core.models.booking import Booking
from core.models.booking_participant import BookingParticipant
from core.models.event_template import EventTemplate
from core.models.user_event_skill import UserEventSkill
from core.serializers.event_template import EventTemplateRefSerializer
from onboard.models.timezone import validate_iana_timezone
from utils.serializers.hybrid_related_field import HybridRelatedField
from user.serializers.custom_user import UserRefSerializer


class BookingParticipantSerializer(serializers.ModelSerializer):
    user = HybridRelatedField(serializer=UserRefSerializer, read_only=True)
    class Meta:
        model = BookingParticipant
        fields = ("id", "user", "name", "email", "phone_number", "role", "response_status")
        read_only_fields = fields


class BookingRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Booking
        fields = ("id", "title", "start_time", "status")
        read_only_fields = fields


class BookingSerializer(TenantBaseModelSerializer):
    """Read/detail serializer. Bookings are mutated only via the service layer,
    so every field is read-only here."""

    participants = BookingParticipantSerializer(many=True, read_only=True)
    event_template = HybridRelatedField(serializer=EventTemplateRefSerializer, read_only=True)
    primary_host = HybridRelatedField(serializer=UserRefSerializer, read_only=True)

    class Meta(TenantBaseModelSerializer.Meta):
        model = Booking
        fields = (
            "id",
            "tenant",
            "event_template",
            "title",
            "primary_host",
            "start_time",
            "end_time",
            "status",
            "source",
            "hold_expires_at",
            "reschedule_token",
            "reschedule_token_expires_at",
            "external_event_id",
            "external_provider",
            "group_id",
            "meeting_url",
            "cancellation_reason",
            "participants",
            "created_by",
            "updated_by",
            "deleted_by",
            "created_at",
            "updated_at",
            "deleted_at",
        )
        read_only_fields = fields


class BookingListSerializer(serializers.ModelSerializer):
    event_template = HybridRelatedField(serializer=EventTemplateRefSerializer, read_only=True)
    primary_host = HybridRelatedField(serializer=UserRefSerializer, read_only=True)

    class Meta:
        model = Booking
        fields = (
            "id",
            "event_template",
            "title",
            "primary_host",
            "start_time",
            "end_time",
            "status",
            "source",
            "meeting_url",
            "created_at",
        )


# Attendee rows are inserted inside the booking transaction, while a row lock is
# held on the host (see bookings._create_once). An unbounded list therefore holds
# that lock — blocking every other booking for that host — for as long as the
# inserts take. 25 is generous for a meeting; the point is that it is finite.
MAX_PARTICIPANTS_PER_BOOKING = 25


class BookingParticipantInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    email = serializers.EmailField()
    phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True)
    role = serializers.ChoiceField(
        choices=[BookingParticipant.Role.ATTENDEE, BookingParticipant.Role.GUEST],
        default=BookingParticipant.Role.ATTENDEE,
    )


class BookingCreateSerializer(serializers.Serializer):
    """Validates input for creating or holding a booking. The actual insert is
    performed by the booking service (which enforces overlap/host selection)."""

    # is_active is NOT filtered by the default manager (which only excludes
    # is_deleted), so this has to be explicit. Without it, deactivating a template
    # stopped the AI bridge booking it (agent_scheduling filters is_active) but not
    # the REST API — two callers disagreeing about what is bookable, with the admin
    # who deactivated it reasonably believing it was off.
    event_template = serializers.PrimaryKeyRelatedField(
        queryset=EventTemplate.objects.filter(is_active=True)
    )
    start_time = serializers.DateTimeField()
    target_host = serializers.PrimaryKeyRelatedField(
        queryset=CustomUser.objects.all(),
        required=False,
        allow_null=True,
        help_text=(
            "The host to book. Required for DIRECT event types; omit for "
            "COLLECTIVE, which books every skilled host."
        ),
    )
    participants = BookingParticipantInputSerializer(many=True)

    def validate_participants(self, value):
        if not value:
            raise serializers.ValidationError("At least one participant is required.")
        if len(value) > MAX_PARTICIPANTS_PER_BOOKING:
            raise serializers.ValidationError(
                f"A booking may have at most {MAX_PARTICIPANTS_PER_BOOKING} participants."
            )
        return value

    def validate(self, attrs):
        request = self.context.get("request")
        tenant = None
        if request and request.user and not request.user.is_superuser:
            # Machine callers name their tenant in `X-Tenant-ID`. They have no tenant
            # mapping, and the lookup below raises RelatedObjectDoesNotExist — which
            # subclasses AttributeError — so without this branch every VB_AGENT booking
            # would fail as "not associated with an active tenant".
            tenant = require_agent_tenant(request)
            if tenant is None:
                try:
                    tenant = request.user.tenant_mapping.tenant
                except AttributeError:
                    raise serializers.ValidationError(
                        "User is not associated with an active tenant."
                    )

        event_template = attrs["event_template"]
        if tenant is not None and event_template.tenant_id != tenant.id:
            raise serializers.ValidationError(
                {"event_template": "Event template does not belong to your tenant."}
            )
        is_collective = (
            event_template.assignment_strategy
            == EventTemplate.AssignmentStrategy.COLLECTIVE
        )
        target_host = attrs.get("target_host")

        if is_collective:
            # Every skilled host is booked; naming one is meaningless.
            if target_host:
                raise serializers.ValidationError(
                    {"target_host": "Collective event types book every skilled host."}
                )
            return attrs

        if not target_host:
            raise serializers.ValidationError(
                {"target_host": "This event type requires a specific host."}
            )
        # target_host is an unscoped queryset (any user in any tenant), and it is
        # now the sole host-selection input — so it must be checked here.
        host_tenant_id = tenant.id if tenant is not None else event_template.tenant_id
        if not UserEventSkill.objects.filter(
            tenant_id=host_tenant_id,
            event_template=event_template,
            user=target_host,
            is_deleted=False,
        ).exists():
            raise serializers.ValidationError(
                {"target_host": "This host is not configured to host this event type."}
            )
        return attrs


class BookingCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")


# The range is capped because the response is zero-filled — every date in it is
# returned whether or not anything is booked — so the payload size is the range
# length, not the result count. 61 days covers the widest thing a calendar UI
# asks for (two full months either side of a boundary) in one request.
MAX_DAILY_COUNT_RANGE_DAYS = 61


class BookingDailyCountQuerySerializer(serializers.Serializer):
    """Query params for the date-wise booking count."""

    start_date = serializers.DateField(
        help_text="First date to count, inclusive. `YYYY-MM-DD`, read in `timezone`."
    )
    end_date = serializers.DateField(
        help_text=(
            "Last date to count, INCLUSIVE — unlike the instant bounds on "
            "available-slots, because this parameter is a date a human picked on a "
            f"calendar. At most {MAX_DAILY_COUNT_RANGE_DAYS} days from `start_date`."
        )
    )
    timezone = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text=(
            "IANA zone that decides which date a booking falls on, e.g. "
            "Asia/Kolkata. NOT presentational: a 21:00 IST appointment is the "
            "previous day in UTC, so this changes the counts. Defaults to the "
            "caller's own timezone, then the tenant's, then UTC."
        ),
    )

    def validate_timezone(self, value):
        """Reject an unknown zone rather than falling back to UTC.

        Same reasoning as available-slots, but with more at stake: there the zone
        only spells a time, here it decides which bucket a booking lands in. A
        silent fallback would return a well-formed set of wrong numbers.
        """
        if not value:
            return value
        try:
            validate_iana_timezone(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages[0]) from exc
        return value

    def validate(self, attrs):
        start_date, end_date = attrs["start_date"], attrs["end_date"]
        if end_date < start_date:
            raise serializers.ValidationError(
                {"end_date": "end_date must be on or after start_date."}
            )
        # Inclusive on both ends, so a single day is a span of 1 — a caller asking
        # for exactly MAX days must not be told it is MAX + 1.
        span = (end_date - start_date).days + 1
        if span > MAX_DAILY_COUNT_RANGE_DAYS:
            raise serializers.ValidationError(
                f"Range covers {span} days; the maximum is "
                f"{MAX_DAILY_COUNT_RANGE_DAYS}."
            )
        return attrs


class BookingDayCountSerializer(serializers.Serializer):
    date = serializers.DateField()
    count = serializers.IntegerField()


class BookingDailyCountResponseSerializer(serializers.Serializer):
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    timezone = serializers.CharField(
        help_text="The zone the dates were resolved in — echoed back because it may have been defaulted."
    )
    total = serializers.IntegerField(help_text="Sum of `days[].count`.")
    days = BookingDayCountSerializer(
        many=True,
        help_text=(
            "One entry per date in the range, in order, INCLUDING dates with no "
            "bookings (`count: 0`). Leaving the caller to fill the gaps is how a "
            "chart ends up with unequal buckets."
        ),
    )

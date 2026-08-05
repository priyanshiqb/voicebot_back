from rest_framework import serializers

from tenant.common import TenantBaseModelSerializer
from tenant.common.views.agent_tenant_scope import require_agent_tenant
from user.models import CustomUser
from core.models.booking import Booking
from core.models.booking_participant import BookingParticipant
from core.models.event_template import EventTemplate
from core.models.user_event_skill import UserEventSkill
from core.serializers.event_template import EventTemplateRefSerializer
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

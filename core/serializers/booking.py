from rest_framework import serializers

from tenant.common import TenantBaseModelSerializer
from user.models import CustomUser
from core.models.booking import Booking
from core.models.booking_participant import BookingParticipant
from core.models.event_template import EventTemplate
from core.serializers.event_template import EventTemplateRefSerializer


class BookingParticipantSerializer(serializers.ModelSerializer):
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
    event_template = EventTemplateRefSerializer(read_only=True)

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
    event_template = EventTemplateRefSerializer(read_only=True)

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

    event_template = serializers.PrimaryKeyRelatedField(queryset=EventTemplate.objects.all())
    start_time = serializers.DateTimeField()
    target_host = serializers.PrimaryKeyRelatedField(
        queryset=CustomUser.objects.all(), required=False, allow_null=True
    )
    participants = BookingParticipantInputSerializer(many=True)

    def validate_participants(self, value):
        if not value:
            raise serializers.ValidationError("At least one participant is required.")
        return value

    def validate(self, attrs):
        request = self.context.get("request")
        tenant = None
        if request and request.user and not request.user.is_superuser:
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
        if (
            event_template.assignment_strategy
            == EventTemplate.AssignmentStrategy.DIRECT
            and not attrs.get("target_host")
        ):
            raise serializers.ValidationError(
                {"target_host": "This event type requires a specific host."}
            )
        return attrs


class BookingCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")

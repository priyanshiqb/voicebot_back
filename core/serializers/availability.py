from datetime import timedelta

from rest_framework import serializers

from user.models import CustomUser
from core.models.event_template import EventTemplate


class AvailableSlotsQuerySerializer(serializers.Serializer):
    event_template = serializers.PrimaryKeyRelatedField(
        # Same reason as BookingCreateSerializer: is_active is not filtered by the
        # default manager. Offering slots for a deactivated template would only
        # lead to a rejected booking.
        queryset=EventTemplate.objects.filter(is_active=True),
        help_text="Event template id. Must belong to the caller's tenant and be active."
    )
    start_utc = serializers.DateTimeField(
        help_text=(
            "Search window start, an ISO-8601 instant (e.g. 2026-08-08T04:00:00Z). "
            "Inclusive. A naive value is read as UTC."
        )
    )
    end_utc = serializers.DateTimeField(
        help_text=(
            "Search window end, an ISO-8601 instant. EXCLUSIVE, so consecutive "
            "windows tile without overlapping. Must be after start_utc and within "
            "the template's max_horizon_days."
        )
    )
    target_host = serializers.PrimaryKeyRelatedField(
        queryset=CustomUser.objects.all(),
        required=False,
        allow_null=True,
        help_text=(
            "The host whose slots to compute. Required for DIRECT event types "
            "(the same host must then be booked); omit for COLLECTIVE, which "
            "returns only slots where every skilled host is free."
        ),
    )

    def validate(self, attrs):
        if attrs["end_utc"] <= attrs["start_utc"]:
            raise serializers.ValidationError("end_utc must be after start_utc.")
        event_template = attrs["event_template"]
        span = attrs["end_utc"] - attrs["start_utc"]
        if span > timedelta(days=event_template.max_horizon_days):
            raise serializers.ValidationError(
                f"Range exceeds this event's {event_template.max_horizon_days}-day horizon."
            )

        is_collective = (
            event_template.assignment_strategy
            == EventTemplate.AssignmentStrategy.COLLECTIVE
        )
        target_host = attrs.get("target_host")
        # Booking a DIRECT event names its host, so slots must be computed for
        # that host. Returning the union over all skilled hosts would advertise
        # slots that 409 on booking.
        if not is_collective and not target_host:
            raise serializers.ValidationError(
                {"target_host": "This event type requires a specific host."}
            )
        if is_collective and target_host:
            raise serializers.ValidationError(
                {"target_host": "Collective event types consider every skilled host."}
            )
        return attrs


class SlotSerializer(serializers.Serializer):
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    eligible_host_ids = serializers.ListField(child=serializers.UUIDField())
    external_check_failed = serializers.BooleanField(
        help_text=(
            "True when a host's external calendar could not be reached, so this "
            "slot is offered on unverified information. Booking it will be "
            "refused with 503 while the provider is still unreachable."
        )
    )

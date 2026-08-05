from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from user.models import CustomUser
from core.models.event_template import EventTemplate
from onboard.models.timezone import validate_iana_timezone


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
    timezone = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text=(
            "IANA zone to render slot times in, e.g. Asia/Kolkata. Presentational "
            "only — it does not change which slots are returned, and every slot "
            "still carries its UTC `start`/`end`. Defaults to the target host's "
            "timezone, then the tenant's."
        ),
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

    def validate_timezone(self, value):
        """Reject an unknown zone at the boundary.

        Silently falling back would be worse than a 400: the caller would render
        every time in some other zone and have no way to notice, because the
        response would still look completely well-formed.
        """
        if not value:
            return value
        try:
            validate_iana_timezone(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages[0]) from exc
        return value

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
    start = serializers.DateTimeField(help_text="UTC. Use this value when booking.")
    end = serializers.DateTimeField(help_text="UTC.")
    start_local = serializers.CharField(
        help_text=(
            "The same instant as `start`, rendered in the response's `timezone` "
            "with that zone's offset for that date. Display only — booking with "
            "this value is equivalent, but `start` is the canonical form."
        )
    )
    end_local = serializers.CharField(help_text="The same instant as `end`, rendered locally.")
    eligible_host_ids = serializers.ListField(child=serializers.UUIDField())
    external_check_failed = serializers.BooleanField(
        help_text=(
            "True when a host's external calendar could not be reached, so this "
            "slot is offered on unverified information. Booking it will be "
            "refused with 503 while the provider is still unreachable."
        )
    )


class AvailableSlotsResponseSerializer(serializers.Serializer):
    """Slots plus the zone they were rendered in.

    The zone is returned rather than assumed because the caller's own timezone,
    the host's, and the tenant's can all differ — and a bare list of UTC instants
    gives a UI no way to label them without a second request.
    """

    timezone = serializers.CharField(
        help_text="IANA zone the `*_local` fields are expressed in."
    )
    slots = SlotSerializer(many=True)

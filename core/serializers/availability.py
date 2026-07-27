from rest_framework import serializers

from user.models import CustomUser
from core.models.event_template import EventTemplate


class AvailableSlotsQuerySerializer(serializers.Serializer):
    event_template = serializers.PrimaryKeyRelatedField(queryset=EventTemplate.objects.all())
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    target_host = serializers.PrimaryKeyRelatedField(
        queryset=CustomUser.objects.all(), required=False, allow_null=True
    )

    def validate(self, attrs):
        if attrs["date_to"] < attrs["date_from"]:
            raise serializers.ValidationError("date_to must be on or after date_from.")
        event_template = attrs["event_template"]
        span_days = (attrs["date_to"] - attrs["date_from"]).days
        if span_days > event_template.max_horizon_days:
            raise serializers.ValidationError(
                f"Date range exceeds this event's {event_template.max_horizon_days}-day horizon."
            )
        return attrs


class SlotSerializer(serializers.Serializer):
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    eligible_host_ids = serializers.ListField(child=serializers.UUIDField())

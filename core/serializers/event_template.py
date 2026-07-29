from rest_framework import serializers

from tenant.common import TenantBaseModelSerializer
from core.models.event_template import EventTemplate


class EventTemplateRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = EventTemplate
        fields = ("id", "name", "color")
        read_only_fields = fields


class EventTemplateSerializer(TenantBaseModelSerializer):
    class Meta(TenantBaseModelSerializer.Meta):
        model = EventTemplate
        fields = (
            "id",
            "tenant",
            "name",
            "description",
            "duration_minutes",
            "buffer_before_minutes",
            "buffer_after_minutes",
            "assignment_strategy",
            "min_notice_minutes",
            "max_horizon_days",
            "slot_granularity_minutes",
            "cancellation_lead_minutes",
            "reschedule_lead_minutes",
            "location_type",
            "color",
            "is_active",
            "created_by",
            "updated_by",
            "deleted_by",
            "created_at",
            "updated_at",
            "deleted_at",
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields

    def validate_duration_minutes(self, value):
        if value < 1:
            raise serializers.ValidationError("Duration must be at least 1 minute.")
        return value

    def validate_slot_granularity_minutes(self, value):
        if value < 1:
            raise serializers.ValidationError("Slot granularity must be at least 1 minute.")
        return value

    def validate(self, attrs):
        name = attrs.get("name")
        request = self.context.get("request")
        tenant = None
        if request and request.user and not request.user.is_superuser:
            try:
                tenant = request.user.tenant_mapping.tenant
            except AttributeError:
                pass
        if name is not None and tenant is not None:
            qs = EventTemplate.objects.filter(name=name, tenant=tenant, is_deleted=False)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {"name": "An event template with this name already exists."}
                )
        return attrs


class EventTemplateListSerializer(serializers.ModelSerializer):
    class Meta:
        model = EventTemplate
        fields = (
            "id",
            "name",
            "description",
            "duration_minutes",
            "assignment_strategy",
            "location_type",
            "color",
            "is_active",
            "created_at",
        )


class EventTemplateDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="name")
    value = serializers.UUIDField(source="id")

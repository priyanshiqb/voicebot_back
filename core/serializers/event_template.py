from rest_framework import serializers

from tenant.common import TenantBaseModelSerializer
from core.models.event_template import (
    MAX_HORIZON_DAYS,
    MIN_SLOT_GRANULARITY_MINUTES,
    EventTemplate,
)


def _first_few(names, limit=3):
    """`a, b, c` — and `a, b, c, …` when there are more — for an error message."""
    shown = ", ".join(names[:limit])
    return f"{shown}, …" if len(names) > limit else shown


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

    def validate_is_active(self, value):
        """Refuse to deactivate a type that a bot or a bookable host still relies on.

        Only the switch from active to inactive is guarded; reactivating is always safe.
        """
        if self.instance is None or value or not self.instance.is_active:
            return value

        bots, hosts = self.instance.deactivation_blockers()
        if not bots and not hosts:
            return value

        reasons = []
        if bots:
            reasons.append(f"offered by {len(bots)} active bot(s): {_first_few(bots)}")
        if hosts:
            reasons.append(
                f"bookable through {len(hosts)} host(s) with an active availability "
                f"profile: {_first_few(hosts)}"
            )
        # Only the steps this template actually needs, in the order the reasons came.
        steps = []
        if bots:
            steps.append("remove it from those bots")
        if hosts:
            steps.append("deactivate those hosts' event skills")
        raise serializers.ValidationError(
            "This event type is still in use and cannot be deactivated: "
            + "; ".join(reasons)
            + ". First "
            + " and ".join(steps)
            + "."
        )

    def validate_duration_minutes(self, value):
        if value < 1:
            raise serializers.ValidationError("Duration must be at least 1 minute.")
        return value

    def validate_slot_granularity_minutes(self, value):
        if value < MIN_SLOT_GRANULARITY_MINUTES:
            raise serializers.ValidationError(
                f"Slot granularity must be at least {MIN_SLOT_GRANULARITY_MINUTES} minutes."
            )
        return value

    def validate_max_horizon_days(self, value):
        if not 1 <= value <= MAX_HORIZON_DAYS:
            raise serializers.ValidationError(
                f"Booking horizon must be between 1 and {MAX_HORIZON_DAYS} days."
            )
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

from django.db import transaction
from rest_framework import serializers

from onboard.models.timezone import Timezone
from tenant.common import TenantBaseModelSerializer
from core.models.availability_profile import AvailabilityProfile
from core.models.availability_rule import AvailabilityRule


class AvailabilityRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = AvailabilityRule
        fields = ("id", "weekday", "start_time", "end_time")
        read_only_fields = ("id",)

    def validate(self, attrs):
        if attrs["end_time"] <= attrs["start_time"]:
            raise serializers.ValidationError("end_time must be after start_time.")
        return attrs


class AvailabilityProfileRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = AvailabilityProfile
        fields = ("id", "name", "user")
        read_only_fields = fields


class AvailabilityProfileSerializer(TenantBaseModelSerializer):
    rules = AvailabilityRuleSerializer(many=True, required=False)
    timezone = serializers.PrimaryKeyRelatedField(queryset=Timezone.objects.all())

    class Meta(TenantBaseModelSerializer.Meta):
        model = AvailabilityProfile
        fields = (
            "id",
            "tenant",
            "user",
            "timezone",
            "name",
            "rules",
            "is_active",
            "created_by",
            "updated_by",
            "deleted_by",
            "created_at",
            "updated_at",
            "deleted_at",
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields

    def validate_rules(self, value):
        by_day = {}
        for rule in value:
            by_day.setdefault(rule["weekday"], []).append(
                (rule["start_time"], rule["end_time"])
            )
        for weekday, intervals in by_day.items():
            intervals.sort()
            for i in range(1, len(intervals)):
                if intervals[i][0] < intervals[i - 1][1]:
                    raise serializers.ValidationError(
                        f"Overlapping availability rules on weekday {weekday}."
                    )
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

        user = attrs.get("user") or getattr(self.instance, "user", None)
        if tenant is not None and user is not None:
            user_tenant = getattr(getattr(user, "tenant_mapping", None), "tenant", None)
            if user_tenant is None or user_tenant.id != tenant.id:
                raise serializers.ValidationError(
                    {"user": "User does not belong to your tenant."}
                )
            # Enforce one active profile per user (friendly 400 instead of a DB 500).
            dup = AvailabilityProfile.objects.filter(
                user=user, tenant=tenant, is_deleted=False
            )
            if self.instance:
                dup = dup.exclude(pk=self.instance.pk)
            if dup.exists():
                raise serializers.ValidationError(
                    {"user": "This user already has an availability profile."}
                )
        return attrs

    def _sync_rules(self, profile, rules):
        for rule in rules:
            AvailabilityRule.objects.create(tenant=profile.tenant, profile=profile, **rule)

    @transaction.atomic
    def create(self, validated_data):
        rules = validated_data.pop("rules", [])
        profile = super().create(validated_data)
        self._sync_rules(profile, rules)
        return profile

    @transaction.atomic
    def update(self, instance, validated_data):
        rules = validated_data.pop("rules", None)
        profile = super().update(instance, validated_data)
        if rules is not None:
            for existing in list(profile.rules.all()):
                existing.delete()
            self._sync_rules(profile, rules)
        return profile


class AvailabilityProfileListSerializer(serializers.ModelSerializer):
    class Meta:
        model = AvailabilityProfile
        fields = ("id", "user", "name", "timezone", "is_active", "created_at")


class AvailabilityProfileDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="name")
    value = serializers.UUIDField(source="id")

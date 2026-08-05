from onboard.serializers.timezone import TimezoneRefSerializer
from django.db import transaction
from rest_framework import serializers

from onboard.models.timezone import Timezone
from tenant.common import TenantBaseModelSerializer
from core.models.availability_profile import AvailabilityProfile
from core.models.availability_rule import AvailabilityRule
from utils.serializers.hybrid_related_field import HybridRelatedField
from user.serializers.custom_user import UserRefSerializer
from user.models import CustomUser


WEEK_SECONDS = 7 * 24 * 3600
DAY_SECONDS = 24 * 3600


def _seconds(t):
    return t.hour * 3600 + t.minute * 60 + t.second


def _placed(rule):
    """
    ``(offset, end)`` on a 0..WEEK_SECONDS timeline.

    A wrapping rule simply extends past its weekday's end; ``00:00 -> 00:00``
    becomes a full 24 hours, which is how seven such rules give true 24/7.
    """
    start = rule["weekday"] * DAY_SECONDS + _seconds(rule["start_time"])
    length = _seconds(rule["end_time"]) - _seconds(rule["start_time"])
    if length <= 0:
        length += DAY_SECONDS
    return start, start + length


class AvailabilityRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = AvailabilityRule
        fields = ("id", "weekday", "start_time", "end_time")
        read_only_fields = ("id",)

    # No per-rule end > start check: that is exactly the rule that forbids an
    # overnight block. `end_time <= start_time` now means "ends the next day",
    # and coherence is enforced across the whole week by
    # AvailabilityProfileSerializer.validate_rules.


class AvailabilityProfileRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = AvailabilityProfile
        fields = ("id", "name", "user")
        read_only_fields = fields


class AvailabilityProfileSerializer(TenantBaseModelSerializer):
    rules = AvailabilityRuleSerializer(many=True, required=False)
    timezone = HybridRelatedField(
        serializer=TimezoneRefSerializer,
        queryset=Timezone.objects.all(),
    )
    user = HybridRelatedField(
        serializer=UserRefSerializer,
        queryset=CustomUser.objects.all(),
    )

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
        """
        Reject overlapping blocks on a CIRCULAR week timeline.

        Per-weekday sorting cannot see a Monday-overnight block colliding with
        Tuesday morning, nor Sunday night colliding with Monday. Placing every
        rule on a 604 800-second timeline and comparing each pair against its
        neighbours a week either side catches all three cases identically.
        """
        placed = [_placed(rule) for rule in value]

        # Cheap over-specification guard before the O(n^2) pass (e.g. eight 24h
        # rules): more than a week of coverage must contain an overlap.
        if sum(end - start for start, end in placed) > WEEK_SECONDS:
            raise serializers.ValidationError(
                "These rules cover more than a full week, so they must overlap."
            )

        for i, a in enumerate(placed):
            for j, b in enumerate(placed[i + 1:], start=i + 1):
                for shift in (-WEEK_SECONDS, 0, WEEK_SECONDS):
                    if a[0] < b[1] + shift and b[0] + shift < a[1]:
                        raise serializers.ValidationError(
                            "Overlapping availability rules: weekday "
                            f"{value[i]['weekday']} conflicts with weekday "
                            f"{value[j]['weekday']}."
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

    def _remat(self, profile):
        """Queue a rebuild of this profile's materialized UTC windows.

        Post-commit, because the task re-reads the profile in its own connection
        and would otherwise see the pre-write state (or nothing at all).
        """
        from core.services import deferred
        from core.tasks import materialize_profile_task

        transaction.on_commit(
            deferred.safe(
                lambda: materialize_profile_task.delay(str(profile.id)),
                label=f"materialize profile {profile.id}",
            )
        )

    @transaction.atomic
    def create(self, validated_data):
        rules = validated_data.pop("rules", [])
        profile = super().create(validated_data)
        self._sync_rules(profile, rules)
        self._remat(profile)
        return profile

    @transaction.atomic
    def update(self, instance, validated_data):
        tz_before = instance.timezone_id
        rules = validated_data.pop("rules", None)
        profile = super().update(instance, validated_data)
        if rules is not None:
            for existing in list(profile.rules.all()):
                existing.delete()
            self._sync_rules(profile, rules)
        # A timezone change re-anchors every window even when no rule changed, and
        # deactivating a profile must purge them (materialize_profile handles the
        # purge branch itself).
        if (
            rules is not None
            or profile.timezone_id != tz_before
            or "is_active" in validated_data
        ):
            self._remat(profile)
        return profile


class AvailabilityProfileListSerializer(serializers.ModelSerializer):
    user = HybridRelatedField(
        serializer=UserRefSerializer,
        read_only=True,
    )
    timezone = HybridRelatedField(
        serializer=TimezoneRefSerializer,
        read_only=True,
    )

    class Meta:
        model = AvailabilityProfile
        fields = ("id", "user", "name", "timezone", "is_active", "created_at")


class AvailabilityProfileDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="name")
    value = serializers.UUIDField(source="id")

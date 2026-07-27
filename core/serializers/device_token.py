from rest_framework import serializers

from tenant.common import TenantBaseModelCoreSerializer
from core.models.device_token import DeviceToken


class DeviceTokenSerializer(TenantBaseModelCoreSerializer):
    class Meta(TenantBaseModelCoreSerializer.Meta):
        model = DeviceToken
        fields = (
            "id",
            "tenant",
            "user",
            "token",
            "platform",
            "is_active",
            "last_seen_at",
            "created_at",
            "updated_at",
            "deleted_at",
        )
        read_only_fields = TenantBaseModelCoreSerializer.Meta.read_only_fields + (
            "user",
            "is_active",
            "last_seen_at",
        )

    def validate_token(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("token is required.")
        return value.strip()


class DeviceTokenListSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceToken
        fields = ("id", "user", "platform", "is_active", "last_seen_at", "created_at")


class DeviceTokenDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="platform")
    value = serializers.UUIDField(source="id")

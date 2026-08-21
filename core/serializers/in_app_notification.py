from rest_framework import serializers

from tenant.common.serializers.tenant_base_model_core_serializer import (
    TenantBaseModelCoreSerializer,
)
from core.models.in_app_notification import InAppNotification
from user.serializers.custom_user import UserRefSerializer


class InAppNotificationRefSerializer(serializers.ModelSerializer):
    """Tier 1 Ref Serializer for InAppNotification."""
    
    class Meta:
        model = InAppNotification
        fields = ["id", "title", "notification_type", "is_read"]
        read_only_fields = fields


class InAppNotificationSerializer(TenantBaseModelCoreSerializer):
    """Tier 1 Main Serializer for InAppNotification."""

    user = UserRefSerializer(read_only=True)

    class Meta:
        model = InAppNotification
        fields = list(TenantBaseModelCoreSerializer.Meta.fields) + [
            "user",
            "title",
            "body",
            "notification_type",
            "is_read",
            "read_at",
        ]
        read_only_fields = list(TenantBaseModelCoreSerializer.Meta.read_only_fields) + [
            "user",
            "title",
            "body",
            "notification_type",
            "read_at",
        ]


class InAppNotificationListSerializer(serializers.ModelSerializer):
    """Tier 1 List Serializer for InAppNotification."""
    
    class Meta:
        model = InAppNotification
        fields = [
            "id",
            "title",
            "body",
            "notification_type",
            "is_read",
            "read_at",
            "created_at",
        ]


class InAppNotificationDropdownSerializer(serializers.Serializer):
    """Tier 1 Dropdown Serializer for InAppNotification."""
    value = serializers.UUIDField(source="id", read_only=True)
    label = serializers.CharField(source="title", read_only=True)

from rest_framework import serializers

from tenant.common import TenantBaseModelSerializer
from core.models.calendar_integration import CalendarIntegration


class CalendarIntegrationRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = CalendarIntegration
        fields = ("id", "provider", "account_email")
        read_only_fields = fields


class CalendarIntegrationSerializer(TenantBaseModelSerializer):
    """Read-only view of an integration. Integrations are created/updated only
    through the OAuth connect/callback flow — never direct writes — and token
    fields are deliberately excluded."""

    class Meta(TenantBaseModelSerializer.Meta):
        model = CalendarIntegration
        fields = (
            "id",
            "tenant",
            "user",
            "provider",
            "account_email",
            "sync_status",
            "token_expires_at",
            "watch_expires_at",
            "last_synced_at",
            "last_error",
            "is_active",
            "created_by",
            "updated_by",
            "deleted_by",
            "created_at",
            "updated_at",
            "deleted_at",
        )
        read_only_fields = fields


class CalendarIntegrationListSerializer(serializers.ModelSerializer):
    class Meta:
        model = CalendarIntegration
        fields = (
            "id",
            "user",
            "provider",
            "account_email",
            "sync_status",
            "created_at",
        )


class CalendarIntegrationDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="account_email")
    value = serializers.UUIDField(source="id")

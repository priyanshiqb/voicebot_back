from rest_framework import serializers

from tenant.common import TenantBaseModelCoreSerializer
from core.models.user_event_skill import UserEventSkill


class UserEventSkillRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserEventSkill
        fields = ("id", "user", "event_template")
        read_only_fields = fields


class UserEventSkillSerializer(TenantBaseModelCoreSerializer):
    class Meta(TenantBaseModelCoreSerializer.Meta):
        model = UserEventSkill
        fields = (
            "id",
            "tenant",
            "user",
            "event_template",
            "weight",
            "is_active",
            "created_at",
            "updated_at",
            "deleted_at",
        )
        read_only_fields = TenantBaseModelCoreSerializer.Meta.read_only_fields

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

        event_template = attrs.get("event_template") or getattr(
            self.instance, "event_template", None
        )
        user = attrs.get("user") or getattr(self.instance, "user", None)

        if tenant is not None:
            if event_template is not None and event_template.tenant_id != tenant.id:
                raise serializers.ValidationError(
                    {"event_template": "Event template does not belong to your tenant."}
                )
            if user is not None:
                user_tenant = getattr(getattr(user, "tenant_mapping", None), "tenant", None)
                if user_tenant is None or user_tenant.id != tenant.id:
                    raise serializers.ValidationError(
                        {"user": "User does not belong to your tenant."}
                    )
        return attrs


class UserEventSkillListSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserEventSkill
        fields = ("id", "user", "event_template", "weight", "is_active", "created_at")


class UserEventSkillDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="id")
    value = serializers.UUIDField(source="id")

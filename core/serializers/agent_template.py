import re
from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from core.models.agent_template import AgentTemplate


class AgentTemplateRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentTemplate
        fields = ("id", "name")


class AgentTemplateSerializer(TenantBaseModelSerializer):
    class Meta(TenantBaseModelSerializer.Meta):
        model = AgentTemplate
        fields = (
            "id",
            "tenant",
            "name",
            "short_description",
            "visual_icon",
            "primary_accent",
            "agent_prompt",
            "strict_facts",
            "can_user_edit_flow",
            "created_by",
            "updated_by",
            "deleted_by",
            "created_at",
            "updated_at",
            "deleted_at",
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields

    def validate_name(self, value):
        if len(value) < 2 or len(value) > 100:
            raise serializers.ValidationError("Name must be between 2 and 100 characters.")
        if not re.match(r"^[\w\s-]+$", value):
            raise serializers.ValidationError("Template Name can only contain letters, numbers, spaces, hyphens, and underscores.")
        return value

    def validate_short_description(self, value):
        if len(value) < 10 or len(value) > 300:
            raise serializers.ValidationError("Short description must be between 10 and 300 characters.")
        return value

    def validate_agent_prompt(self, value):
        if len(value) < 30 or len(value) > 5000:
            raise serializers.ValidationError("Agent prompt must be between 30 and 5000 characters.")
        return value

    def validate_primary_accent(self, value):
        if value:
            if not re.match(r"^#[0-9A-Fa-f]{6}$", value):
                raise serializers.ValidationError("Primary accent must be a valid 6-digit hex color code starting with '#' (e.g., #00FF00).")
        return value

    def validate(self, attrs):
        name = attrs.get("name")
        if name is not None:
            request = self.context.get("request")
            tenant = None
            if request and request.user and not request.user.is_superuser:
                try:
                    tenant = request.user.tenant_mapping.tenant
                except AttributeError:
                    pass

            qs = AgentTemplate.objects.filter(name=name, tenant=tenant, is_deleted=False)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"name": "An active agent template with this name already exists for your tenant."})
        return attrs


class AgentTemplateListSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentTemplate
        fields = (
            "id",
            "name",
            "short_description",
            "strict_facts",
            "can_user_edit_flow",
            "created_at",
        )


class AgentTemplateDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="name")
    value = serializers.UUIDField(source="id")

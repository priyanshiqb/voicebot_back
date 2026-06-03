import re
from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from utils.serializers.hybrid_related_field import HybridRelatedField
from core.models.agent import Agent
from core.models.agent_template import AgentTemplate
from core.models.knowledge_base import KnowledgeBase
from core.serializers.agent_template import AgentTemplateRefSerializer
from core.serializers.knowledge_base import KnowledgeBaseRefSerializer


class AgentRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Agent
        fields = ("id", "bot_name")


class AgentSerializer(TenantBaseModelSerializer):
    agent_template = HybridRelatedField(
        serializer=AgentTemplateRefSerializer,
        queryset=AgentTemplate.objects.all(),
        required=False,
        allow_null=True,
    )
    knowledge_bases = HybridRelatedField(
        serializer=KnowledgeBaseRefSerializer,
        queryset=KnowledgeBase.objects.all(),
        many=True,
        required=False,
    )

    class Meta(TenantBaseModelSerializer.Meta):
        model = Agent
        fields = (
            "id",
            "tenant",
            "bot_name",
            "persona_name",
            "languages",
            "tone",
            "industry",
            "agent_template",
            "voice_name",
            "knowledge_bases",
            "transfer_destinations",
            "created_by",
            "updated_by",
            "deleted_by",
            "created_at",
            "updated_at",
            "deleted_at",
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields

    def validate_bot_name(self, value):
        if len(value) < 2 or len(value) > 100:
            raise serializers.ValidationError("Bot name must be between 2 and 100 characters.")
        if not re.match(r"^[\w\s-]+$", value):
            raise serializers.ValidationError("Bot Name can only contain letters, numbers, spaces, hyphens, and underscores.")
        return value

    def validate_persona_name(self, value):
        if len(value) < 2 or len(value) > 100:
            raise serializers.ValidationError("Persona name must be between 2 and 100 characters.")
        if not re.match(r"^[\w\s-]+$", value):
            raise serializers.ValidationError("Persona Name can only contain letters, numbers, spaces, hyphens, and underscores.")
        return value

    def validate_languages(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Languages must be a list of language strings.")
        for lang in value:
            if not isinstance(lang, str) or not lang.strip():
                raise serializers.ValidationError("Each language code must be a non-empty string.")
        return value

    def validate(self, attrs):
        bot_name = attrs.get("bot_name")
        if bot_name is not None:
            request = self.context.get("request")
            tenant = None
            if request and request.user and not request.user.is_superuser:
                try:
                    tenant = request.user.tenant_mapping.tenant
                except AttributeError:
                    pass

            qs = Agent.objects.filter(bot_name=bot_name, tenant=tenant, is_deleted=False)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"bot_name": "An active agent with this bot name already exists for your tenant."})
        return attrs


class AgentListSerializer(serializers.ModelSerializer):
    agent_template = AgentTemplateRefSerializer(read_only=True)

    class Meta:
        model = Agent
        fields = (
            "id",
            "bot_name",
            "persona_name",
            "tone",
            "industry",
            "agent_template",
            "created_at",
        )


class AgentDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="bot_name")
    value = serializers.UUIDField(source="id")

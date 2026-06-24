import re
from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from utils.serializers.hybrid_related_field import HybridRelatedField
from core.models.agent import Agent
from core.models.agent_template import AgentTemplate
from core.models.knowledge_base import KnowledgeBase
from core.models.probing_question import ProbingQuestion
from core.serializers.agent_template import AgentTemplateRefSerializer, AgentTemplateRefWithQuestionSerializer
from core.serializers.knowledge_base import KnowledgeBaseRefSerializer
from core.serializers.probing_question import ProbingQuestionRefSerializer


class AgentRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Agent
        fields = ("id", "bot_name")


class AgentSerializer(TenantBaseModelSerializer):
    agent_template = HybridRelatedField(
        serializer=AgentTemplateRefWithQuestionSerializer,
        queryset=AgentTemplate.objects.all(),
        required=False,
        allow_null=True,
    )
    knowledge_bases = HybridRelatedField(
        serializer=KnowledgeBaseRefSerializer,
        read_only=True,
        many=True,
        required=False,
    )
    add_knowledge_bases = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
    )
    remove_knowledge_bases = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
    )
    probing_questions = HybridRelatedField(
        serializer=ProbingQuestionRefSerializer,
        read_only=True,
        many=True,
        required=False,
    )
    add_probing_questions = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
    )
    remove_probing_questions = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        write_only=True,
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
            "agent_template",
            "voice_name",
            "knowledge_bases",
            "add_knowledge_bases",
            "remove_knowledge_bases",
            "probing_questions",
            "add_probing_questions",
            "remove_probing_questions",
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

    def validate_knowledge_bases(self, value):
        request = self.context.get("request")
        active_tenant = None
        if request and request.user and not request.user.is_superuser:
            try:
                active_tenant = request.user.tenant_mapping.tenant
            except AttributeError:
                raise serializers.ValidationError("User is not associated with an active tenant.")

        if active_tenant:
            for kb in value:
                if kb.tenant != active_tenant:
                    raise serializers.ValidationError(
                        f"Knowledge Base {kb.id} does not belong to your tenant context."
                    )
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

        initial_data = getattr(self, "initial_data", {})
        if "knowledge_bases" in initial_data:
            raise serializers.ValidationError({"knowledge_bases": "This field is read-only. Use add_knowledge_bases/remove_knowledge_bases instead."})

        if "probing_questions" in initial_data:
            raise serializers.ValidationError({"probing_questions": "This field is read-only. Use add_probing_questions/remove_probing_questions instead."})

        # Add/remove knowledge bases validations
        add_kbs = attrs.get("add_knowledge_bases")
        remove_kbs = attrs.get("remove_knowledge_bases")

        request = self.context.get("request")
        active_tenant = None
        if request and request.user and not request.user.is_superuser:
            try:
                active_tenant = request.user.tenant_mapping.tenant
            except AttributeError:
                raise serializers.ValidationError("User is not associated with an active tenant.")

        if add_kbs:
            resolved_add_kbs = []
            for kb_id in add_kbs:
                try:
                    kb = KnowledgeBase.objects.get(id=kb_id, is_deleted=False)
                except KnowledgeBase.DoesNotExist:
                    raise serializers.ValidationError({"add_knowledge_bases": f"Knowledge base {kb_id} does not exist."})

                if active_tenant and kb.tenant != active_tenant:
                    raise serializers.ValidationError({
                        "add_knowledge_bases": f"Knowledge base {kb.id} does not belong to your tenant context."
                    })
                resolved_add_kbs.append(kb)
            attrs["resolved_add_knowledge_bases"] = resolved_add_kbs

        if remove_kbs:
            resolved_remove_kbs = []
            for kb_id in remove_kbs:
                try:
                    kb = KnowledgeBase.objects.get(id=kb_id, is_deleted=False)
                except KnowledgeBase.DoesNotExist:
                    raise serializers.ValidationError({"remove_knowledge_bases": f"Knowledge base {kb_id} does not exist."})
                resolved_remove_kbs.append(kb)
            attrs["resolved_remove_knowledge_bases"] = resolved_remove_kbs

        # Add/remove probing questions validations
        add_pqs = attrs.get("add_probing_questions")
        remove_pqs = attrs.get("remove_probing_questions")

        if add_pqs:
            resolved_add_pqs = []
            for pq_id in add_pqs:
                try:
                    pq = ProbingQuestion.objects.get(id=pq_id, is_deleted=False)
                except ProbingQuestion.DoesNotExist:
                    raise serializers.ValidationError({"add_probing_questions": f"Probing question {pq_id} does not exist."})

                if active_tenant and pq.tenant != active_tenant and not pq.is_default:
                    raise serializers.ValidationError({
                        "add_probing_questions": f"Probing question {pq.id} does not belong to your tenant context and is not a default question."
                    })
                resolved_add_pqs.append(pq)
            attrs["resolved_add_probing_questions"] = resolved_add_pqs

        if remove_pqs:
            resolved_remove_pqs = []
            for pq_id in remove_pqs:
                try:
                    pq = ProbingQuestion.objects.get(id=pq_id, is_deleted=False)
                except ProbingQuestion.DoesNotExist:
                    raise serializers.ValidationError({"remove_probing_questions": f"Probing question {pq_id} does not exist."})
                resolved_remove_pqs.append(pq)
            attrs["resolved_remove_probing_questions"] = resolved_remove_pqs

        return attrs

    def create(self, validated_data):
        resolved_add_kbs = validated_data.pop("resolved_add_knowledge_bases", None)
        validated_data.pop("resolved_remove_knowledge_bases", None)
        validated_data.pop("add_knowledge_bases", None)
        validated_data.pop("remove_knowledge_bases", None)

        resolved_add_pqs = validated_data.pop("resolved_add_probing_questions", None)
        validated_data.pop("resolved_remove_probing_questions", None)
        validated_data.pop("add_probing_questions", None)
        validated_data.pop("remove_probing_questions", None)

        from django.db import transaction
        with transaction.atomic():
            instance = super().create(validated_data)

            if resolved_add_kbs:
                instance.knowledge_bases.add(*resolved_add_kbs)
            if resolved_add_pqs:
                instance.probing_questions.add(*resolved_add_pqs)
            return instance

    def update(self, instance, validated_data):
        resolved_add_kbs = validated_data.pop("resolved_add_knowledge_bases", None)
        resolved_remove_kbs = validated_data.pop("resolved_remove_knowledge_bases", None)
        validated_data.pop("add_knowledge_bases", None)
        validated_data.pop("remove_knowledge_bases", None)

        resolved_add_pqs = validated_data.pop("resolved_add_probing_questions", None)
        resolved_remove_pqs = validated_data.pop("resolved_remove_probing_questions", None)
        validated_data.pop("add_probing_questions", None)
        validated_data.pop("remove_probing_questions", None)

        from django.db import transaction
        with transaction.atomic():
            instance = super().update(instance, validated_data)

            if resolved_remove_kbs:
                instance.knowledge_bases.remove(*resolved_remove_kbs)
            if resolved_add_kbs:
                instance.knowledge_bases.add(*resolved_add_kbs)

            if resolved_remove_pqs:
                instance.probing_questions.remove(*resolved_remove_pqs)
            if resolved_add_pqs:
                instance.probing_questions.add(*resolved_add_pqs)
            return instance


class AgentListSerializer(serializers.ModelSerializer):
    agent_template = AgentTemplateRefSerializer(read_only=True)

    class Meta:
        model = Agent
        fields = (
            "id",
            "bot_name",
            "persona_name",
            "tone",
            "agent_template",
            "created_at",
        )


class AgentDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="bot_name")
    value = serializers.UUIDField(source="id")

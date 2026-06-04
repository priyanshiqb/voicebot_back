import re
from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from utils.serializers.hybrid_related_field import HybridRelatedField
from core.models.agent_template import AgentTemplate
from core.models.probing_question import ProbingQuestion
from core.serializers.probing_question import ProbingQuestionRefSerializer


class AgentTemplateRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentTemplate
        fields = ("id", "name")


class AgentTemplateSerializer(TenantBaseModelSerializer):
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
        model = AgentTemplate
        fields = (
            "id",
            "tenant",
            "name",
            "short_description",
            "visual_icon",
            "primary_accent",
            "agent_prompt",
            "industry",
            "strict_facts",
            "can_user_edit_flow",
            "is_active",
            "probing_questions",
            "add_probing_questions",
            "remove_probing_questions",
            "created_by",
            "updated_by",
            "deleted_by",
            "created_at",
            "updated_at",
            "deleted_at",
            "is_default",
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + ("is_default",)

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

    def validate_probing_questions(self, value):
        request = self.context.get("request")
        if request and request.user and not request.user.is_superuser:
            try:
                active_tenant = request.user.tenant_mapping.tenant
            except AttributeError:
                raise serializers.ValidationError("User is not associated with an active tenant.")
            
            for pq in value:
                if pq.tenant != active_tenant and not pq.is_default:
                    raise serializers.ValidationError(
                        f"Probing question {pq.id} does not belong to your tenant context and is not a default question."
                    )
        return value

    def validate(self, attrs):
        if self.instance and self.instance.is_default:
            raise serializers.ValidationError("Default agent templates cannot be modified.")

        initial_data = getattr(self, "initial_data", {})
        if "probing_questions" in initial_data:
            raise serializers.ValidationError({"probing_questions": "This field is read-only. Use add_probing_questions/remove_probing_questions instead."})

        add_pqs = attrs.get("add_probing_questions")
        remove_pqs = attrs.get("remove_probing_questions")

        request = self.context.get("request")
        active_tenant = None
        if request and request.user and not request.user.is_superuser:
            try:
                active_tenant = request.user.tenant_mapping.tenant
            except AttributeError:
                raise serializers.ValidationError("User is not associated with an active tenant.")

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

        name = attrs.get("name")
        if name is not None:
            tenant = None
            if request and request.user and not request.user.is_superuser:
                try:
                    tenant = request.user.tenant_mapping.tenant
                except AttributeError:
                    pass

            if tenant is not None:
                qs = AgentTemplate.objects.filter(name=name, tenant=tenant, is_deleted=False)
            else:
                qs = AgentTemplate.objects.filter(name=name, is_default=True, is_deleted=False)

            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"name": "An active agent template with this name already exists."})
        return attrs

    def create(self, validated_data):
        resolved_add_pqs = validated_data.pop("resolved_add_probing_questions", None)
        validated_data.pop("resolved_remove_probing_questions", None)
        validated_data.pop("add_probing_questions", None)
        validated_data.pop("remove_probing_questions", None)

        instance = super().create(validated_data)

        if resolved_add_pqs:
            instance.probing_questions.add(*resolved_add_pqs)
        return instance

    def update(self, instance, validated_data):
        resolved_add_pqs = validated_data.pop("resolved_add_probing_questions", None)
        resolved_remove_pqs = validated_data.pop("resolved_remove_probing_questions", None)
        validated_data.pop("add_probing_questions", None)
        validated_data.pop("remove_probing_questions", None)

        instance = super().update(instance, validated_data)

        if resolved_remove_pqs:
            instance.probing_questions.remove(*resolved_remove_pqs)
        if resolved_add_pqs:
            instance.probing_questions.add(*resolved_add_pqs)
        return instance


class AgentTemplateListSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentTemplate
        fields = (
            "id",
            "name",
            "short_description",
            "visual_icon",
            "primary_accent",
            "industry",
            "strict_facts",
            "can_user_edit_flow",
            "is_default",
            "is_active",
            "created_at",
        )


class AgentTemplateDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="name")
    value = serializers.UUIDField(source="id")

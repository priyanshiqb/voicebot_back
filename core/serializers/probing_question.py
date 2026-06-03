from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from utils.serializers.hybrid_related_field import HybridRelatedField
from core.models.agent_template import AgentTemplate
from core.models.probing_question import ProbingQuestion
from core.serializers.agent_template import AgentTemplateRefSerializer


class ProbingQuestionRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProbingQuestion
        fields = ("id", "question")


class ProbingQuestionSerializer(TenantBaseModelSerializer):
    agent_template = HybridRelatedField(
        serializer=AgentTemplateRefSerializer,
        queryset=AgentTemplate.objects.all(),
    )

    class Meta(TenantBaseModelSerializer.Meta):
        model = ProbingQuestion
        fields = (
            "id",
            "tenant",
            "agent_template",
            "question",
            "priority",
            "score",
            "is_mandatory",
            "created_by",
            "updated_by",
            "deleted_by",
            "created_at",
            "updated_at",
            "deleted_at",
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields

    def validate_question(self, value):
        if len(value) < 10 or len(value) > 100:
            raise serializers.ValidationError("Question must be between 10 and 100 characters.")
        return value

    def validate_priority(self, value):
        if value < 1:
            raise serializers.ValidationError("Priority must be at least 1.")
        return value

    def validate_score(self, value):
        if value < 0 or value > 100:
            raise serializers.ValidationError("Score must be between 0 and 100.")
        return value

    def validate(self, attrs):
        question = attrs.get("question")
        agent_template = attrs.get("agent_template")

        if question is None and self.instance:
            question = self.instance.question
        if agent_template is None and self.instance:
            agent_template = self.instance.agent_template

        if question is not None and agent_template is not None:
            qs = ProbingQuestion.objects.filter(
                question=question,
                agent_template=agent_template,
                is_deleted=False,
            )
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({
                    "question": "An active probing question with this text already exists for this agent template."
                })
        return attrs


class ProbingQuestionListSerializer(serializers.ModelSerializer):
    agent_template = AgentTemplateRefSerializer(read_only=True)

    class Meta:
        model = ProbingQuestion
        fields = (
            "id",
            "agent_template",
            "question",
            "priority",
            "score",
            "is_mandatory",
            "created_at",
        )


class ProbingQuestionDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="question")
    value = serializers.UUIDField(source="id")

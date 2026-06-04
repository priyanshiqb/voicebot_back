from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from core.models.probing_question import ProbingQuestion



class ProbingQuestionRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProbingQuestion
        fields = ("id", "question")


class ProbingQuestionSerializer(TenantBaseModelSerializer):

    class Meta(TenantBaseModelSerializer.Meta):
        model = ProbingQuestion
        fields = (
            "id",
            "tenant",
            "question",
            "priority",
            "score",
            "is_mandatory",
            "can_user_edit",
            "created_by",
            "updated_by",
            "deleted_by",
            "created_at",
            "updated_at",
            "deleted_at",
            "is_active",
            "is_default",
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "can_user_edit",
            "is_default",
        )

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
        if self.instance:
            if self.instance.is_default:
                raise serializers.ValidationError("Default probing questions cannot be modified.")
            if not self.instance.can_user_edit:
                raise serializers.ValidationError("This probing question is read-only and cannot be modified.")

        question = attrs.get("question")

        if question is None and self.instance:
            question = self.instance.question

        if question is not None:
            request = self.context.get("request")
            tenant = None
            if request and request.user and not request.user.is_superuser:
                try:
                    tenant = request.user.tenant_mapping.tenant
                except AttributeError:
                    pass

            if tenant is not None:
                qs = ProbingQuestion.objects.filter(
                    question=question,
                    tenant=tenant,
                    is_deleted=False,
                )
            else:
                qs = ProbingQuestion.objects.filter(
                    question=question,
                    is_default=True,
                    is_deleted=False,
                )

            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({
                    "question": "An active probing question with this text already exists."
                })
        return attrs


class ProbingQuestionListSerializer(serializers.ModelSerializer):

    class Meta:
        model = ProbingQuestion
        fields = (
            "id",
            "question",
            "priority",
            "score",
            "is_mandatory",
            "can_user_edit",
            "is_default",
            "created_at",
            "is_active"
        )


class ProbingQuestionDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="question")
    value = serializers.UUIDField(source="id")

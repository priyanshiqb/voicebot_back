from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from utils.serializers.hybrid_related_field import HybridRelatedField
from core.models.knowledge_base import KBQuestionAnswer, KBQuestions
from core.serializers.kb_questions import KBQuestionsRefSerializer

class KBQuestionAnswerRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBQuestionAnswer
        fields = ("id", "answer")

class KBQuestionAnswerSerializer(TenantBaseModelSerializer):
    questions = HybridRelatedField(
        serializer=KBQuestionsRefSerializer,
        queryset=KBQuestions.objects.all(),
        many=True
    )

    class Meta(TenantBaseModelSerializer.Meta):
        model = KBQuestionAnswer
        fields = (
            "id", "tenant", "questions", "answer", "is_processed",
            "processing_status", "processing_error", "last_processed_at",
            "processing_history", "created_by", "updated_by", "deleted_by",
            "created_at", "updated_at", "deleted_at"
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "is_processed", "processing_status", "processing_error",
            "last_processed_at", "processing_history",
        )

    def validate_questions(self, value):
        if not value:
            raise serializers.ValidationError("At least one question must be linked.")
        return value

class KBQuestionAnswerListSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBQuestionAnswer
        fields = ("id", "answer", "is_processed", "processing_status", "created_at")

class KBQuestionAnswerDropdownSerializer(serializers.Serializer):
    label = serializers.SerializerMethodField()
    value = serializers.UUIDField(source="id")

    def get_label(self, obj):
        return obj.answer[:50] + "..." if len(obj.answer) > 50 else obj.answer

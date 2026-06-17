from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from utils.serializers.hybrid_related_field import HybridRelatedField
from core.models.knowledge_base import KBQuestionAnswer, KBQuestions
from core.serializers.kb_questions import KBQuestionsRefSerializer, NestedKBQuestionsSerializer

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
            "processing_status", "processing_error", "etl_id", "pending_at",
            "processing_at", "completed_at", "failed_at", "created_by", "updated_by", "deleted_by",
            "created_at", "updated_at", "deleted_at"
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "is_processed", "processing_status", "processing_error",
            "etl_id", "pending_at", "processing_at", "completed_at", "failed_at",
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


class NestedKBQuestionAnswerSerializer(KBQuestionAnswerSerializer):
    id = serializers.UUIDField(required=False)
    flag = serializers.ChoiceField(choices=["create", "update", "delete"], required=False)
    questions = NestedKBQuestionsSerializer(many=True, required=False)

    class Meta(KBQuestionAnswerSerializer.Meta):
        fields = KBQuestionAnswerSerializer.Meta.fields + ("id", "flag")

    def to_internal_value(self, data):
        flag = data.get('flag')
        if flag in ['update', 'delete'] or ('id' in data and not flag):
            record_id = data.get('id')
            if record_id:
                try:
                    existing = KBQuestionAnswer.objects.get(id=record_id, is_deleted=False)
                    for field in self.fields:
                        if field not in data and hasattr(existing, field):
                            if field not in ['tenant', 'questions', 'flag']:
                                data[field] = getattr(existing, field)
                except KBQuestionAnswer.DoesNotExist:
                    pass
        return super().to_internal_value(data)

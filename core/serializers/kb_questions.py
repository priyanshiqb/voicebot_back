from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from core.models.knowledge_base import KBQuestions

class KBQuestionsRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBQuestions
        fields = ("id", "question")

class KBQuestionsSerializer(TenantBaseModelSerializer):
    class Meta(TenantBaseModelSerializer.Meta):
        model = KBQuestions
        fields = (
            "id", "tenant", "question", "is_processed", "processing_status",
            "processing_error", "last_processed_at", "processing_history",
            "created_by", "updated_by", "deleted_by", "created_at", "updated_at", "deleted_at"
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "is_processed", "processing_status", "processing_error",
            "last_processed_at", "processing_history",
        )

class KBQuestionsListSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBQuestions
        fields = ("id", "question", "is_processed", "processing_status", "created_at")

class KBQuestionsDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="question")
    value = serializers.UUIDField(source="id")

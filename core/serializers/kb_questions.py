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
            "processing_error", "etl_id", "pending_at", "processing_at",
            "completed_at", "failed_at", "created_by", "updated_by", "deleted_by",
            "created_at", "updated_at", "deleted_at"
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "is_processed", "processing_status", "processing_error",
            "etl_id", "pending_at", "processing_at", "completed_at", "failed_at",
        )

class KBQuestionsListSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBQuestions
        fields = ("id", "question", "is_processed", "processing_status", "created_at")

class KBQuestionsDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="question")
    value = serializers.UUIDField(source="id")


class NestedKBQuestionsSerializer(KBQuestionsSerializer):
    id = serializers.UUIDField(required=False)
    flag = serializers.ChoiceField(choices=["create", "update", "delete"], required=False)

    class Meta(KBQuestionsSerializer.Meta):
        fields = KBQuestionsSerializer.Meta.fields + ("id", "flag")

    def to_internal_value(self, data):
        flag = data.get('flag')
        if flag in ['update', 'delete'] or ('id' in data and not flag):
            record_id = data.get('id')
            if record_id:
                try:
                    existing = KBQuestions.objects.get(id=record_id, is_deleted=False)
                    for field in self.fields:
                        if field not in data and hasattr(existing, field):
                            if field not in ['tenant', 'flag']:
                                data[field] = getattr(existing, field)
                except KBQuestions.DoesNotExist:
                    pass
        return super().to_internal_value(data)

from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from core.models.knowledge_base import KBText

class KBTextRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBText
        fields = ("id", "title")

class KBTextSerializer(TenantBaseModelSerializer):
    class Meta(TenantBaseModelSerializer.Meta):
        model = KBText
        fields = (
            "id", "tenant", "title", "description", "is_processed",
            "processing_status", "processing_error", "last_processed_at",
            "processing_history", "created_by", "updated_by", "deleted_by",
            "created_at", "updated_at", "deleted_at"
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "is_processed", "processing_status", "processing_error",
            "last_processed_at", "processing_history",
        )

class KBTextListSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBText
        fields = ("id", "title", "is_processed", "processing_status", "created_at")

class KBTextDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="title")
    value = serializers.UUIDField(source="id")

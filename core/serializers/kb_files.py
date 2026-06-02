import os
from django.conf import settings
from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from core.models.knowledge_base import KBFiles

class KBFilesRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBFiles
        fields = ("id", "original_name")

class KBFilesSerializer(TenantBaseModelSerializer):
    class Meta(TenantBaseModelSerializer.Meta):
        model = KBFiles
        fields = (
            "id", "tenant", "file", "original_name", "is_processed",
            "processing_status", "processing_error", "last_processed_at",
            "processing_history", "created_by", "updated_by", "deleted_by",
            "created_at", "updated_at", "deleted_at"
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "original_name", "is_processed", "processing_status",
            "processing_error", "last_processed_at", "processing_history",
        )

    def validate_file(self, value):
        max_size = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        if value.size > max_size:
            raise serializers.ValidationError(f"File size exceeds the limit of {max_size / (1024 * 1024)}MB.")
        allowed_extensions = ['.pdf', '.docx', '.txt']
        ext = os.path.splitext(value.name)[1].lower()
        if ext not in allowed_extensions:
            raise serializers.ValidationError(f"Unsupported format. Only {', '.join(allowed_extensions)} are allowed.")
        return value

class KBFilesListSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBFiles
        fields = ("id", "original_name", "file", "is_processed", "processing_status", "created_at")

class KBFilesDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="original_name")
    value = serializers.UUIDField(source="id")

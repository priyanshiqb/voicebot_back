import os
from django.conf import settings
from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from core.models.knowledge_base import KBFiles

class KBFilesRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBFiles
        fields = ("id", "original_name", "extension")

class KBFilesSerializer(TenantBaseModelSerializer):
    class Meta(TenantBaseModelSerializer.Meta):
        model = KBFiles
        fields = (
            "id", "tenant", "file", "original_name", "extension", "is_processed",
            "processing_status", "processing_error", "etl_id", "pending_at",
            "processing_at", "completed_at", "failed_at", "created_by", "updated_by", "deleted_by",
            "created_at", "updated_at", "deleted_at"
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "original_name", "is_processed", "processing_status",
            "processing_error", "etl_id", "pending_at", "processing_at", "completed_at", "failed_at",
        )

    def validate_file(self, value):
        max_size = getattr(settings, "DATA_UPLOAD_MAX_MEMORY_SIZE", 11 * 1024 * 1024)
        if value.size > max_size:
            raise serializers.ValidationError("File size exceeds the limit of 10 MB.")
        allowed_extensions = ['.pdf', '.docx', '.txt']
        ext = os.path.splitext(value.name)[1].lower()
        if ext not in allowed_extensions:
            raise serializers.ValidationError(f"Unsupported format. Only {', '.join(allowed_extensions)} are allowed.")
        return value

    def validate_extension(self, value):
        if value:
            allowed_extensions = ['.pdf', '.docx', '.txt']
            val_lower = value.lower()
            if not val_lower.startswith('.'):
                val_lower = '.' + val_lower
            if val_lower not in allowed_extensions:
                raise serializers.ValidationError(f"Unsupported format. Only {', '.join(allowed_extensions)} are allowed.")
            return val_lower
        return value

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        if instance.file and instance.file.name:
            if getattr(settings, 'USE_S3_STORAGE', False):
                from utils.s3_storage.service import S3StorageService
                try:
                    ret['file'] = S3StorageService().get_presigned_url_for_object(instance.file.name, expiry_hours=24)
                except Exception:
                    pass
        return ret

class KBFilesListSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBFiles
        fields = ("id", "original_name", "file", "extension", "is_processed", "processing_status", "created_at")

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        if instance.file and instance.file.name:
            if getattr(settings, 'USE_S3_STORAGE', False):
                from utils.s3_storage.service import S3StorageService
                try:
                    ret['file'] = S3StorageService().get_presigned_url_for_object(instance.file.name, expiry_hours=24)
                except Exception:
                    pass
        return ret

class KBFilesDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="original_name")
    value = serializers.UUIDField(source="id")


class NestedKBFilesSerializer(KBFilesSerializer):
    id = serializers.UUIDField(required=False)
    flag = serializers.ChoiceField(choices=["create", "update", "delete"], required=False)
    file = serializers.FileField(required=False)

    class Meta(KBFilesSerializer.Meta):
        fields = KBFilesSerializer.Meta.fields + ("id", "flag")

    def to_internal_value(self, data):
        flag = data.get('flag')
        if flag in ['update', 'delete'] or ('id' in data and not flag):
            record_id = data.get('id')
            if record_id:
                try:
                    existing = KBFiles.objects.get(id=record_id, is_deleted=False)
                    for field in self.fields:
                        if field not in data and hasattr(existing, field):
                            if field not in ['tenant', 'file', 'flag']:
                                data[field] = getattr(existing, field)
                except KBFiles.DoesNotExist:
                    pass
        return super().to_internal_value(data)

    def validate(self, attrs):
        flag = attrs.get('flag')
        record_id = attrs.get('id')
        if flag == 'create' or (not record_id and not flag):
            if 'file' not in attrs:
                raise serializers.ValidationError({"file": "No file was submitted."})
        return attrs

class RequestUploadUrlSerializer(serializers.Serializer):
    filename = serializers.CharField(max_length=255)
    content_type = serializers.CharField(max_length=100)
    file_size_in_bytes = serializers.IntegerField()

    def validate_file_size_in_bytes(self, value):
        max_size = getattr(settings, "DATA_UPLOAD_MAX_MEMORY_SIZE", 11 * 1024 * 1024)
        if value > max_size:
            raise serializers.ValidationError("File size exceeds the limit of 10 MB.")
        return value

    def validate_filename(self, value):
        allowed_extensions = ['.pdf', '.docx', '.txt']
        ext = os.path.splitext(value)[1].lower()
        if ext not in allowed_extensions:
            raise serializers.ValidationError(f"Unsupported format. Only {', '.join(allowed_extensions)} are allowed.")
        return value

class ConfirmUploadSerializer(serializers.Serializer):
    object_key = serializers.CharField(max_length=512)
    original_name = serializers.CharField(max_length=255)
    extension = serializers.CharField(max_length=10, required=False)

    def validate_original_name(self, value):
        allowed_extensions = ['.pdf', '.docx', '.txt']
        ext = os.path.splitext(value)[1].lower()
        if ext not in allowed_extensions:
            raise serializers.ValidationError(f"Unsupported format. Only {', '.join(allowed_extensions)} are allowed.")
        return value

    def validate_extension(self, value):
        if value:
            allowed_extensions = ['.pdf', '.docx', '.txt']
            val_lower = value.lower()
            if not val_lower.startswith('.'):
                val_lower = '.' + val_lower
            if val_lower not in allowed_extensions:
                raise serializers.ValidationError(f"Unsupported format. Only {', '.join(allowed_extensions)} are allowed.")
            return val_lower
        return value

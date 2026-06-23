import logging
from django.conf import settings
from django.urls import reverse
from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from core.models.knowledge_base import (
    KnowledgeBase,
    KBText,
    KBFiles,
    KBWebsite,
    KBQuestionAnswer,
    KBQuestions
)

logger = logging.getLogger(__name__)


class KBTextETLSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBText
        fields = (
            "id",
            "title",
            "description",
            "is_processed",
            "processing_status",
            "processing_error",
            "etl_id",
            "is_deleted",
        )


class KBFilesETLSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBFiles
        fields = (
            "id",
            "file",
            "original_name",
            "extension",
            "file_size_in_bytes",
            "is_processed",
            "processing_status",
            "processing_error",
            "etl_id",
            "is_deleted",
        )

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


class KBWebsiteETLSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBWebsite
        fields = (
            "id",
            "url",
            "is_processed",
            "processing_status",
            "processing_error",
            "etl_id",
            "is_deleted",
        )


class KBQuestionsETLSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBQuestions
        fields = (
            "id",
            "question",
            "is_deleted",
        )


class KBQuestionAnswerETLSerializer(serializers.ModelSerializer):
    questions = KBQuestionsETLSerializer(many=True, read_only=True)

    class Meta:
        model = KBQuestionAnswer
        fields = (
            "id",
            "title",
            "questions",
            "answer",
            "is_processed",
            "processing_status",
            "processing_error",
            "etl_id",
            "is_deleted",
        )


class KnowledgeBaseETLSerializer(TenantBaseModelSerializer):
    texts = KBTextETLSerializer(many=True, read_only=True)
    files = KBFilesETLSerializer(many=True, read_only=True)
    websites = KBWebsiteETLSerializer(many=True, read_only=True)
    question_answers = KBQuestionAnswerETLSerializer(many=True, read_only=True)
    callback_update_url = serializers.SerializerMethodField()

    class Meta(TenantBaseModelSerializer.Meta):
        model = KnowledgeBase
        fields = (
            "id",
            "name",
            "texts",
            "files",
            "websites",
            "question_answers",
            "is_processed",
            "tenant",
            "is_deleted",
            "callback_update_url",
            "etl_id"
        )
        read_only_fields = fields

    def get_callback_update_url(self, obj):
        callback_base = getattr(settings, "CALLBACK_URL_BASE", None)
        path = reverse("api-v1:core:knowledge-base-update-etl-status", kwargs={"pk": obj.id})

        if callback_base:
            return f"{callback_base.rstrip('/')}{path}"

        request = self.context.get('request')
        if request:
            try:
                return request.build_absolute_uri(path)
            except Exception as e:
                logger.warning(f"Failed to reverse update-etl-status URL: {e}")

        backend_url = getattr(settings, "BACKEND_PROJECT_URL", "http://localhost:8000")
        return f"{backend_url.rstrip('/')}{path}"


class UpdateETLStatusSerializer(serializers.Serializer):
    TYPE_CHOICES = (
        ("knowledgebase", "Knowledge Base"),
        ("file", "File"),
        ("website", "Website"),
        ("text", "Text"),
        ("qa", "Question Answer"),
    )
    type = serializers.ChoiceField(choices=TYPE_CHOICES)
    object_id = serializers.UUIDField()
    etl_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=KnowledgeBase.ProcessingStatusChoices.choices)
    timestamp = serializers.DateTimeField(required=False, allow_null=True)
    error_message = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    def validate(self, attrs):
        kb_instance = self.context.get("kb_instance")
        if not kb_instance:
            raise serializers.ValidationError("Knowledge Base context is missing.")

        obj_type = attrs.get("type")
        obj_id = attrs.get("object_id")

        target_obj = None

        if obj_type == "knowledgebase":
            if kb_instance.id != obj_id:
                raise serializers.ValidationError({"object_id": "Object ID does not match this Knowledge Base ID."})
            target_obj = kb_instance
        elif obj_type == "file":
            target_obj = kb_instance.files.filter(id=obj_id, is_deleted=False).first()
            if not target_obj:
                raise serializers.ValidationError({"object_id": "File with this ID was not found in this Knowledge Base."})
        elif obj_type == "website":
            target_obj = kb_instance.websites.filter(id=obj_id, is_deleted=False).first()
            if not target_obj:
                raise serializers.ValidationError({"object_id": "Website with this ID was not found in this Knowledge Base."})
        elif obj_type == "text":
            target_obj = kb_instance.texts.filter(id=obj_id, is_deleted=False).first()
            if not target_obj:
                raise serializers.ValidationError({"object_id": "Text with this ID was not found in this Knowledge Base."})
        elif obj_type == "qa":
            target_obj = kb_instance.question_answers.filter(id=obj_id, is_deleted=False).first()
            if not target_obj:
                raise serializers.ValidationError({"object_id": "Q&A with this ID was not found in this Knowledge Base."})

        self.target_obj = target_obj
        return attrs

    def save(self, **kwargs):
        etl_id = self.validated_data.get("etl_id")
        status = self.validated_data.get("status")
        timestamp = self.validated_data.get("timestamp")
        error_message = self.validated_data.get("error_message")

        return self.target_obj.update_etl_status(
            etl_id=etl_id,
            status=status,
            error_message=error_message,
            timestamp=timestamp
        )

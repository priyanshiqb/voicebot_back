from rest_framework import serializers
from tenant.common.serializers.tenant_base_model_serializer import TenantBaseModelSerializer
from core.models.agent_call_recording import AgentCallRecording

class AgentCallRecordingSerializer(TenantBaseModelSerializer):
    class Meta(TenantBaseModelSerializer.Meta):
        model = AgentCallRecording
        fields = TenantBaseModelSerializer.Meta.fields + [
            "file", 
            "original_name", 
            "file_size_in_bytes", 
            "extension", 
            "is_processed"
        ]
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields

class AgentCallRecordingRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentCallRecording
        fields = ["id", "original_name", "extension", "file_size_in_bytes", "file"]

class RequestRecordingUploadUrlSerializer(serializers.Serializer):
    filename = serializers.CharField(max_length=255, required=True, help_text="The original filename being uploaded (e.g., 'call_123.mp3').")
    content_type = serializers.ChoiceField(
        choices=[
            ("audio/mpeg", "audio/mpeg"),
            ("audio/mp3", "audio/mp3"),
        ],
        required=True,
        help_text="The MIME type of the file. Must be audio/mpeg or audio/mp3."
    )
    file_size_in_bytes = serializers.IntegerField(
        required=True, 
        min_value=1,
        help_text="The size of the MP3 file in bytes."
    )
    agent = serializers.UUIDField(
        required=False, 
        help_text="The ID of the Voicebot Agent. Required when the AI team (VB_AGENT) is uploading."
    )

class ConfirmRecordingUploadSerializer(serializers.Serializer):
    object_key = serializers.CharField(required=True, help_text="The S3 object key returned by request-upload-url.")
    original_name = serializers.CharField(max_length=255, required=True, help_text="The original filename.")
    extension = serializers.CharField(max_length=10, required=False, allow_blank=True, help_text="The file extension (e.g., '.mp3').")
    agent = serializers.UUIDField(
        required=False, 
        help_text="The ID of the Voicebot Agent. Required when the AI team (VB_AGENT) is confirming."
    )

from rest_framework import serializers
from tenant.common.serializers.tenant_base_model_core_serializer import TenantBaseModelCoreSerializer
from core.models.agent_call_recording import AgentCallRecording

class AgentCallRecordingSerializer(TenantBaseModelCoreSerializer):
    class Meta(TenantBaseModelCoreSerializer.Meta):
        model = AgentCallRecording
        fields = TenantBaseModelCoreSerializer.Meta.fields + [
            "file", 
            "original_name", 
            "file_size_in_bytes", 
            "extension", 
            "is_processed"
        ]
        read_only_fields = TenantBaseModelCoreSerializer.Meta.read_only_fields

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
    file_id = serializers.UUIDField(
        required=False,
        help_text="Preferred. The id returned by request-upload-url."
    )
    object_key = serializers.CharField(
        required=False,
        help_text="Deprecated — send file_id instead. Accepted during the transition window."
    )
    original_name = serializers.CharField(max_length=255, required=False, help_text="The original filename. Required only with object_key.")
    extension = serializers.CharField(max_length=10, required=False, allow_blank=True, help_text="The file extension (e.g., '.mp3').")
    agent = serializers.UUIDField(
        required=False,
        help_text="The ID of the Voicebot Agent. Required when the AI team (VB_AGENT) is confirming."
    )

    def validate(self, attrs):
        if not attrs.get("file_id") and not attrs.get("object_key"):
            raise serializers.ValidationError(
                {"file_id": "Provide file_id (preferred) or object_key."}
            )
        if not attrs.get("file_id") and not attrs.get("original_name"):
            raise serializers.ValidationError(
                {"original_name": "This field is required when using object_key."}
            )
        return attrs

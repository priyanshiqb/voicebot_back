from rest_framework import serializers
from tenant.common.serializers.tenant_base_model_serializer import TenantBaseModelSerializer
from core.models.custom_voice import CustomVoice

class CustomVoiceSerializer(TenantBaseModelSerializer):
    """
    Serializer for CustomVoice.
    Bypasses standard serialization to strictly accept and return the dynamic JSON payload.
    """
    class Meta(TenantBaseModelSerializer.Meta):
        model = CustomVoice
        fields = TenantBaseModelSerializer.Meta.fields + ["data"]
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields

    def to_internal_value(self, data):
        # We wrap the incoming raw payload inside the 'data' field expected by ModelSerializer
        formatted_data = {
            "data": data
        }
        return super().to_internal_value(formatted_data)

    def to_representation(self, instance):
        # Return only the JSON payload exactly as requested
        return instance.data



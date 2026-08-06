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
        # The stored payload, plus the two server-owned fields a client cannot
        # otherwise obtain. Without `id` the row is unaddressable: retrieve, delete
        # and bulk_delete all route on the primary key, and returning only the blob
        # meant nothing in any response could be fed back into those URLs.
        #
        # Server values are applied last and deliberately win over same-named keys in
        # the payload. A client-supplied `id` is inert — it never routes — so letting
        # it shadow the real primary key would reintroduce exactly the problem this
        # solves.
        return {
            **instance.data,
            "id": str(instance.id),
            "created_at": serializers.DateTimeField().to_representation(
                instance.created_at
            ),
        }



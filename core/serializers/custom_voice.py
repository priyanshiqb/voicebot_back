from rest_framework import serializers
from tenant.common.serializers.tenant_base_model_serializer import TenantBaseModelSerializer
from core.models.custom_voice import CustomVoice


def _tenant_of(context):
    """The caller's tenant, or None for a caller without one (the view refuses those)."""
    request = context.get("request")
    try:
        return request.user.tenant_mapping.tenant
    except Exception:
        return None

class CustomVoiceSerializer(TenantBaseModelSerializer):
    """
    Serializer for CustomVoice.
    Bypasses standard serialization to strictly accept and return the dynamic JSON payload.
    """
    class Meta(TenantBaseModelSerializer.Meta):
        model = CustomVoice
        fields = TenantBaseModelSerializer.Meta.fields + ["data"]
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields

    def validate(self, attrs):
        """Voice names are unique per tenant, ignoring case: the one rule on the payload.

        Everything else in the body is stored as sent. The name is what voice pickers
        show, so two voices sharing one cannot be told apart.
        """
        attrs = super().validate(attrs)
        payload = attrs.get("data")
        name = payload.get("name") if isinstance(payload, dict) else None
        tenant = _tenant_of(self.context)
        if isinstance(name, str) and name.strip() and tenant is not None:
            exclude_pk = self.instance.pk if self.instance is not None else None
            if CustomVoice.name_in_use(name, tenant=tenant, exclude_pk=exclude_pk):
                raise serializers.ValidationError(
                    {"name": ["A custom voice with this name already exists."]}
                )
        return attrs

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




class CustomVoiceNameCheckSerializer(serializers.Serializer):
    """The body of ``POST /custom-voices/check-name/``."""

    name = serializers.CharField(max_length=255)
    exclude_id = serializers.UUIDField(required=False, allow_null=True)

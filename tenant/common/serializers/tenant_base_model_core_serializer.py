from utils.serializers.base_model_core_serializer import BaseModelCoreSerializer
from utils.serializers.hybrid_related_field import HybridRelatedField
from tenant.serializers.tenant import TenantRefSerializer
from tenant.models.Tenant import Tenant


class TenantBaseModelCoreSerializer(BaseModelCoreSerializer):
    """
    Core Tenant-scoped Base Model Serializer.
    Uses HybridRelatedField to enable nested JSON Tenant views on read operations,
    while keeping the tenant relation protected as read-only for incoming validations.
    """
    tenant = HybridRelatedField(
        serializer=TenantRefSerializer,
        read_only=True,
    )

    class Meta(BaseModelCoreSerializer.Meta):
        abstract = True
        fields = BaseModelCoreSerializer.Meta.fields + [
            "tenant",
        ]
        read_only_fields = (
            *BaseModelCoreSerializer.Meta.read_only_fields,
            "tenant",
        )

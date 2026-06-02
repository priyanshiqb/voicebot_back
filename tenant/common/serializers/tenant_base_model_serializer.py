from utils.serializers.base_model_serializer import BaseModelSerializer
from utils.serializers.hybrid_related_field import HybridRelatedField
from tenant.serializers.tenant import TenantRefSerializer
from tenant.models.Tenant import Tenant


class TenantBaseModelSerializer(BaseModelSerializer):
    """
    Audited Tenant-scoped Base Model Serializer.
    Combines TenantBaseModelCoreSerializer's hybrid tenant references with BaseModelSerializer's
    audited user tracking nested lists (created_by, updated_by, deleted_by).
    """
    tenant = HybridRelatedField(
        serializer=TenantRefSerializer,
        read_only=True,
    )

    class Meta(BaseModelSerializer.Meta):
        abstract = True
        read_only_fields = (
            *BaseModelSerializer.Meta.read_only_fields,
            "tenant",
        )

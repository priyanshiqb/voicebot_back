from rest_framework import serializers
from tenant.common import TenantBaseModelSerializer
from core.models.knowledge_base import KBWebsite

class KBWebsiteRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBWebsite
        fields = ("id", "url")

class KBWebsiteSerializer(TenantBaseModelSerializer):
    class Meta(TenantBaseModelSerializer.Meta):
        model = KBWebsite
        fields = (
            "id", "tenant", "url", "is_processed", "processing_status",
            "processing_error", "last_processed_at", "processing_history",
            "created_by", "updated_by", "deleted_by", "created_at", "updated_at", "deleted_at"
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "is_processed", "processing_status", "processing_error",
            "last_processed_at", "processing_history",
        )

    def validate(self, attrs):
        url = attrs.get('url')
        request = self.context.get('request')
        tenant = None
        if request and request.user and not request.user.is_superuser:
            try:
                tenant = request.user.tenant_mapping.tenant
            except AttributeError:
                pass
        
        qs = KBWebsite.objects.filter(tenant=tenant, url=url, is_deleted=False)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError({"url": "This website URL has already been registered."})
        return attrs

class KBWebsiteListSerializer(serializers.ModelSerializer):
    class Meta:
        model = KBWebsite
        fields = ("id", "url", "is_processed", "processing_status", "created_at")

class KBWebsiteDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="url")
    value = serializers.UUIDField(source="id")

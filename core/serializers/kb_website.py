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
            "processing_error", "etl_id", "pending_at", "processing_at",
            "completed_at", "failed_at", "created_by", "updated_by", "deleted_by",
            "created_at", "updated_at", "deleted_at"
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "is_processed", "processing_status", "processing_error",
            "etl_id", "pending_at", "processing_at", "completed_at", "failed_at",
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


class NestedKBWebsiteSerializer(KBWebsiteSerializer):
    id = serializers.UUIDField(required=False)
    flag = serializers.ChoiceField(choices=["create", "update", "delete"], required=False)

    class Meta(KBWebsiteSerializer.Meta):
        fields = KBWebsiteSerializer.Meta.fields + ("id", "flag")

    def to_internal_value(self, data):
        flag = data.get('flag')
        if flag in ['update', 'delete'] or ('id' in data and not flag):
            record_id = data.get('id')
            if record_id:
                try:
                    existing = KBWebsite.objects.get(id=record_id, is_deleted=False)
                    for field in self.fields:
                        if field not in data and hasattr(existing, field):
                            if field not in ['tenant', 'flag']:
                                data[field] = getattr(existing, field)
                except KBWebsite.DoesNotExist:
                    pass
        return super().to_internal_value(data)

    def validate(self, attrs):
        flag = attrs.get('flag')
        if flag == 'delete':
            return attrs

        url = attrs.get('url')
        request = self.context.get('request')
        tenant = None
        if request and request.user and not request.user.is_superuser:
            try:
                tenant = request.user.tenant_mapping.tenant
            except AttributeError:
                pass

        qs = KBWebsite.objects.filter(tenant=tenant, url=url, is_deleted=False)
        record_id = attrs.get('id')
        if record_id:
            qs = qs.exclude(pk=record_id)
        if qs.exists():
            raise serializers.ValidationError({"url": "This website URL has already been registered."})
        return attrs

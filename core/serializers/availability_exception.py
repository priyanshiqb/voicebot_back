from rest_framework import serializers

from tenant.common import TenantBaseModelSerializer
from core.models.availability_exception import AvailabilityException


class AvailabilityExceptionRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = AvailabilityException
        fields = ("id", "user", "start_time", "end_time")
        read_only_fields = fields


class AvailabilityExceptionSerializer(TenantBaseModelSerializer):
    class Meta(TenantBaseModelSerializer.Meta):
        model = AvailabilityException
        fields = (
            "id",
            "tenant",
            "user",
            "start_time",
            "end_time",
            "type",
            "reason",
            "is_all_day",
            "is_active",
            "created_by",
            "updated_by",
            "deleted_by",
            "created_at",
            "updated_at",
            "deleted_at",
        )
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields

    def validate(self, attrs):
        start = attrs.get("start_time") or getattr(self.instance, "start_time", None)
        end = attrs.get("end_time") or getattr(self.instance, "end_time", None)
        if start and end and end <= start:
            raise serializers.ValidationError(
                {"end_time": "end_time must be after start_time."}
            )

        request = self.context.get("request")
        user = attrs.get("user") or getattr(self.instance, "user", None)
        if request and request.user and not request.user.is_superuser and user is not None:
            try:
                tenant = request.user.tenant_mapping.tenant
            except AttributeError:
                raise serializers.ValidationError(
                    "User is not associated with an active tenant."
                )
            user_tenant = getattr(getattr(user, "tenant_mapping", None), "tenant", None)
            if user_tenant is None or user_tenant.id != tenant.id:
                raise serializers.ValidationError(
                    {"user": "User does not belong to your tenant."}
                )
        return attrs


class AvailabilityExceptionListSerializer(serializers.ModelSerializer):
    class Meta:
        model = AvailabilityException
        fields = (
            "id",
            "user",
            "start_time",
            "end_time",
            "type",
            "is_all_day",
            "created_at",
        )


class AvailabilityExceptionDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="reason")
    value = serializers.UUIDField(source="id")

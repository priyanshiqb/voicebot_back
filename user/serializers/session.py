from rest_framework import serializers
from utils.serializers.base_model_core_serializer import BaseModelCoreSerializer
from user.models.user_session import UserSession

class UserSessionRefSerializer(BaseModelCoreSerializer):
    """
    Ref Serializer: Minimal identifiers.
    """
    class Meta(BaseModelCoreSerializer.Meta):
        model = UserSession
        fields = ["id", "device_name"]


class UserSessionSerializer(BaseModelCoreSerializer):
    """
    Main Serializer: Full details including is_current.
    """
    is_current = serializers.SerializerMethodField()

    class Meta(BaseModelCoreSerializer.Meta):
        model = UserSession
        fields = BaseModelCoreSerializer.Meta.read_only_fields + (
            "device_name",
            "ip_address",
            "is_active",
            "expires_at",
            "last_used",
            "is_current",
        )

    def get_is_current(self, obj) -> bool:
        request = self.context.get("request")
        if not request:
            return False
        auth = getattr(request, "auth", None)
        if auth and hasattr(auth, "get"):
            return obj.access_jti == auth.get("jti")
        return False


class UserSessionListSerializer(BaseModelCoreSerializer):
    """
    List Serializer: Optimized layout for active session listings.
    """
    is_current = serializers.SerializerMethodField()

    class Meta(BaseModelCoreSerializer.Meta):
        model = UserSession
        fields = (
            "id",
            "device_name",
            "ip_address",
            "is_active",
            "last_used",
            "created_at",
            "updated_at",
            "is_current",
        )

    def get_is_current(self, obj) -> bool:
        request = self.context.get("request")
        if not request:
            return False
        auth = getattr(request, "auth", None)
        if auth and hasattr(auth, "get"):
            return obj.access_jti == auth.get("jti")
        return False


class UserSessionDropdownSerializer(BaseModelCoreSerializer):
    """
    Dropdown Serializer: label and value picker.
    """
    label = serializers.SerializerMethodField()
    value = serializers.SerializerMethodField()

    class Meta(BaseModelCoreSerializer.Meta):
        model = UserSession
        fields = ["label", "value"]

    def get_label(self, obj):
        return f"{obj.device_name or 'Unknown'} (IP: {obj.ip_address or 'Unknown'})"

    def get_value(self, obj):
        return obj.id

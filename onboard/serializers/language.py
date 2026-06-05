from rest_framework import serializers
from onboard.models.language import Language
from utils.serializers.base_model_core_serializer import BaseModelCoreSerializer


class LanguageRefSerializer(serializers.ModelSerializer):
    """
    Language Reference Serializer.
    Extremely lightweight representation used for nested relations or brief displays.
    """
    class Meta:
        model = Language
        fields = (
            "id",
            "name",
            "code",
        )
        read_only_fields = fields


class LanguageSerializer(BaseModelCoreSerializer):
    """
    Main Language Serializer for detail, create, and update operations.
    """
    class Meta(BaseModelCoreSerializer.Meta):
        model = Language
        fields = (
            "id",
            "name",
            "code",
            "is_active",
            "created_at",
            "updated_at",
        )


class LanguageListSerializer(BaseModelCoreSerializer):
    """
    Language List Serializer.
    Optimized layout tailored for list displays, dashboards, and admin tables.
    """
    class Meta(BaseModelCoreSerializer.Meta):
        model = Language
        fields = (
            "id",
            "name",
            "code",
            "is_active",
            "created_at",
        )


class LanguageDropdownSerializer(serializers.ModelSerializer):
    """
    Language Dropdown Serializer.
    Returns label/value pairs matching unified selection patterns.
    """
    label = serializers.SerializerMethodField()
    value = serializers.UUIDField(source="id")

    class Meta:
        model = Language
        fields = (
            "label",
            "value",
        )
        read_only_fields = fields

    def get_label(self, obj) -> str:
        return f"{obj.name} ({obj.code})"

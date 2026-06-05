from rest_framework import serializers
from onboard.models.timezone import Timezone
from utils.serializers.base_model_core_serializer import BaseModelCoreSerializer


class TimezoneRefSerializer(serializers.ModelSerializer):
    """
    Timezone Reference Serializer.
    Extremely lightweight representation used for nested relations or brief displays.
    """
    class Meta:
        model = Timezone
        fields = (
            "id",
            "timezone",
            "offset",
        )
        read_only_fields = fields


class TimezoneSerializer(BaseModelCoreSerializer):
    """
    Main Timezone Serializer for detail, create, and update operations.
    """
    class Meta(BaseModelCoreSerializer.Meta):
        model = Timezone
        fields = (
            "id",
            "offset",
            "region",
            "timezone",
            "is_active",
            "created_at",
            "updated_at",
        )


class TimezoneListSerializer(BaseModelCoreSerializer):
    """
    Timezone List Serializer.
    Optimized layout tailored for list displays, dashboards, and admin tables.
    """
    class Meta(BaseModelCoreSerializer.Meta):
        model = Timezone
        fields = (
            "id",
            "offset",
            "region",
            "timezone",
            "is_active",
            "created_at",
        )


class TimezoneDropdownSerializer(serializers.ModelSerializer):
    """
    Timezone Dropdown Serializer.
    Returns label/value pairs matching unified selection patterns.
    """
    label = serializers.SerializerMethodField()
    value = serializers.UUIDField(source="id")

    class Meta:
        model = Timezone
        fields = (
            "label",
            "value",
        )
        read_only_fields = fields

    def get_label(self, obj) -> str:
        return f"{obj.timezone} ({obj.offset})"

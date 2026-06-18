from rest_framework import serializers
from onboard.models.postal_code import PostalCode
from utils.serializers.base_model_core_serializer import BaseModelCoreSerializer


class PostalCodeRefSerializer(serializers.ModelSerializer):
    """
    PostalCode Reference Serializer.
    Extremely lightweight representation used for nested relations or brief displays.
    """
    class Meta:
        model = PostalCode
        fields = (
            "id",
            "country_code",
            "postal_code",
            "place_name",
            "coordinates",
        )
        read_only_fields = fields


class PostalCodeSerializer(serializers.ModelSerializer):
    """
    Main PostalCode Serializer for detail, create, and update operations.
    """
    class Meta:
        model = PostalCode
        fields = (
            "id",
            "country_code",
            "postal_code",
            "place_name",
            "admin_name1",
            "admin_code1",
            "admin_name2",
            "admin_code2",
            "admin_name3",
            "admin_code3",
            "latitude",
            "longitude",
            "accuracy",
            "coordinates",
            "is_active",
        )
        read_only_fields = ("id", "coordinates")

    def validate_latitude(self, value):
        if value is not None and (value < -90 or value > 90):
            raise serializers.ValidationError("Latitude must be between -90 and 90 degrees.")
        return value

    def validate_longitude(self, value):
        if value is not None and (value < -180 or value > 180):
            raise serializers.ValidationError("Longitude must be between -180 and 180 degrees.")
        return value

    def validate_accuracy(self, value):
        if value is not None and (value < 1 or value > 6):
            raise serializers.ValidationError("Accuracy must be an integer between 1 and 6.")
        return value


class PostalCodeListSerializer(serializers.ModelSerializer):
    """
    PostalCode List Serializer.
    Optimized layout tailored for list displays, dashboards, and admin tables.
    """
    class Meta:
        model = PostalCode
        fields = (
            "id",
            "country_code",
            "postal_code",
            "place_name",
            "admin_name1",
            "admin_code1",
            "latitude",
            "longitude",
            "is_active",
        )



class PostalCodeDropdownSerializer(serializers.ModelSerializer):
    """
    PostalCode Dropdown Serializer.
    Returns label/value pairs matching unified selection patterns.
    """
    label = serializers.SerializerMethodField()
    value = serializers.UUIDField(source="id")

    class Meta:
        model = PostalCode
        fields = (
            "label",
            "value",
        )
        read_only_fields = fields

    def get_label(self, obj) -> str:
        return f"{obj.postal_code} - {obj.place_name} ({obj.country_code})"

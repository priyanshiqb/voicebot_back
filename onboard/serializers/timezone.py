from django.utils import timezone as dj_timezone
from rest_framework import serializers
from onboard.models.timezone import Timezone
from utils.serializers.base_model_core_serializer import BaseModelCoreSerializer


class CurrentOffsetMixin:
    """
    Adds ``current_offset`` — the offset actually in force right now.

    ``offset`` is a static standard-time string, so for any DST-observing zone it
    is wrong for roughly half the year (London reads UTC+00:00 in July). Clients
    should display ``current_offset``; ``offset`` is retained unchanged so no
    existing consumer breaks.

    ``timezone.now()`` is resolved once per serialization pass and cached on the
    serializer, so a long list costs one clock read rather than one per row.
    """

    def _now(self):
        if not hasattr(self, "_now_cache"):
            self._now_cache = dj_timezone.now()
        return self._now_cache

    def get_current_offset(self, obj) -> str:
        return obj.offset_label_at(self._now())


class TimezoneRefSerializer(CurrentOffsetMixin, serializers.ModelSerializer):
    """
    Timezone Reference Serializer.
    Extremely lightweight representation used for nested relations or brief displays.
    """
    current_offset = serializers.SerializerMethodField()

    class Meta:
        model = Timezone
        fields = (
            "id",
            "timezone",
            "offset",
            "current_offset",
        )
        read_only_fields = fields


class TimezoneSerializer(CurrentOffsetMixin, BaseModelCoreSerializer):
    """
    Main Timezone Serializer for detail, create, and update operations.
    """
    current_offset = serializers.SerializerMethodField()

    class Meta(BaseModelCoreSerializer.Meta):
        model = Timezone
        fields = (
            "id",
            "offset",
            "current_offset",
            "region",
            "timezone",
            "is_active",
            "created_at",
            "updated_at",
        )


class TimezoneListSerializer(CurrentOffsetMixin, BaseModelCoreSerializer):
    """
    Timezone List Serializer.
    Optimized layout tailored for list displays, dashboards, and admin tables.
    """
    current_offset = serializers.SerializerMethodField()

    class Meta(BaseModelCoreSerializer.Meta):
        model = Timezone
        fields = (
            "id",
            "offset",
            "current_offset",
            "region",
            "timezone",
            "is_active",
            "created_at",
        )


class TimezoneDropdownSerializer(CurrentOffsetMixin, serializers.ModelSerializer):
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
        # The live offset, not the stored string — a picker showing London as
        # UTC+00:00 in July is the most visible symptom of the static column.
        return f"{obj.timezone} ({obj.offset_label_at(self._now())})"

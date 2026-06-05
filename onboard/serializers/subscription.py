from rest_framework import serializers
from onboard.models.subscription import Subscription
from onboard.serializers.plan import PlanSerializer
from utils.serializers.base_model_core_serializer import BaseModelCoreSerializer


class SubscriptionRefSerializer(serializers.ModelSerializer):
    """
    Subscription Reference Serializer.
    Extremely lightweight representation used for nested relations or brief displays.
    """
    class Meta:
        model = Subscription
        fields = (
            "id",
            "status",
            "start_date",
            "end_date",
        )
        read_only_fields = fields


class SubscriptionSerializer(BaseModelCoreSerializer):
    """
    Main Subscription Serializer for detailed representation.
    """
    plan = PlanSerializer(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta(BaseModelCoreSerializer.Meta):
        model = Subscription
        fields = (
            "id",
            "status",
            "status_display",
            "start_date",
            "end_date",
            "plan_snapshot",
            "plan",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = BaseModelCoreSerializer.Meta.read_only_fields + (
            "status_display",
            "plan_snapshot",
        )


class SubscriptionListSerializer(BaseModelCoreSerializer):
    """
    Subscription List Serializer for dashboard displays.
    """
    plan_name = serializers.CharField(source="plan.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta(BaseModelCoreSerializer.Meta):
        model = Subscription
        fields = (
            "id",
            "status",
            "status_display",
            "start_date",
            "end_date",
            "plan_name",
            "is_active",
            "created_at",
        )


class SubscriptionDropdownSerializer(serializers.ModelSerializer):
    """
    Subscription Dropdown Serializer for option pickers.
    """
    label = serializers.SerializerMethodField()
    value = serializers.UUIDField(source="id")

    class Meta:
        model = Subscription
        fields = (
            "label",
            "value",
        )
        read_only_fields = fields

    def get_label(self, obj):
        plan_name = obj.plan.name if obj.plan else "Unknown Plan"
        return f"{plan_name} ({obj.get_status_display()})"

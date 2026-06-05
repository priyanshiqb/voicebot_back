from rest_framework import serializers
from onboard.models.plan import Plan
from utils.serializers.base_model_core_serializer import BaseModelCoreSerializer


class PlanRefSerializer(serializers.ModelSerializer):
    """
    Plan Reference Serializer.
    Extremely lightweight representation used for nested relations or brief displays.
    """
    tax_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    total_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = Plan
        fields = (
            "id",
            "code",
            "name",
            "base_price",
            "currency",
            "tax_amount",
            "total_price",
        )
        read_only_fields = fields


class PlanSerializer(BaseModelCoreSerializer):
    """
    Main Plan Serializer for detail, create, and update operations.
    Exposes all configuration fields and returns computed financial values as read-only.
    """
    discount_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    net_base_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    tax_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    total_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    billing_cycle_display = serializers.CharField(source="get_billing_cycle_display", read_only=True)
    plan_type_display = serializers.CharField(source="get_plan_type_display", read_only=True)
    currency_display = serializers.CharField(source="get_currency_display", read_only=True)

    class Meta(BaseModelCoreSerializer.Meta):
        model = Plan
        fields = (
            "id",
            "code",
            "name",
            "description",
            "base_price",
            "discount_percentage",
            "discount_amount",
            "net_base_price",
            "tax_percentage",
            "tax_amount",
            "total_price",
            "currency",
            "currency_display",
            "billing_cycle",
            "billing_cycle_display",
            "plan_type",
            "plan_type_display",
            "sort_order",
            "is_most_popular",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = BaseModelCoreSerializer.Meta.read_only_fields + (
            "discount_amount",
            "net_base_price",
            "tax_amount",
            "total_price",
        )


class PlanListSerializer(BaseModelCoreSerializer):
    """
    Plan List Serializer.
    Tailored representation for listing plans on administrative dashboards.
    """
    discount_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    net_base_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    tax_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    total_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    billing_cycle_display = serializers.CharField(source="get_billing_cycle_display", read_only=True)
    plan_type_display = serializers.CharField(source="get_plan_type_display", read_only=True)

    class Meta(BaseModelCoreSerializer.Meta):
        model = Plan
        fields = (
            "id",
            "code",
            "name",
            "base_price",
            "discount_percentage",
            "discount_amount",
            "net_base_price",
            "tax_percentage",
            "tax_amount",
            "total_price",
            "currency",
            "billing_cycle",
            "billing_cycle_display",
            "plan_type",
            "plan_type_display",
            "sort_order",
            "is_most_popular",
            "is_active",
            "created_at",
        )


class PlanDropdownSerializer(serializers.ModelSerializer):
    """
    Plan Dropdown Serializer.
    Returns label/value pairs matching unified selection patterns.
    """
    label = serializers.SerializerMethodField()
    value = serializers.UUIDField(source="id")

    class Meta:
        model = Plan
        fields = (
            "label",
            "value",
            "is_most_popular",
        )
        read_only_fields = fields

    def get_label(self, obj) -> str:
        type_str = obj.get_plan_type_display()
        cycle_str = obj.get_billing_cycle_display()
        return f"{obj.name} ({type_str} - {obj.total_price:.2f} {obj.currency}/{cycle_str})"

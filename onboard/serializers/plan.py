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
            "net_base_price",
            "currency",
            "tax_amount",
            "total_price",
        )
        read_only_fields = fields


class PlanOfferingMixin(serializers.Serializer):
    """What a plan actually gives you, beyond its price.

    Shared by the detail and list serializers because the pricing page reads the list
    endpoint — a plan you cannot compare on credits, limits and per-minute rates is
    just a number with a name.
    """

    entitlements = serializers.SerializerMethodField()
    rates = serializers.SerializerMethodField()
    is_minutes_based = serializers.BooleanField(read_only=True)
    is_fixed_quantity = serializers.BooleanField(read_only=True)

    def get_entitlements(self, obj) -> list:
        """What the plan includes of each resource, and what more of it costs."""
        return [
            {
                "resource": row.resource,
                "included": row.included_quantity,
                # Without the period the number is ambiguous — ten a month and ten a
                # year are the same figure and a very different offer.
                "reset_period": row.reset_period,
                "addon_available": row.is_addon_available,
                "price_per_block": str(row.price_per_additional),
                "block_size": row.addon_step,
                "max_additional": row.max_additional,
            }
            for row in obj.entitlements.filter(is_deleted=False).order_by("resource")
        ]

    def get_rates(self, obj) -> list:
        """What each call tier costs on this plan, in credits and in rupees.

        The rupee price is what a minute of that tier is *sold* at. Only the plan's
        ``credit_basis`` tier actually prices a purchase, but the other two are what
        let a pricing page say "and realtime runs ₹3.60/min" rather than leaving the
        customer to work it out from a credit rate.
        """
        return [
            {
                "operation": rate.operation,
                "unit": rate.unit,
                "credits_per_unit": rate.credits_per_unit,
                "price_per_minute_inr": (
                    str(rate.price_per_minute_inr)
                    if rate.price_per_minute_inr is not None
                    else None
                ),
                "minimum_units": rate.minimum_units,
                "is_basis": rate.operation == obj.credit_basis,
            }
            for rate in (
                obj.open_rates
                if getattr(obj, "open_rates", None) is not None
                else obj.credit_rates.filter(
                    is_deleted=False, effective_to__isnull=True
                ).order_by("operation")
            )
        ]


class PlanSerializer(PlanOfferingMixin, BaseModelCoreSerializer):
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
            "net_base_price",
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
            # What the plan actually gives you. The pricing page needs these to say
            # anything more useful than a price.
            "min_minutes",
            "max_minutes",
            "credit_basis",
            "is_minutes_based",
            "is_fixed_quantity",
            "cost_per_additional_credit",
            "max_rollover_credits",
            "allow_rollover_on_renewal",
            "allow_rollover_on_upgrade",
            "allow_rollover_on_downgrade",
            "allow_use_after_expiry",
            "low_balance_threshold",
            "entitlements",
            "rates",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = BaseModelCoreSerializer.Meta.read_only_fields + (
            "entitlements",
            "rates",
            "discount_amount",
            "net_base_price",
            "tax_amount",
            "total_price",
        )


class PlanListSerializer(PlanOfferingMixin, BaseModelCoreSerializer):
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
            "net_base_price",
            "discount_percentage",
            "discount_amount",
            "net_base_price",
            "tax_percentage",
            "tax_amount",
            "total_price",
            "currency",
            "billing_cycle",
            "min_minutes",
            "max_minutes",
            "credit_basis",
            "is_minutes_based",
            "is_fixed_quantity",
            "cost_per_additional_credit",
            "max_rollover_credits",
            "allow_rollover_on_renewal",
            "allow_rollover_on_upgrade",
            "allow_rollover_on_downgrade",
            "allow_use_after_expiry",
            "entitlements",
            "rates",
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

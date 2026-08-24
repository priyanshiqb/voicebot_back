from django.contrib import admin
from onboard.models.plan import Plan
from onboard.admin.plan_entitlement import PlanEntitlementInline
# Presentation-layer import only. A plan's rate card and its entitlements are one
# editing task, and splitting them across two pages means pricing a plan from
# memory. No model or service in onboard depends on wallet.
from wallet.admin.credit_rate import CreditRateInline
from utils.admin.base_admin_core import BaseAdminCore


@admin.register(Plan)
class PlanAdmin(BaseAdminCore):
    """
    Plan ModelAdmin interface configuration.
    Registers Plan model inheriting from BaseAdminCore with searches, filters,
    list displays, and calculated read-only parameters.
    """
    model = Plan
    inlines = [CreditRateInline, PlanEntitlementInline]

    list_display = (
        "code",
        "name",
        "plan_type",
        "currency",
        "base_price",
        "discount_percentage",
        "tax_percentage",
        "total_price_display",
        "billing_cycle",
        "sort_order",
        "is_most_popular",
        "is_active",
        "created_at",
    )

    list_filter = (
        "plan_type",
        "currency",
        "billing_cycle",
        "is_most_popular",
    ) + BaseAdminCore.list_filter

    search_fields = (
        "code",
        "name",
        "description",
    ) + BaseAdminCore.search_fields

    readonly_fields = BaseAdminCore.readonly_fields + (
        "discount_amount_display",
        "net_base_price_display",
        "tax_amount_display",
        "total_price_display",
    )

    ordering = ("sort_order", "-created_at")

    fieldsets = (
        ("General Details", {
            "fields": (
                "id",
                "code",
                "name",
                "description",
                "plan_type",
                "is_most_popular",
                "is_active",
                "sort_order",
            )
        }),
        ("Pricing Configuration", {
            "fields": (
                "base_price",
                "currency",
                "billing_cycle",
            )
        }),
        ("Discounts & Taxes", {
            "fields": (
                "discount_percentage",
                "tax_percentage",
            )
        }),
        ("Calculated Totals (Read-only)", {
            "fields": (
                "discount_amount_display",
                "net_base_price_display",
                "tax_amount_display",
                "total_price_display",
            )
        }),
        ("Credits & Rollover", {
            "fields": (
                "allocated_credits",
                "cost_per_additional_credit",
                "low_balance_threshold",
                "max_rollover_credits",
                "allow_rollover_on_renewal",
                "allow_rollover_on_upgrade",
                "allow_rollover_on_downgrade",
                "allow_use_after_expiry",
            ),
            "description": (
                "What a cycle grants and what happens to leftovers. Per-minute prices "
                "are not here — they are Credit Rate rows for this plan."
            ),
        }),
        ("Timestamps", {
            "fields": (
                "created_at",
                "updated_at",
                "deleted_at",
            )
        }),
    )

    # Helper display methods to format Decimal properties beautifully in Admin
    @admin.display(description="Discount Amount")
    def discount_amount_display(self, obj):
        return f"{obj.discount_amount:.2f} {obj.currency}"

    @admin.display(description="Net Base Price")
    def net_base_price_display(self, obj):
        return f"{obj.net_base_price:.2f} {obj.currency}"

    @admin.display(description="Tax Amount")
    def tax_amount_display(self, obj):
        return f"{obj.tax_amount:.2f} {obj.currency}"

    @admin.display(description="Total Price")
    def total_price_display(self, obj):
        return f"{obj.total_price:.2f} {obj.currency}"

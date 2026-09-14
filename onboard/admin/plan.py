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

    **Engineering's escape hatch, not where plans are made.** Plans are created and
    activated in the super admin console (``/api/v1/ops/plans/``), which refuses to
    activate an incomplete plan or an overlapping ladder. ``status`` is therefore
    read-only here: a plan saved from this page keeps its status, and a new one stays a
    draft until the console's gate lets it through.
    """
    model = Plan
    inlines = [CreditRateInline, PlanEntitlementInline]

    # BaseAdminCore's bulk activate/deactivate write `is_active` with `.update()`,
    # which skips both `Plan.save()` and the activation gate. "Activate" would make a
    # draft sellable with no readiness check; "deactivate" is silently undone by the
    # plan's next save, which re-derives `is_active` from `status`.
    actions = ("soft_delete_selected", "restore_selected")

    list_display = (
        "code",
        "name",
        "plan_type",
        "status",
        "min_minutes",
        "max_minutes",
        "credit_basis",
        "currency",
        "total_price",
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
        "status",
        "plan_type",
        "credit_basis",
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
        # Derived from status in Plan.save(), so a checkbox here would do nothing.
        "status",
        "is_active",
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
                "status",
                "is_active",
                "sort_order",
            ),
            "description": (
                "Status moves only through the super admin console, which checks the "
                "plan is complete before it can be sold."
            ),
        }),
        ("Pricing Configuration", {
            "fields": (
                "currency",
                "billing_cycle",
            )
        }),
        ("Minutes Band", {
            "fields": (
                "min_minutes",
                "max_minutes",
                "credit_basis",
            ),
            "description": (
                "The quantities this plan sells, and the call tier it is sold at. Leave "
                "min blank for anything up to max, or max blank for anything from min; "
                "equal values make a fixed pack. Price and grant are minutes x the "
                "price_per_minute_inr and credits_per_unit on this plan's Credit Rate "
                "row for that tier."
            ),
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
            ),
            "description": "For the plan's smallest sellable quantity.",
        }),
        ("Credits & Rollover", {
            "fields": (
                "cost_per_additional_credit",
                "low_balance_percent",
                "low_balance_threshold",
                "max_rollover_ratio",
                "max_rollover_credits",
                "allow_rollover_on_renewal",
                "allow_rollover_on_upgrade",
                "allow_rollover_on_downgrade",
                "allow_use_after_expiry",
            ),
            "description": (
                "What happens to a cycle's grant and its leftovers. The grant itself is "
                "not here: it is minutes x credits-per-minute, fixed on each "
                "subscription when it is sold. The percent and ratio scale with that "
                "grant and are read before the absolute threshold and cap."
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

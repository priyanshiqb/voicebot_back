from django.contrib import admin

from onboard.models.plan_entitlement import PlanEntitlement
from utils.admin.base_admin_core import BaseAdminCore


class PlanEntitlementInline(admin.TabularInline):
    """Edited on the plan itself, because that is the only place the numbers mean
    anything — "10 agents, +1 at ₹299" is a sentence about a plan, not a standalone
    record. A separate page would have you cross-referencing seven rows against the
    plan you are actually pricing.
    """

    model = PlanEntitlement
    extra = 0
    fields = (
        "resource",
        "included_quantity",
        "is_addon_available",
        "price_per_additional",
        "addon_step",
        "max_additional",
        "is_active",
    )
    ordering = ("resource",)


@admin.register(PlanEntitlement)
class PlanEntitlementAdmin(BaseAdminCore):
    """Standalone view, for answering "what does every plan charge for storage?" —
    a question the per-plan inline cannot show.
    """

    model = PlanEntitlement

    list_display = (
        "plan",
        "resource",
        "included_quantity_display",
        "is_addon_available",
        "price_per_additional",
        "addon_step",
        "max_additional",
        "is_active",
    )
    list_filter = ("resource", "is_addon_available", "plan") + BaseAdminCore.list_filter
    search_fields = ("plan__code", "plan__name", "resource") + BaseAdminCore.search_fields
    list_select_related = ("plan",)
    ordering = ("plan", "resource")

    fieldsets = (
        ("Allowance", {
            "fields": ("id", "plan", "resource", "included_quantity", "is_active"),
            "description": "Leave the included quantity blank for unlimited.",
        }),
        ("Add-On Pricing", {
            "fields": (
                "is_addon_available",
                "price_per_additional",
                "addon_step",
                "max_additional",
            ),
            "description": (
                "Recurring pre-tax rupees per block, charged every cycle for as long as "
                "the tenant keeps the extra. Add-ons cannot be enabled on an unlimited "
                "allowance."
            ),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at", "deleted_at")}),
    )

    @admin.display(description="Included", ordering="included_quantity")
    def included_quantity_display(self, obj):
        return "unlimited" if obj.is_unlimited else obj.included_quantity

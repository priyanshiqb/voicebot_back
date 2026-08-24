from django.contrib import admin

from onboard.models.tenant_entitlement import TenantEntitlement
from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore


@admin.register(TenantEntitlement)
class TenantEntitlementAdmin(TenantBaseAdminCore):
    """
    Extra capacity a tenant has bought, and what it adds to their bill each cycle.

    ``price_per_unit`` and ``addon_step`` are **read-only**: they are frozen at
    purchase so a later price rise does not silently re-price capacity a tenant
    already holds. Editing them here would rewrite what the customer agreed to pay,
    which is a conversation, not a form field. Granting capacity to make something
    right is what ``additional_quantity`` and ``notes`` are for.
    """

    model = TenantEntitlement

    list_display = (
        "tenant",
        "resource",
        "additional_quantity",
        "price_per_unit",
        "addon_step",
        "recurring_charge_display",
        "purchased_at",
        "is_active",
    )
    list_filter = ("resource",) + TenantBaseAdminCore.list_filter
    search_fields = (
        "tenant__name",
        "tenant__subdomain",
        "resource",
    ) + TenantBaseAdminCore.search_fields
    list_select_related = ("tenant",)
    ordering = ("tenant", "resource")

    readonly_fields = TenantBaseAdminCore.readonly_fields + (
        "price_per_unit",
        "addon_step",
        "recurring_charge_display",
    )

    fieldsets = (
        ("Capacity", {
            "fields": ("id", "tenant", "resource", "additional_quantity", "is_active"),
        }),
        ("Frozen Pricing (read-only)", {
            "fields": ("price_per_unit", "addon_step", "recurring_charge_display"),
            "description": (
                "Captured when the extras were last bought. Changing these would "
                "re-price capacity the tenant already holds."
            ),
        }),
        ("Provenance", {
            "fields": ("subscription", "purchased_at", "notes")}),
        ("Timestamps", {
            "fields": ("created_at", "updated_at", "deleted_at")}),
    )

    @admin.display(description="Per cycle")
    def recurring_charge_display(self, obj):
        return f"{obj.recurring_charge} (pre-tax)"

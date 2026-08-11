from django.contrib import admin

from core.models.catalog_variant import CatalogVariant
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin

AUDIT_FIELDS = (
    "id",
    "created_at",
    "created_by",
    "updated_at",
    "updated_by",
    "is_deleted",
    "deleted_at",
    "deleted_by",
)


@admin.register(CatalogVariant)
class CatalogVariantAdmin(TenantBaseAdmin):
    """Registered separately for SKU search and bulk repricing. The inline on
    CatalogItemAdmin is the normal way in."""

    model = CatalogVariant

    list_display = (
        "variant_label",
        "item",
        "sku",
        "is_default",
        "display_price",
        "availability_status",
        "stock_quantity",
        "is_active",
        "updated_at",
    )
    list_filter = (
        "pricing_type",
        "availability_status",
        "track_inventory",
        "unit_of_measure",
        "currency",
    ) + TenantBaseAdmin.list_filter
    search_fields = ("variant_label", "sku", "barcode", "item__name", "item__item_code")
    ordering = ("item", "sort_order")
    list_select_related = ("tenant", "item")
    raw_id_fields = ("item",)

    fieldsets = (
        (
            "Identity",
            {
                "fields": (
                    "tenant",
                    "item",
                    "variant_label",
                    "options",
                    "sku",
                    "barcode",
                    "is_default",
                    "sort_order",
                )
            },
        ),
        (
            "Pricing",
            {
                "fields": (
                    "pricing_type",
                    "price",
                    "base_price",
                    "max_price",
                    "compare_at_price",
                    "currency",
                    "unit_of_measure",
                    "unit_label",
                    "recurring_interval",
                    "recurring_interval_count",
                    "deposit_amount",
                    "duration_minutes",
                ),
                "description": (
                    "`price` is the per-unit rate when `base_price` is set, and the "
                    "percentage when pricing_type is RATE. `duration_minutes` must be "
                    "empty if the item is booked through an event template."
                ),
            },
        ),
        ("Tax", {"fields": ("tax_rate_percentage", "is_tax_inclusive", "tax_code")}),
        (
            "Availability",
            {
                "fields": (
                    "availability_status",
                    "track_inventory",
                    "stock_quantity",
                    "low_stock_threshold",
                    "min_order_quantity",
                    "quantity_increment",
                    "is_active",
                ),
                "description": (
                    "`availability_status` is stated, never derived from stock. "
                    "Nothing in this module decrements `stock_quantity`."
                ),
            },
        ),
        ("Audit", {"fields": AUDIT_FIELDS, "classes": ("collapse",)}),
    )

    @admin.display(description="Price")
    def display_price(self, obj):
        return obj.display_price

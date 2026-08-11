from django.contrib import admin

from core.models.catalog_item import CatalogItem
from core.models.catalog_item_media import CatalogItemMedia
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


class _TenantStampedInline(admin.TabularInline):
    """Inline that copies `tenant` down from the parent item.

    Both child models have a non-null `tenant` that the inline form does not show,
    so without this every add through the admin raises IntegrityError. Same fix as
    BusinessHoursIntervalInline.
    """

    extra = 0

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        parent = obj

        class TenantStampedFormSet(formset):
            def save_new(self, form, commit=True):
                instance = super().save_new(form, commit=False)
                if parent is not None:
                    instance.tenant_id = parent.tenant_id
                if commit:
                    instance.save()
                return instance

        return TenantStampedFormSet


class CatalogVariantInline(_TenantStampedInline):
    """Variants are edited on the item they belong to.

    A separate screen would mean pricing three sizes of the same product without
    ever seeing them side by side, which is how one of them ends up at last year's
    price.
    """

    model = CatalogVariant
    fields = (
        "variant_label",
        "sku",
        "is_default",
        "pricing_type",
        "price",
        "base_price",
        "currency",
        "unit_of_measure",
        "availability_status",
        "track_inventory",
        "stock_quantity",
        "sort_order",
        "is_active",
    )
    ordering = ("sort_order", "created_at")


class CatalogItemMediaInline(_TenantStampedInline):
    model = CatalogItemMedia
    fields = ("media_type", "file", "alt_text", "is_primary", "upload_status", "sort_order")
    readonly_fields = ("upload_status",)
    ordering = ("sort_order", "created_at")


@admin.register(CatalogItem)
class CatalogItemAdmin(TenantBaseAdmin):
    model = CatalogItem

    list_display = (
        "name",
        "item_kind",
        "category",
        "brand",
        "variant_count",
        "headline_price",
        "is_featured",
        "is_active",
        "updated_at",
    )
    list_filter = ("item_kind", "is_featured") + TenantBaseAdmin.list_filter
    search_fields = ("name", "item_code", "brand", "short_description", "description")
    ordering = ("sort_order", "-created_at")
    list_select_related = ("tenant", "category")
    raw_id_fields = ("category", "event_template")
    filter_horizontal = ("add_on_items",)
    inlines = (CatalogVariantInline, CatalogItemMediaInline)

    fieldsets = (
        (
            "Identity",
            {"fields": ("tenant", "item_kind", "name", "item_code", "category", "brand")},
        ),
        (
            "What the agent says",
            {
                "fields": ("short_description", "aliases", "pronunciation_hint"),
                "description": (
                    "`aliases` is what callers actually say — speech recognition "
                    "returns their words, not the catalog's."
                ),
            },
        ),
        ("Detail", {"fields": ("description", "tags", "attributes")}),
        (
            "Delivery & booking",
            {"fields": ("fulfilment_modes", "lead_time_minutes", "event_template")},
        ),
        ("Add-ons", {"fields": ("add_on_items",)}),
        (
            "Listing",
            {
                "fields": (
                    "available_from",
                    "available_until",
                    "is_featured",
                    "sort_order",
                    "is_active",
                )
            },
        ),
        ("Audit", {"fields": AUDIT_FIELDS, "classes": ("collapse",)}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("variants")

    def save_related(self, request, form, formsets, change):
        """Guarantee the default variant after the inlines have been written.

        `ensure_default_variant` has to run here rather than in `save_model`: at
        `save_model` time the inline formsets have not been saved, so an item created
        with three variants in one submission would still look empty and get a junk
        "Standard" row created alongside them.
        """
        super().save_related(request, form, formsets, change)
        form.instance.ensure_default_variant()

    @admin.display(description="Variants")
    def variant_count(self, obj):
        return obj.variants.count()

    @admin.display(description="Price")
    def headline_price(self, obj):
        """What a caller would be quoted. Blank means no default variant, which
        `save_related` should have made impossible — worth seeing if it happens."""
        variant = obj.default_variant
        return variant.display_price if variant else "—"

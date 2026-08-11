from django.contrib import admin

from core.models.catalog_category import CatalogCategory
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin


@admin.register(CatalogCategory)
class CatalogCategoryAdmin(TenantBaseAdmin):
    model = CatalogCategory

    list_display = (
        "indented_name",
        "depth",
        "parent",
        "item_count",
        "sort_order",
        "is_active",
        "updated_at",
    )
    list_filter = ("depth",) + TenantBaseAdmin.list_filter
    search_fields = ("name", "description")
    ordering = ("depth", "sort_order", "name")
    list_select_related = ("tenant", "parent")
    raw_id_fields = ("parent",)

    # Derived from `parent` on save. Editable, it would drift from the tree it
    # describes and quietly break the depth cap.
    readonly_fields = TenantBaseAdmin.readonly_fields + ("depth",)

    fieldsets = (
        ("Section", {"fields": ("tenant", "name", "parent", "depth", "description")}),
        ("Display", {"fields": ("sort_order", "is_active")}),
        (
            "Audit",
            {
                # Listed explicitly rather than splatting TenantBaseAdmin.readonly_fields,
                # which is built from a set and so has no stable order.
                "fields": (
                    "id",
                    "created_at",
                    "created_by",
                    "updated_at",
                    "updated_by",
                    "is_deleted",
                    "deleted_at",
                    "deleted_by",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Name", ordering="name")
    def indented_name(self, obj):
        """Nesting is the point of this screen; a flat list of names hides it."""
        return f"{'— ' * obj.depth}{obj.name}"

    @admin.display(description="Items")
    def item_count(self, obj):
        """Zero here is worth spotting — an empty section is one nobody filled in,
        and it is also the only state in which the category can be deleted."""
        return obj.items.count()

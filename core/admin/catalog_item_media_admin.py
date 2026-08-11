from django.contrib import admin

from core.models.catalog_item_media import CatalogItemMedia
from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore


@admin.register(CatalogItemMedia)
class CatalogItemMediaAdmin(TenantBaseAdminCore):
    """Tier 1 Core — media is derivative of an audited parent.

    The inline on CatalogItemAdmin is the normal way in; this screen exists for
    finding orphaned PENDING rows, which is what a client that requested an upload
    URL and never confirmed leaves behind.
    """

    model = CatalogItemMedia

    list_display = (
        "original_name",
        "item",
        "media_type",
        "upload_status",
        "file_size_in_bytes",
        "is_primary",
        "sort_order",
        "created_at",
    )
    list_filter = ("media_type", "upload_status", "is_primary") + TenantBaseAdminCore.list_filter
    search_fields = ("original_name", "alt_text", "item__name")
    ordering = ("item", "sort_order")
    list_select_related = ("tenant", "item")
    raw_id_fields = ("item",)

    # Written by the upload flow, not by hand. `upload_status` in particular is the
    # gate on whether the object is served at all, and flipping it manually would
    # publish a key that may have nothing behind it.
    readonly_fields = TenantBaseAdminCore.readonly_fields + (
        "upload_status",
        "file_size_in_bytes",
        "extension",
    )

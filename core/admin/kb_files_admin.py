from django.contrib import admin
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from core.models.knowledge_base import KBFiles

@admin.register(KBFiles)
class KBFilesAdmin(TenantBaseAdmin):
    """
    Plan-like ModelAdmin interface for KBFiles objects.
    Groups fields cleanly into segmented administrative blocks.
    """
    model = KBFiles

    list_display = (
        "id",
        "file",
        "original_name",
        "tenant",
        "file_size_in_bytes",
        "processing_status",
        "is_processed",
        "is_active",
        "created_at",
    )

    list_filter = (
        "processing_status",
        "is_processed",
    ) + TenantBaseAdmin.list_filter

    search_fields = (
        "file",
        "original_name",
    ) + TenantBaseAdmin.search_fields

    readonly_fields = TenantBaseAdmin.readonly_fields + (
        "file_size_in_bytes",
        "is_processed",
        "processing_status",
        "processing_error",
        "last_processed_at",
        "processing_history",
    )

    ordering = ("-created_at",)

    fieldsets = (
        ("General Details", {
            "fields": (
                "id",
                "tenant",
                "file",
                "original_name",
                "file_size_in_bytes",
                "is_active",
            )
        }),
        ("ETL / Ingestion Processing", {
            "fields": (
                "is_processed",
                "processing_status",
                "last_processed_at",
                "processing_error",
                "processing_history",
            )
        }),
        ("User Auditing", {
            "fields": (
                "created_by",
                "updated_by",
                "deleted_by",
            )
        }),
        ("Timestamps", {
            "fields": (
                "created_at",
                "updated_at",
                "deleted_at",
            )
        }),
    )

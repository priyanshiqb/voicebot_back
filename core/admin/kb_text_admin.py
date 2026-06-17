from django.contrib import admin
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from core.models.knowledge_base import KBText

@admin.register(KBText)
class KBTextAdmin(TenantBaseAdmin):
    """
    Plan-like ModelAdmin interface for KBText blocks.
    Groups fields cleanly into segmented administrative blocks.
    """
    model = KBText

    list_display = (
        "id",
        "title",
        "tenant",
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
        "title",
        "description",
    ) + TenantBaseAdmin.search_fields

    readonly_fields = TenantBaseAdmin.readonly_fields + (
        "is_processed",
        "processing_status",
        "processing_error",
        "etl_id",
        "pending_at",
        "processing_at",
        "completed_at",
        "failed_at",
    )

    ordering = ("-created_at",)

    fieldsets = (
        ("General Details", {
            "fields": (
                "id",
                "tenant",
                "title",
                "is_active",
            )
        }),
        ("Content Description", {
            "fields": (
                "description",
            )
        }),
        ("ETL / Ingestion Processing", {
            "fields": (
                "etl_id",
                "is_processed",
                "processing_status",
                "pending_at",
                "processing_at",
                "completed_at",
                "failed_at",
                "processing_error",
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

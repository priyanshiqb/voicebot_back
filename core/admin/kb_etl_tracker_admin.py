from django.contrib import admin
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from core.models.kb_etl_tracker import KBETLTracker

@admin.register(KBETLTracker)
class KBETLTrackerAdmin(TenantBaseAdmin):
    """
    Dedicated ModelAdmin interface for KBETLTracker pipeline runs.
    Groups execution checklists, snapshots, stats, and failures into segmented blocks.
    """
    model = KBETLTracker

    list_display = (
        "id",
        "kb",
        "tenant",
        "task_id",
        "status",
        "created_at",
        "completed_at",
    )

    list_filter = (
        "status",
    ) + TenantBaseAdmin.list_filter

    search_fields = (
        "task_id",
        "status",
    ) + TenantBaseAdmin.search_fields

    readonly_fields = TenantBaseAdmin.readonly_fields + (
        "kb_json_snapshot",
        "to_process_ids",
        "processed_ids",
        "processing_stats",
        "error_details",
        "completed_at",
    )

    ordering = ("-created_at",)

    fieldsets = (
        ("General Details", {
            "fields": (
                "id",
                "tenant",
                "kb",
                "task_id",
                "status",
            )
        }),
        ("Ingestion Snapshots & Checklists", {
            "fields": (
                "kb_json_snapshot",
                "to_process_ids",
                "processed_ids",
            )
        }),
        ("Execution Stats & Diagnostics", {
            "fields": (
                "processing_stats",
                "error_details",
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
                "completed_at",
                "created_at",
                "updated_at",
                "deleted_at",
            )
        }),
    )

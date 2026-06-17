from django.contrib import admin
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from core.models.knowledge_base import KnowledgeBase

@admin.register(KnowledgeBase)
class KnowledgeBaseAdmin(TenantBaseAdmin):
    """
    Subscription-like ModelAdmin interface for KnowledgeBase aggregate containers.
    Groups fields cleanly into segmented administrative blocks.
    """
    model = KnowledgeBase

    list_display = (
        "id",
        "name",
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
        "name",
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

    # Use clean horizontal filter layout for ManyToMany aggregation relations
    filter_horizontal = (
        "texts",
        "files",
        "websites",
        "question_answers",
    )

    fieldsets = (
        ("General Details", {
            "fields": (
                "id",
                "tenant",
                "name",
                "is_active",
            )
        }),
        ("Aggregated KB Content", {
            "fields": (
                "texts",
                "files",
                "websites",
                "question_answers",
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

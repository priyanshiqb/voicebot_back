from django.contrib import admin
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from core.models.knowledge_base import KBQuestionAnswer

@admin.register(KBQuestionAnswer)
class KBQuestionAnswerAdmin(TenantBaseAdmin):
    """
    Plan-like ModelAdmin interface for KBQuestionAnswer objects.
    Groups fields cleanly into segmented administrative blocks.
    """
    model = KBQuestionAnswer

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
        "answer",
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

    # Use raw_id_fields or filter_horizontal for ManyToMany questions relation to look very clean
    filter_horizontal = ("questions",)

    fieldsets = (
        ("General Details", {
            "fields": (
                "id",
                "tenant",
                "title",
                "is_active",
            )
        }),
        ("Q&A Pair Content", {
            "fields": (
                "questions",
                "answer",
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

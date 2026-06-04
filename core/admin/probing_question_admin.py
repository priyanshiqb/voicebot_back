from django.contrib import admin
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from core.models.probing_question import ProbingQuestion


@admin.register(ProbingQuestion)
class ProbingQuestionAdmin(TenantBaseAdmin):
    """
    Modular Django Admin interface for ProbingQuestion.
    Conforms to multi-tenant scoped filtering and auditing.
    """
    model = ProbingQuestion

    list_display = (
        "id",
        "question",
        "priority",
        "score",
        "is_mandatory",
        "can_user_edit",
        "is_default",
        "tenant",
        "is_active",
    )

    list_filter = (
        "is_mandatory",
        "can_user_edit",
        "is_default",
    ) + TenantBaseAdmin.list_filter

    search_fields = (
        "question",
    ) + TenantBaseAdmin.search_fields

    readonly_fields = TenantBaseAdmin.readonly_fields + (
        "is_deleted",
    )

    ordering = ("priority", "-created_at")

    fieldsets = (
        ("General Details & Linkage", {
            "fields": (
                "id",
                "tenant",
                "question",
                "can_user_edit",
                "is_default",
                "is_active",
                "is_deleted",
            )
        }),
        ("Cognitive Weights", {
            "fields": (
                "priority",
                "score",
                "is_mandatory",
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


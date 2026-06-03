from django.contrib import admin
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from core.models.agent_template import AgentTemplate


@admin.register(AgentTemplate)
class AgentTemplateAdmin(TenantBaseAdmin):
    """
    Modular Django Admin interface for AgentTemplate.
    Conforms to multi-tenant scoped filtering and auditing.
    """
    model = AgentTemplate

    list_display = (
        "id",
        "name",
        "tenant",
        "strict_facts",
        "can_user_edit_flow",
        "is_active",
        "created_at",
    )

    list_filter = (
        "strict_facts",
        "can_user_edit_flow",
    ) + TenantBaseAdmin.list_filter

    search_fields = (
        "name",
        "short_description",
    ) + TenantBaseAdmin.search_fields

    readonly_fields = TenantBaseAdmin.readonly_fields + (
        "is_deleted",
    )

    ordering = ("-created_at",)

    fieldsets = (
        ("General Details", {
            "fields": (
                "id",
                "tenant",
                "name",
                "short_description",
                "is_active",
                "is_deleted",
            )
        }),
        ("Aesthetics", {
            "fields": (
                "visual_icon",
                "primary_accent",
            )
        }),
        ("Cognitive Prompt & Behavior", {
            "fields": (
                "agent_prompt",
                "strict_facts",
                "can_user_edit_flow",
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


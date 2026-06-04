from django.contrib import admin
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from core.models.agent import Agent


@admin.register(Agent)
class AgentAdmin(TenantBaseAdmin):
    """
    Modular Django Admin interface for Agent.
    Conforms to multi-tenant scoped filtering and auditing.
    """
    model = Agent

    list_display = (
        "id",
        "bot_name",
        "persona_name",
        "tone",
        "agent_template",
        "tenant",
        "is_active",
        "created_at",
    )

    list_filter = (
        "tone",
        "agent_template",
    ) + TenantBaseAdmin.list_filter

    search_fields = (
        "bot_name",
        "persona_name",
    ) + TenantBaseAdmin.search_fields

    filter_horizontal = (
        "knowledge_bases",
        "probing_questions",
    )
    raw_id_fields = ("agent_template",)

    readonly_fields = TenantBaseAdmin.readonly_fields + (
        "is_deleted",
    )

    ordering = ("-created_at",)

    fieldsets = (
        ("General Details", {
            "fields": (
                "id",
                "tenant",
                "bot_name",
                "persona_name",
                "is_active",
                "is_deleted",
            )
        }),
        ("AI & Voice", {
            "fields": (
                "languages",
                "tone",
                "agent_template",
                "voice_name",
            )
        }),
        ("Cognitive Context", {
            "fields": (
                "knowledge_bases",
                "probing_questions",
                "transfer_destinations",
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

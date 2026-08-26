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
        "service_tier",
        "agent_template",
        "tenant",
        "is_active",
        "created_at",
    )

    list_filter = (
        "service_tier",
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
        "event_templates",
    )
    raw_id_fields = ("agent_template", "timezone")

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
                "service_tier",
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
        ("Scheduling", {
            "fields": (
                "event_templates",
                "timezone",
            ),
            "description": (
                "Timezone is the clock this agent SPEAKS in — times quoted to and "
                "heard from the caller. Everything stored stays UTC. Blank falls "
                "back to the tenant's base timezone."
            ),
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

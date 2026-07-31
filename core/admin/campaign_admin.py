from django.contrib import admin

from core.models.campaign import Campaign
from tenant.common.admin.tenant_base_admin import TenantBaseAdmin


@admin.register(Campaign)
class CampaignAdmin(TenantBaseAdmin):
    model = Campaign

    list_display = (
        "id",
        "name",
        "tenant",
        "agent",
        "status",
        "scheduled_start_at",
        "total_members",
        "pending_count",
        "suppressed_count",
        "is_active",
        "created_at",
    )
    list_filter = (
        "status",
        "audience_source",
        "handoff_status",
    ) + TenantBaseAdmin.list_filter
    search_fields = ("name", "description", "agent__bot_name")
    ordering = ("-created_at",)
    filter_horizontal = ("contacts",)
    autocomplete_fields = TenantBaseAdmin.autocomplete_fields + ("agent",)

    # Every one of these is derived by the campaign lifecycle. Hand-editing status or a
    # counter desynchronises the campaign from its members; hand-editing the handoff
    # fields desynchronises it from the runner.
    readonly_fields = TenantBaseAdmin.readonly_fields + (
        "status",
        "scheduled_start_at",
        "schedule_timezone",
        "started_at",
        "completed_at",
        "launched_by",
        "paused_by",
        "cancelled_by",
        "handoff_id",
        "handoff_status",
        "handed_off_at",
        "total_members",
        "pending_count",
        "suppressed_count",
        "completed_count",
        "failed_count",
        "error_message",
    )

    fieldsets = (
        (
            "Definition",
            {"fields": ("name", "description", "agent", "is_active")},
        ),
        (
            "Audience",
            {"fields": ("audience_source", "contacts", "audience_filter")},
        ),
        (
            "Schedule",
            {
                "fields": (
                    "scheduled_start_at",
                    "schedule_timezone",
                    "started_at",
                    "completed_at",
                )
            },
        ),
        (
            "Handoff",
            {"fields": ("handoff_status", "handoff_id", "handed_off_at")},
        ),
        (
            "Progress",
            {
                "fields": (
                    "status",
                    "total_members",
                    "queued_count",
                    "suppressed_count",
                    "completed_count",
                    "failed_count",
                    "error_message",
                )
            },
        ),
        (
            "Attribution",
            {"fields": ("launched_by", "paused_by", "cancelled_by")},
        ),
    )

from django.contrib import admin

from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from core.models.calendar_integration import CalendarIntegration


@admin.register(CalendarIntegration)
class CalendarIntegrationAdmin(TenantBaseAdmin):
    model = CalendarIntegration

    list_display = (
        "id",
        "user",
        "tenant",
        "provider",
        "account_email",
        "sync_status",
        "token_expires_at",
        "created_at",
    )
    list_filter = ("provider", "sync_status") + TenantBaseAdmin.list_filter
    search_fields = ("account_email",) + TenantBaseAdmin.search_fields
    ordering = ("-created_at",)
    # Never surface encrypted token material in the admin form.
    exclude = ("access_token", "refresh_token", "sync_token")
    readonly_fields = TenantBaseAdmin.readonly_fields + (
        "account_email",
        "token_expires_at",
        "watch_channel_id",
        "watch_resource_id",
        "watch_expires_at",
        "last_synced_at",
        "last_error",
    )

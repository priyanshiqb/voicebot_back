from django.contrib import admin

from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore
from core.models.notification_log import NotificationLog


@admin.register(NotificationLog)
class NotificationLogAdmin(TenantBaseAdminCore):
    model = NotificationLog

    list_display = (
        "id",
        "notification_type",
        "channel",
        "status",
        "recipient",
        "booking",
        "sent_at",
        "created_at",
    )
    list_filter = ("channel", "notification_type", "status") + TenantBaseAdminCore.list_filter
    search_fields = ("recipient", "dedup_key")
    ordering = ("-created_at",)
    readonly_fields = TenantBaseAdminCore.readonly_fields + (
        "dedup_key",
        "booking",
        "recipient",
        "sent_at",
        "error",
    )

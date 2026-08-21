from django.contrib import admin
from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore
from core.models.in_app_notification import InAppNotification


class InAppNotificationAdmin(TenantBaseAdminCore):
    """
    Tier 1 Core Admin for InAppNotification.
    """
    
    list_display = (
        "id",
        "title",
        "user",
        "notification_type",
        "is_read",
        "read_at",
        "created_at",
    )
    
    list_filter = (
        "is_read",
        "notification_type",
    )
    
    search_fields = (
        "id",
        "title",
        "body",
        "user__email",
        "user__username",
    )
    
    readonly_fields = (
        "created_at",
        "updated_at",
        "read_at",
    )
    
    fieldsets = (
        (
            "Notification Details",
            {
                "fields": (
                    "user",
                    "title",
                    "body",
                    "notification_type",
                )
            },
        ),
        (
            "Read Status",
            {
                "fields": (
                    "is_read",
                    "read_at",
                )
            },
        ),
        (
            "Audit & Context",
            {
                "fields": (
                    "tenant",
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
    )


admin.site.register(InAppNotification, InAppNotificationAdmin)

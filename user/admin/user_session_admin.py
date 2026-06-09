from django.contrib import admin
from utils.admin.base_admin_core import BaseAdminCore
from user.models.user_session import UserSession

@admin.register(UserSession)
class UserSessionAdmin(BaseAdminCore):
    list_display = (
        "id",
        "user",
        "device_name",
        "ip_address",
        "is_active",
        "expires_at",
        "created_at",
    )

    search_fields = BaseAdminCore.search_fields + (
        "user__email",
        "device_name",
        "ip_address",
    )

    list_filter = BaseAdminCore.list_filter + (
        "expires_at",
    )

    readonly_fields = BaseAdminCore.readonly_fields + (
        "refresh_jti",
        "access_jti",
    )

    fieldsets = (
        (
            "General Info",
            {
                "fields": (
                    "user",
                    "device_name",
                    "ip_address",
                )
            },
        ),
        (
            "JWT Tokens & Status",
            {
                "fields": (
                    "refresh_jti",
                    "access_jti",
                    "is_active",
                    "expires_at",
                )
            },
        ),
        (
            "Auditing & Timestamps",
            {
                "fields": (
                    "id",
                    "created_at",
                    "updated_at",
                    "deleted_at",
                )
            },
        ),
    )

from django.contrib import admin

from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore
from core.models.device_token import DeviceToken


@admin.register(DeviceToken)
class DeviceTokenAdmin(TenantBaseAdminCore):
    model = DeviceToken

    list_display = (
        "id",
        "user",
        "tenant",
        "platform",
        "is_active",
        "last_seen_at",
        "created_at",
    )
    list_filter = ("platform",) + TenantBaseAdminCore.list_filter
    search_fields = ("token",)
    ordering = ("-created_at",)

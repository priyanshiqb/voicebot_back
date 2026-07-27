from django.contrib import admin

from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore
from core.models.user_event_skill import UserEventSkill


@admin.register(UserEventSkill)
class UserEventSkillAdmin(TenantBaseAdminCore):
    model = UserEventSkill

    list_display = (
        "id",
        "user",
        "event_template",
        "tenant",
        "weight",
        "is_active",
        "created_at",
    )
    list_filter = TenantBaseAdminCore.list_filter
    search_fields = TenantBaseAdminCore.search_fields
    ordering = ("-created_at",)

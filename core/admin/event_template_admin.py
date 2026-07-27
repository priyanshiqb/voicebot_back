from django.contrib import admin

from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from core.models.event_template import EventTemplate


@admin.register(EventTemplate)
class EventTemplateAdmin(TenantBaseAdmin):
    model = EventTemplate

    list_display = (
        "id",
        "name",
        "tenant",
        "duration_minutes",
        "assignment_strategy",
        "location_type",
        "is_active",
        "created_at",
    )
    list_filter = ("assignment_strategy", "location_type") + TenantBaseAdmin.list_filter
    search_fields = ("name", "description") + TenantBaseAdmin.search_fields
    ordering = ("-created_at",)

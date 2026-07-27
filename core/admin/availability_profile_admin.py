from django.contrib import admin

from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from core.models.availability_profile import AvailabilityProfile
from core.models.availability_rule import AvailabilityRule


class AvailabilityRuleInline(admin.TabularInline):
    """Read-only view of a profile's weekly rules (edit via the API/serializer,
    which injects tenant correctly)."""

    model = AvailabilityRule
    extra = 0
    can_delete = False
    fields = ("weekday", "start_time", "end_time", "is_active")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(AvailabilityProfile)
class AvailabilityProfileAdmin(TenantBaseAdmin):
    model = AvailabilityProfile

    list_display = ("id", "name", "user", "tenant", "timezone", "is_active", "created_at")
    list_filter = TenantBaseAdmin.list_filter
    search_fields = ("name",) + TenantBaseAdmin.search_fields
    ordering = ("-created_at",)
    inlines = [AvailabilityRuleInline]

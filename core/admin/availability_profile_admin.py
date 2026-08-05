from django.contrib import admin

from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore
from core.models.availability_profile import AvailabilityProfile
from core.models.availability_rule import AvailabilityRule
from core.models.availability_window import AvailabilityWindow


class AvailabilityRuleInline(admin.TabularInline):
    """Read-only view of a profile's weekly rules (edit via the API/serializer,
    which injects tenant correctly)."""

    model = AvailabilityRule
    extra = 0
    can_delete = False
    fields = ("weekday", "start_time", "end_time", "wraps_midnight", "is_active")
    readonly_fields = fields

    @admin.display(boolean=True, description="Ends next day")
    def wraps_midnight(self, obj):
        return obj.wraps_midnight

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


@admin.register(AvailabilityWindow)
class AvailabilityWindowAdmin(TenantBaseAdminCore):
    """Read-only. Windows are derived data — rebuild them with
    `manage.py materialize_availability`, never by hand.

    Worth having in admin anyway: it is where a support question like "why was
    this shift an hour short in March?" gets answered, via dst_flag.
    """

    model = AvailabilityWindow

    list_display = ("user", "local_date", "start_utc", "end_utc", "dst_flag", "tenant")
    list_filter = ("dst_flag",) + TenantBaseAdminCore.list_filter
    ordering = ("-start_utc",)
    date_hierarchy = "local_date"
    readonly_fields = TenantBaseAdminCore.readonly_fields + (
        "profile",
        "rule",
        "user",
        "local_date",
        "start_utc",
        "end_utc",
        "dst_flag",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

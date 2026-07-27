from django.contrib import admin

from tenant.common.admin.tenant_base_admin import TenantBaseAdmin
from core.models.availability_exception import AvailabilityException


@admin.register(AvailabilityException)
class AvailabilityExceptionAdmin(TenantBaseAdmin):
    model = AvailabilityException

    list_display = (
        "id",
        "user",
        "tenant",
        "type",
        "start_time",
        "end_time",
        "is_all_day",
        "created_at",
    )
    list_filter = ("type",) + TenantBaseAdmin.list_filter
    search_fields = ("reason",) + TenantBaseAdmin.search_fields
    ordering = ("-start_time",)

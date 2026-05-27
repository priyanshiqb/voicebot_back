from django.contrib import admin
from utils.admin.base_admin import BaseAdmin
from tenant.models.TenantUserMapping import TenantUserMapping


@admin.register(TenantUserMapping)
class TenantUserMappingAdmin(BaseAdmin):
    """
    Administrative display dashboard for Tenant User Mapping resources.
    Uses BaseAdmin for soft-delete hooks, search fields, and dynamic trace details.
    """
    list_display = ("user", "tenant", "is_active", "is_deleted", "created_at")
    list_filter = ("is_active", "is_deleted", "created_at")
    search_fields = ("user__username", "user__email", "tenant__name")

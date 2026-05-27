from django.contrib import admin
from utils.admin.base_admin_core import BaseAdminCore
from tenant.models.Tenant import Tenant


@admin.register(Tenant)
class TenantAdmin(BaseAdminCore):
    """
    Administrative display dashboard for Tenant resources.
    Uses BaseAdminCore for soft-delete hooks, search fields, and unified logs views.
    """
    list_display = ("name", "subdomain", "is_active", "is_deleted", "created_at")
    list_filter = ("is_active", "is_deleted", "created_at")
    search_fields = ("name", "subdomain", "id")

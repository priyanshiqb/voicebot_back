from django.contrib import admin
from django.core.exceptions import PermissionDenied
from utils.admin.base_admin_core import BaseAdminCore


class TenantBaseAdminCore(BaseAdminCore):
    """
    Core Tenant-Scoped Base Admin.
    Extends BaseAdminCore to implement automated row-level data isolation,
    filtering all querysets and auto-assigning tenant contexts on save.
    """

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs

        try:
            active_tenant = request.user.tenant_mapping.tenant
            return qs.filter(tenant=active_tenant)
        except Exception:
            return qs.none()

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            if "tenant" not in readonly:
                readonly.append("tenant")
        return readonly

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            try:
                active_tenant = request.user.tenant_mapping.tenant
                if change and obj.tenant != active_tenant:
                    raise PermissionDenied("You do not have permissions to modify another tenant's records.")
                obj.tenant = active_tenant
            except Exception:
                raise PermissionDenied("User profile has no active tenant mapped. Record cannot be saved.")

        super().save_model(request, obj, form, change)

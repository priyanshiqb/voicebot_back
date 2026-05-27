from utils.admin.base_admin import BaseAdmin
from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore


class TenantBaseAdmin(TenantBaseAdminCore, BaseAdmin):
    """
    Audited Tenant-Scoped Base Admin.
    Combines TenantBaseAdminCore's row-level tenancy constraints with BaseAdmin's
    audited user tracking mechanisms to avoid code duplication.
    """
    # Merge read-only fields cleanly from both parent classes
    readonly_fields = tuple(set(BaseAdmin.readonly_fields + TenantBaseAdminCore.readonly_fields))
    autocomplete_fields = BaseAdmin.autocomplete_fields

from tenant.serializers.tenant import TenantRefSerializer

__all__ = [
    "TenantRefSerializer",
]

# Deliberately NOT re-exported here: TenantEmailConfigurationSerializer and its
# siblings. `tenant.common` imports `tenant.serializers.tenant`, so anything in
# this __init__ that imports back from `tenant.common` closes a cycle — and the
# model layer reaches TenantBaseModel through `tenant.common`, so the failure lands
# during app loading rather than anywhere obvious.
#
# Import them by module path instead, exactly as the core app does with its own:
#     from tenant.serializers.tenant_email_configuration import (
#         TenantEmailConfigurationSerializer,
#     )

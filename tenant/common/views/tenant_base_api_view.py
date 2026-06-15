import logging
from rest_framework.exceptions import PermissionDenied
from utils.views.base_api_view import BaseAPIView

logger = logging.getLogger(__name__)


class TenantBaseAPIView(BaseAPIView):
    """
    Tenant-scoped Base APIView.
    Provides utility methods to fetch and validate the tenant context of the logged-in user.
    """

    def get_tenant(self):
        """
        Retrieves the active tenant associated with the request user.
        Raises PermissionDenied if the user account is not associated with any active tenant.
        For superusers, returns None if no mapping exists, allowing global bypass.
        """
        user = self.request.user

        try:
            return user.tenant_mapping.tenant
        except Exception as e:
            if user.is_superuser:
                return None
            logger.warning(
                f"User {user.id} attempted to access tenant-scoped view but has no active mapping: {str(e)}"
            )
            raise PermissionDenied("Your user account is not associated with any active tenant.")

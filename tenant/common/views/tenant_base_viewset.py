from utils.views.base_model_viewset import BaseViewSet
from rest_framework.exceptions import PermissionDenied


class TenantBaseViewSet(BaseViewSet):
    """
    Tenant-scoped Base ViewSet.
    Automatically filters all querysets to the logged-in user's mapped tenant,
    and auto-assigns the tenant context to new records during creation.
    """

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user

        # Django Superusers can bypass filters to view all records globally
        if user.is_superuser:
            return queryset

        try:
            # Enforce tenant isolation based on user's active tenant mapping
            active_tenant = user.tenant_mapping.tenant
            return queryset.filter(tenant=active_tenant)
        except Exception:
            raise PermissionDenied("Your user account is not associated with any active tenant.")

    def perform_create(self, serializer):
        user = self.request.user

        if user.is_superuser:
            super().perform_create(serializer)
            return

        try:
            active_tenant = user.tenant_mapping.tenant
        except Exception:
            raise PermissionDenied("Your user account is not associated with any active tenant.")

        model = serializer.Meta.model
        data = {}
        if hasattr(model, "tenant"):
            data["tenant"] = active_tenant

        # Save auto-injects tenant, and lets parent perform_create handle created_by/updated_by auditing
        serializer.save(**data)

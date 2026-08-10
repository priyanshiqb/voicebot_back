from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.availability_profile import AvailabilityProfile
from core.serializers.availability_profile import (
    AvailabilityProfileDropdownSerializer,
    AvailabilityProfileListSerializer,
    AvailabilityProfileSerializer,
)

from drf_spectacular.utils import extend_schema

@extend_schema(tags=["Availability Profile"])
class AvailabilityProfileViewSet(TenantBaseViewSet):
    """CRUD for per-user weekly working hours (with nested rules), tenant-scoped."""

    queryset = AvailabilityProfile.objects.all()
    serializer_class = AvailabilityProfileSerializer
    list_serializer_class = AvailabilityProfileListSerializer
    dropdown_serializer_class = AvailabilityProfileDropdownSerializer

    filterset_fields = ["user", "is_active", "timezone"]
    search_fields = [
        "name",
        "user__email",
        "user__first_name",
        "user__last_name",
        "timezone__timezone",
        "timezone__region",
    ]
    ordering_fields = ["created_at", "updated_at", "name", "timezone__timezone"]
    ordering = ["-created_at"]
    select_related_fields = ["timezone", "user"]
    prefetch_related_fields = ["rules"]

    # No perform_destroy override: AvailabilityProfile.soft_delete() purges the
    # profile's materialized windows itself, so bulk_delete — which calls
    # soft_delete directly and never reaches this viewset method — is covered too.

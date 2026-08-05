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

    filterset_fields = ["user", "is_active"]
    ordering_fields = ["created_at", "updated_at"]
    ordering = ["-created_at"]
    select_related_fields = ["timezone", "user"]
    prefetch_related_fields = ["rules"]

    def perform_destroy(self, instance):
        """Soft-delete the profile, then drop its materialized windows.

        Windows are derived rows, so they are hard-deleted rather than soft-
        deleted. Skipping this would leave the host bookable from a deleted
        profile until the next beat run.
        """
        from core.services import availability_materializer as materializer

        super().perform_destroy(instance)
        materializer.purge_profile(instance)

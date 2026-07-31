from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.availability_exception import AvailabilityException
from core.serializers.availability_exception import (
    AvailabilityExceptionDropdownSerializer,
    AvailabilityExceptionListSerializer,
    AvailabilityExceptionSerializer,
)

from drf_spectacular.utils import extend_schema

@extend_schema(tags=["Availability Exception"])
class AvailabilityExceptionViewSet(TenantBaseViewSet):
    """
    CRUD for time-off / leave / emergency windows, tenant-scoped. Creating one
    fires the reschedule workflow (post_save signal -> reschedule task) for any
    of that user's overlapping bookings.
    """

    queryset = AvailabilityException.objects.all()
    serializer_class = AvailabilityExceptionSerializer
    list_serializer_class = AvailabilityExceptionListSerializer
    dropdown_serializer_class = AvailabilityExceptionDropdownSerializer

    filterset_fields = ["user", "type", "is_active"]
    ordering_fields = ["start_time", "end_time", "created_at"]
    ordering = ["-start_time"]
    select_related_fields = ["user"]

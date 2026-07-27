from rest_framework.exceptions import ValidationError

from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.booking import Booking
from core.models.event_template import EventTemplate
from core.serializers.event_template import (
    EventTemplateDropdownSerializer,
    EventTemplateListSerializer,
    EventTemplateSerializer,
)


class EventTemplateViewSet(TenantBaseViewSet):
    """CRUD for bookable event types, tenant-scoped."""

    queryset = EventTemplate.objects.all()
    serializer_class = EventTemplateSerializer
    list_serializer_class = EventTemplateListSerializer
    dropdown_serializer_class = EventTemplateDropdownSerializer

    filterset_fields = ["is_active", "assignment_strategy", "location_type"]
    search_fields = ["name", "description"]
    ordering_fields = ["name", "created_at", "updated_at", "duration_minutes"]
    ordering = ["-created_at"]

    def perform_destroy(self, instance):
        active = Booking.objects.filter(
            event_template=instance,
            is_deleted=False,
            status__in=Booking.ACTIVE_STATUSES,
        ).exists()
        if active:
            raise ValidationError(
                {"detail": "This event type has active bookings and cannot be deleted."}
            )
        super().perform_destroy(instance)

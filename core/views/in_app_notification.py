from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from tenant.common.views.tenant_base_viewset import TenantBaseViewSet
from core.models.in_app_notification import InAppNotification
from core.serializers.in_app_notification import (
    InAppNotificationSerializer,
    InAppNotificationListSerializer,
    InAppNotificationDropdownSerializer,
)


class InAppNotificationViewSet(TenantBaseViewSet):
    """
    API endpoint that allows InAppNotifications to be viewed or edited.
    Automatically scoped to the requesting user.
    """

    queryset = InAppNotification.objects.all()
    serializer_class = InAppNotificationSerializer
    list_serializer_class = InAppNotificationListSerializer
    dropdown_serializer_class = InAppNotificationDropdownSerializer

    filterset_fields = ["is_read", "notification_type"]
    search_fields = ["title", "body"]
    ordering_fields = ["created_at", "read_at"]
    ordering = ["-created_at"]
    select_related_fields = ["tenant", "user"]
    prefetch_related_fields = []

    def get_queryset(self):
        """
        Filter to only show the current user's notifications, along with the standard
        TenantBaseViewSet scoping.
        """
        qs = super().get_queryset()
        if self.request and self.request.user and self.request.user.is_authenticated:
            qs = qs.filter(user=self.request.user)
        else:
            qs = qs.none()
        return qs

    @action(detail=True, methods=["post"], url_path="mark-read")
    def mark_read(self, request, pk=None):
        """Mark a specific notification as read."""
        notification = self.get_object()
        if not notification.is_read:
            notification.is_read = True
            notification.read_at = timezone.now()
            notification.save(update_fields=["is_read", "read_at", "updated_at"])
            
        serializer = self.get_serializer(notification)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="mark-all-read")
    def mark_all_read(self, request):
        """Mark all unread notifications for the current user as read."""
        unread_qs = self.get_queryset().filter(is_read=False)
        updated_count = unread_qs.update(
            is_read=True, 
            read_at=timezone.now(),
            updated_at=timezone.now()
        )
        return Response({"updated_count": updated_count}, status=status.HTTP_200_OK)

from django.conf import settings
from django.db import models

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore
from core.models.notification_log import NotificationLog


class InAppNotification(TenantBaseModelCore):
    """
    In-app notification meant to be displayed in a user's dashboard or notification center.
    Unlike NotificationLog (which tracks outbound external pushes and emails), this model
    is mapped strictly per-user and tracks read state.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="in_app_notifications",
        help_text="The user this notification belongs to.",
    )
    title = models.CharField(
        max_length=255,
        help_text="Short title of the notification.",
    )
    body = models.TextField(
        help_text="Full text content of the notification.",
    )
    notification_type = models.CharField(
        max_length=32,
        choices=NotificationLog.NotificationType.choices,
        help_text="Logical type of the notification (maps to NotificationLog types).",
    )
    is_read = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Whether the user has read this notification.",
    )
    read_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the user marked this notification as read.",
    )

    class Meta:
        db_table = "core_in_app_notifications"
        verbose_name = "In-App Notification"
        verbose_name_plural = "In-App Notifications"
        ordering = ["-created_at"]

    def __str__(self):
        read_status = "Read" if self.is_read else "Unread"
        return f"{self.title} - {self.user} ({read_status})"

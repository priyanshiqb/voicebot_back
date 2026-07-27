from django.conf import settings
from django.db import models

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


class DeviceToken(TenantBaseModelCore):
    """
    An FCM registration token for a user's device, used for push notifications
    (internal-team alerts). Tier 1 Core. Deactivated (is_active=False) when FCM
    reports the token is unregistered/invalid.
    """

    class Platform(models.TextChoices):
        ANDROID = "ANDROID", "Android"
        IOS = "IOS", "iOS"
        WEB = "WEB", "Web"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="device_tokens",
        help_text="Owner of the device.",
    )
    token = models.CharField(max_length=512, help_text="FCM registration token.")
    platform = models.CharField(
        max_length=10, choices=Platform.choices, default=Platform.WEB
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "core_device_tokens"
        verbose_name = "Device Token"
        verbose_name_plural = "Device Tokens"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "token"],
                condition=models.Q(is_deleted=False),
                name="unique_active_device_token",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} {self.platform} ({'active' if self.is_active else 'inactive'})"

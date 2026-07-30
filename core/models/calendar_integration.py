from django.conf import settings
from django.db import models

from tenant.common.models.tenant_base_model import TenantBaseModel


class CalendarIntegration(TenantBaseModel):
    """
    A user's OAuth connection to an external calendar. Tier 2 (audited — who
    connected an account matters). Access/refresh tokens are stored ENCRYPTED
    and are never exposed by any serializer.
    """

    class Provider(models.TextChoices):
        GOOGLE = "GOOGLE", "Google"

    class SyncStatus(models.TextChoices):
        CONNECTED = "CONNECTED", "Connected"
        REAUTH_REQUIRED = "REAUTH_REQUIRED", "Re-auth Required"
        ERROR = "ERROR", "Error"
        DISCONNECTED = "DISCONNECTED", "Disconnected"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="calendar_integrations",
        help_text="The user whose external calendar this connects.",
    )
    provider = models.CharField(
        max_length=20, choices=Provider.choices, default=Provider.GOOGLE
    )
    account_email = models.CharField(
        max_length=254, blank=True, default="", help_text="Connected account identity."
    )
    access_token = models.TextField(blank=True, default="", help_text="Encrypted access token.")
    refresh_token = models.TextField(blank=True, default="", help_text="Encrypted refresh token.")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    scope = models.TextField(blank=True, default="", help_text="Granted OAuth scopes.")
    sync_status = models.CharField(
        max_length=20,
        choices=SyncStatus.choices,
        default=SyncStatus.CONNECTED,
        db_index=True,
    )
    sync_token = models.TextField(blank=True, default="", help_text="Provider incremental-sync cursor.")
    watch_channel_id = models.CharField(max_length=255, blank=True, null=True)
    watch_resource_id = models.CharField(max_length=255, blank=True, null=True)
    watch_token = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=(
            "Per-channel secret validated on webhook delivery — Google's "
            "X-Goog-Channel-Token."
        ),
    )
    watch_expires_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        db_table = "core_calendar_integrations"
        verbose_name = "Calendar Integration"
        verbose_name_plural = "Calendar Integrations"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "user"],
                condition=models.Q(is_deleted=False),
                name="unique_active_calendar_integration",
            ),
        ]

    # --- encrypted token accessors -------------------------------------------

    def set_tokens(self, access_token=None, refresh_token=None):
        from core.services import crypto

        if access_token is not None:
            self.access_token = crypto.encrypt(access_token)
        if refresh_token is not None:
            self.refresh_token = crypto.encrypt(refresh_token)

    def get_access_token(self):
        from core.services import crypto

        return crypto.decrypt(self.access_token)

    def get_refresh_token(self):
        from core.services import crypto

        return crypto.decrypt(self.refresh_token)

    def __str__(self):
        return f"{self.provider}:{self.account_email or self.user_id} ({self.sync_status})"

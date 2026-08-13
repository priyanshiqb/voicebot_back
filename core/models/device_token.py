from django.conf import settings
from django.db import models

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


class DeviceToken(TenantBaseModelCore):
    """
    An FCM registration token for a user's device, used for push notifications
    (internal-team alerts). Tier 1 Core.

    **A token is a device, not a user and not a login.** One user has many; one
    device can change hands, which is why re-registration reassigns ``user``.

    ``is_active`` is switched off from several places: an explicit ``deactivate``
    call, a logout naming this device, FCM disowning the token on a send, the
    staleness sweep, the per-user cap, and the death of the session it registered
    under. ``last_error`` records which, because "inactive, and nobody knows why" is
    an unanswerable support question.

    Liveness is **not** knowable from the token itself — it carries no expiry and
    cannot be validated offline. See ``core.services.device_tokens`` for the
    staleness and retention model, and ``docs/flows/push-notifications.md`` for the
    end-to-end lifecycle.
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
    token = models.CharField(
        max_length=512,
        db_index=True,
        help_text="FCM registration token.",
    )
    platform = models.CharField(
        max_length=10, choices=Platform.choices, default=Platform.WEB
    )
    session = models.ForeignKey(
        "user.UserSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="device_tokens",
        help_text=(
            "The login session this device registered under. Set on registration and "
            "moved on re-registration, so it always names the newest login on this "
            "device. When that session dies — logout, expiry, or a remote revoke — the "
            "device stops receiving pushes without needing the client to tell us, which "
            "matters because a client whose refresh token expired has no credentials "
            "left to call any endpoint with."
        ),
    )
    last_seen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the client last re-registered this token. This is 'last "
            "registered', not 'last active' — nothing else moves it, so it is only "
            "meaningful if clients re-register on every app start. Staleness is "
            "measured from here, falling back to created_at when null."
        ),
    )
    failure_count = models.PositiveSmallIntegerField(
        default=0,
        help_text=(
            "Consecutive sends that failed in a way that might be the token's fault "
            "(INVALID_ARGUMENT). Reset to 0 on any success or re-registration. At "
            "DEVICE_TOKEN_FAILURE_STRIKES the token is retired. Errors that are "
            "definitely not the token's fault (quota, outage, APNs credentials) do "
            "not count."
        ),
    )
    last_error = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Why this token was last deactivated or failed. Cleared on re-registration.",
    )

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
        indexes = [
            # The send fan-out: tokens for one user, or for one tenant's admins.
            models.Index(
                fields=["tenant", "user", "is_active"],
                name="device_token_fanout_idx",
            ),
            # The staleness sweep and the retention purge, both of which scan by
            # age across every tenant.
            models.Index(
                fields=["is_active", "last_seen_at"],
                name="device_token_sweep_idx",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} {self.platform} ({'active' if self.is_active else 'inactive'})"

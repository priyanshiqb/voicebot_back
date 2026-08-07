from django.db import models

from tenant.common.models.tenant_base_model_core import TenantBaseModelCore


class NotificationLog(TenantBaseModelCore):
    """
    Idempotency ledger for outbound notifications. A unique ``dedup_key`` per
    tenant guarantees a given logical notification is sent at most once, even
    across Celery retries or duplicate service calls. Tier 1 Core.

    **These rows are the idempotency mechanism, not merely a record of it.**
    Deleting one re-arms the notification it represents, which is why the retention
    task (``core.tasks.purge_notification_logs``) never touches a row belonging to a
    booking that has not happened yet.

    Rows hold customer email addresses and are never pruned by any other path, so
    retention is a privacy obligation as much as a disk one. Message bodies are
    deliberately **not** stored: they add nothing a ``notification_type`` does not
    already say, and for authentication mail they would be a credential store
    nobody meant to build.
    """

    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        FCM = "FCM", "FCM"

    class NotificationType(models.TextChoices):
        CONFIRMATION = "CONFIRMATION", "Confirmation"
        REMINDER = "REMINDER", "Reminder"
        RESCHEDULE_REQUEST = "RESCHEDULE_REQUEST", "Reschedule Request"
        # Legacy: no longer written. Kept so historical rows still render.
        REASSIGNED = "REASSIGNED", "Reassigned"
        CANCELED = "CANCELED", "Canceled"
        RESCHEDULED = "RESCHEDULED", "Rescheduled"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"

    booking = models.ForeignKey(
        "core.Booking",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
        help_text="Booking this notification relates to (null for non-booking notifications).",
    )
    channel = models.CharField(
        max_length=10,
        choices=Channel.choices,
        default=Channel.EMAIL,
        help_text="Delivery channel.",
    )
    notification_type = models.CharField(
        max_length=32,
        choices=NotificationType.choices,
        help_text="Logical type of the notification.",
    )
    recipient = models.TextField(help_text="Recipient address(es) for this notification.")
    dedup_key = models.CharField(
        max_length=255,
        help_text="Idempotency key; unique per tenant. Prevents duplicate sends.",
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        help_text="Delivery status.",
    )
    sent_at = models.DateTimeField(null=True, blank=True, help_text="When the send completed.")
    error = models.TextField(blank=True, default="", help_text="Last failure detail, if any.")
    email_configuration = models.ForeignKey(
        "tenant.TenantEmailConfiguration",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notification_logs",
        help_text=(
            "Which SMTP configuration carried this message. Null for FCM, for "
            "messages sent before this was recorded, and for messages that were "
            "never sent because no sender was configured."
        ),
    )

    class Meta:
        db_table = "core_notification_logs"
        verbose_name = "Notification Log"
        verbose_name_plural = "Notification Logs"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "dedup_key"],
                name="unique_notification_dedup_key",
            ),
        ]

    def __str__(self):
        return f"{self.notification_type} → {self.recipient} ({self.status})"

from django.contrib import admin

from tenant.common.admin.tenant_base_admin_core import TenantBaseAdminCore
from core.models.notification_log import NotificationLog


@admin.register(NotificationLog)
class NotificationLogAdmin(TenantBaseAdminCore):
    """
    Tenant-to-customer notification ledger.

    Booking email only sends when the tenant has assigned a verified SMTP
    configuration, so ``FAILED`` rows carrying *"no booking sender"* are the normal
    state for a tenant that has not set one up — not an incident. ``sender`` is on
    the changelist because "which configuration carried this" is the first question
    asked when mail stops, and the answer is frequently *none*.

    **Deleting a row here re-arms the notification it represents.** ``dedup_key``
    is the idempotency mechanism, not a record of one, which is why
    ``core.tasks.purge_notification_logs`` refuses to touch a row whose booking has
    not happened yet. The same caution applies to deleting by hand.
    """

    model = NotificationLog

    list_display = (
        "id",
        "notification_type",
        "channel",
        "status",
        "recipient",
        "booking",
        "sender",
        "sent_at",
        "created_at",
    )
    list_filter = (
        "channel",
        "notification_type",
        "status",
    ) + TenantBaseAdminCore.list_filter
    search_fields = ("recipient", "dedup_key")
    ordering = ("-created_at",)
    # The changelist renders a booking and a configuration per row; without this
    # each one is its own query.
    list_select_related = ("booking", "email_configuration")
    # Both tables grow without bound, so neither belongs in a <select>.
    raw_id_fields = ("booking", "email_configuration")
    readonly_fields = TenantBaseAdminCore.readonly_fields + (
        "dedup_key",
        "booking",
        "recipient",
        "sent_at",
        "error",
        # System-written, and the record of which credentials were actually used.
        # Editable, it would be a note about the past rather than evidence of it.
        "email_configuration",
    )

    @admin.display(description="Sender", ordering="email_configuration__name")
    def sender(self, obj):
        """The configuration that carried this, or why there wasn't one.

        Null is not one condition but three, and they need different responses:
        FCM never has a sender, older rows predate the column, and a message that
        was never sent has none because the tenant assigned no booking sender.
        Only the last is actionable, so it is the one spelled out.
        """
        if obj.email_configuration_id is not None:
            return obj.email_configuration.name or obj.email_configuration.from_email
        if obj.channel != NotificationLog.Channel.EMAIL:
            return "—"
        if obj.status == NotificationLog.Status.FAILED:
            return "no sender configured"
        return "—"

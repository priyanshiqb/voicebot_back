from django.contrib import admin
from onboard.models.webhook_log import RazorpayWebhookLog
from utils.admin.base_admin_core import BaseAdminCore


@admin.register(RazorpayWebhookLog)
class RazorpayWebhookLogAdmin(BaseAdminCore):
    """
    RazorpayWebhookLog ModelAdmin interface configuration.
    Registers RazorpayWebhookLog model inheriting from BaseAdminCore.
    """
    model = RazorpayWebhookLog

    list_display = (
        "id",
        "event_id",
        "event_type",
        "is_signature_valid",
        "is_active",
        "created_at",
    )

    list_filter = (
        "event_type",
        "is_signature_valid",
    ) + BaseAdminCore.list_filter

    search_fields = (
        "event_id",
        "event_type",
        "error_message",
    ) + BaseAdminCore.search_fields

    fieldsets = (
        ("Webhook Meta", {
            "fields": (
                "id",
                "event_id",
                "event_type",
            )
        }),
        ("Security & Auditing", {
            "fields": (
                "signature",
                "is_signature_valid",
                "error_message",
            )
        }),
        ("Raw Payload", {
            "fields": (
                "payload",
            ),
            "classes": ("collapse",),
        }),
        ("Timestamps", {
            "fields": (
                "created_at",
                "updated_at",
                "deleted_at",
            )
        }),
    )

from django.contrib import admin
from onboard.models.transaction import Transaction
from utils.admin.base_admin_core import BaseAdminCore


@admin.register(Transaction)
class TransactionAdmin(BaseAdminCore):
    """
    Transaction ModelAdmin interface configuration.
    Registers Transaction model inheriting from BaseAdminCore.
    """
    model = Transaction

    list_display = (
        "id",
        "razorpay_order_id",
        "razorpay_payment_id",
        "amount",
        "status",
        "temp_user",
        "user",
        "plan",
        "is_active",
        "created_at",
    )

    list_filter = (
        "status",
        "plan",
    ) + BaseAdminCore.list_filter

    search_fields = (
        "razorpay_order_id",
        "razorpay_payment_id",
        "amount",
    ) + BaseAdminCore.search_fields

    fieldsets = (
        ("Associations", {
            "fields": (
                "id",
                "temp_user",
                "user",
                "plan",
            )
        }),
        ("Razorpay Integration", {
            "fields": (
                "razorpay_order_id",
                "razorpay_payment_id",
                "amount",
                "status",
            )
        }),
        ("Webhook Audit Payload", {
            "fields": (
                "raw_webhook_payload",
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

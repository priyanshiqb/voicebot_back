from django.db import models
from utils.models.base_model_core import BaseModelCore


class RazorpayWebhookLog(BaseModelCore):
    """
    Model representing historical records of all incoming webhook calls received from Razorpay.
    Serves as an immutable audit trail for debugging and status reconciliation.
    """
    event_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        db_index=True,
        help_text="The unique event ID supplied by Razorpay."
    )
    event_type = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        db_index=True,
        help_text="The event type, e.g. payment.captured, payment.failed, order.paid."
    )
    signature = models.TextField(
        blank=True,
        null=True,
        help_text="The X-Razorpay-Signature header received with the request."
    )
    is_signature_valid = models.BooleanField(
        default=False,
        help_text="Flag indicating whether the cryptographic signature check passed."
    )
    payload = models.JSONField(
        help_text="The raw JSON payload body sent by Razorpay."
    )
    error_message = models.TextField(
        blank=True,
        null=True,
        help_text="Error message detailed if signature verification or processing fails."
    )

    class Meta:
        db_table = "onboard_razorpay_webhook_log"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Webhook event {self.event_type} (Valid={self.is_signature_valid}) at {self.created_at}"

"""Reading a tenant's own payment history.

Separate from the onboarding serializers, which describe a payment being *made*.
These describe one that has already happened, to somebody who wants to know what they
were charged for and what became of it.
"""

from rest_framework import serializers

from onboard.models import Transaction


class TransactionListSerializer(serializers.ModelSerializer):
    """One row of billing history."""

    summary = serializers.SerializerMethodField(
        help_text="What the payment was for, in words a tenant recognises."
    )
    can_cancel = serializers.SerializerMethodField(
        help_text="Whether POST .../cancel/ would succeed right now."
    )
    can_retry = serializers.SerializerMethodField(
        help_text="Whether POST .../retry/ would return a fresh checkout payload."
    )

    class Meta:
        model = Transaction
        fields = [
            "id",
            "razorpay_order_id",
            "razorpay_payment_id",
            "transaction_type",
            "status",
            "amount",
            "summary",
            "can_cancel",
            "can_retry",
            "expires_at",
            "created_at",
        ]
        read_only_fields = fields

    # `can_cancel` and `can_retry` are computed rather than derived from status alone
    # so the frontend never has to reimplement the rule. An expired PENDING row is
    # still PENDING in the database until something sweeps it, and offering a
    # "Pay now" button on it would send the tenant to a dead order.
    def _is_open(self, obj):
        from django.utils import timezone

        if obj.status != Transaction.TransactionStatusChoices.PENDING:
            return False
        if obj.transaction_type == Transaction.TransactionTypeChoices.ONBOARDING:
            # Belongs to the signup flow, which has its own cancel endpoint.
            return False
        return obj.expires_at is None or obj.expires_at > timezone.now()

    def get_can_cancel(self, obj) -> bool:
        return self._is_open(obj)

    def get_can_retry(self, obj) -> bool:
        return self._is_open(obj)

    def get_summary(self, obj) -> str:
        """A human-readable description of what was bought.

        Built from the breakdown frozen at order time rather than from the current
        catalogue, so a historical row keeps describing what the tenant actually
        agreed to even after prices or plan names move.
        """
        T = Transaction.TransactionTypeChoices
        breakdown = obj.amount_breakdown or {}

        if obj.transaction_type == T.TOP_UP:
            credits = obj.credits_purchased or 0
            return f"{credits:,} credits"

        if obj.transaction_type == T.ADDON:
            resource = breakdown.get("resource") or "capacity"
            units = breakdown.get("additional_units") or 0
            return f"{units} × {resource.replace('_', ' ')}"

        if obj.transaction_type == T.RENEWAL:
            name = getattr(obj.plan, "name", None)
            return f"Renewal — {name}" if name else "Renewal"

        if obj.transaction_type == T.UPGRADE:
            frm = getattr(obj.plan, "name", None)
            to = getattr(obj.target_plan, "name", None)
            if frm and to:
                return f"{frm} → {to}"
            return f"Plan change — {to}" if to else "Plan change"

        if obj.transaction_type == T.ONBOARDING:
            name = getattr(obj.plan, "name", None)
            return f"Signup — {name}" if name else "Signup"

        return obj.get_transaction_type_display()


class TransactionDetailSerializer(TransactionListSerializer):
    """One payment, with the price breakdown it was quoted at."""

    plan_name = serializers.CharField(source="plan.name", read_only=True, default=None)
    target_plan_name = serializers.CharField(
        source="target_plan.name", read_only=True, default=None
    )

    class Meta(TransactionListSerializer.Meta):
        fields = TransactionListSerializer.Meta.fields + [
            "plan_name",
            "target_plan_name",
            "credits_purchased",
            "amount_breakdown",
            "updated_at",
        ]
        read_only_fields = fields

    # `raw_webhook_payload` is deliberately absent. It is gateway diagnostics, can
    # carry contact details from the payment method, and belongs in the admin rather
    # than on a tenant-facing endpoint.


class CheckoutPayloadSerializer(serializers.Serializer):
    """Everything Razorpay Checkout needs, returned again for an unpaid order."""

    razorpay_order_id = serializers.CharField()
    amount = serializers.IntegerField(help_text="Paise. Hand to Razorpay as-is.")
    amount_display = serializers.CharField(help_text="Rupees, for showing a human.")
    currency = serializers.CharField()
    razorpay_key_id = serializers.CharField()
    expires_at = serializers.DateTimeField(allow_null=True)
    breakdown = serializers.DictField(allow_null=True)

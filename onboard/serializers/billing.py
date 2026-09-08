from rest_framework import serializers

from onboard.models.plan import MAX_SELECTABLE_MINUTES

from onboard.constants.entitlement_resource import EntitlementResource


class TopUpPurchaseSerializer(serializers.Serializer):
    """How many credits to buy."""

    credits = serializers.IntegerField(
        min_value=1,
        help_text="Whole credits to purchase. Priced at the plan's per-credit rate.",
    )


class AddonPurchaseSerializer(serializers.Serializer):
    """Which resource to extend, and by how much."""

    resource = serializers.ChoiceField(
        choices=EntitlementResource.choices,
        help_text="The capacity to extend, e.g. agent or storage_gb.",
    )
    quantity = serializers.IntegerField(
        min_value=1,
        help_text=(
            "Additional units. Charged per whole block, so asking for 12 on a 10-unit "
            "block bills two blocks."
        ),
    )


class QuoteSerializer(serializers.Serializer):
    """A price and the numbers behind it. Fields vary by product."""

    net = serializers.CharField(read_only=True)
    tax = serializers.CharField(read_only=True)
    total = serializers.CharField(read_only=True)
    tax_percentage = serializers.CharField(read_only=True)


class OrderCreatedSerializer(serializers.Serializer):
    """Everything the checkout widget needs, plus the breakdown that justified it."""

    razorpay_order_id = serializers.CharField(read_only=True)
    amount = serializers.IntegerField(read_only=True, help_text="In paise, for the gateway.")
    amount_display = serializers.CharField(read_only=True, help_text="In rupees, for humans.")
    currency = serializers.CharField(read_only=True)
    razorpay_key_id = serializers.CharField(read_only=True)
    expires_at = serializers.DateTimeField(read_only=True)
    breakdown = serializers.DictField(read_only=True)


class PlanChangeSerializer(serializers.Serializer):
    """Which plan to move to, and how much of it."""

    plan_id = serializers.UUIDField(help_text="The plan being moved to.")
    minutes = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        max_value=MAX_SELECTABLE_MINUTES,
        help_text=(
            "Minutes per cycle on the new plan. Omit to keep the quantity you have "
            "now, resolved into the new plan's band — a fixed-quantity plan always "
            "resolves to its single value. Ignored for a plan not sold by the minute."
        ),
    )


# --------------------------------------------------------------------------- #
# Response shapes
#
# These exist for the schema, not for validation: the views build their payloads
# by hand. Without them drf-spectacular cannot guess a response type for an
# APIView and **drops the endpoint from the OpenAPI document entirely** — so a
# client generating an SDK gets no method for it at all, silently.
# --------------------------------------------------------------------------- #

class SubscriptionPlanRefSerializer(serializers.Serializer):
    """Just enough of the plan to render a heading."""

    id = serializers.UUIDField(read_only=True)
    code = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    billing_cycle = serializers.CharField(read_only=True)


class ScheduledChangeSerializer(serializers.Serializer):
    """A downgrade waiting for the cycle to end."""

    plan_id = serializers.UUIDField(read_only=True)
    plan_code = serializers.CharField(read_only=True)
    effective_at = serializers.DateTimeField(read_only=True)


class EntitlementUsageSerializer(serializers.Serializer):
    """One resource: what the plan included, what was bought, what is left."""

    resource = serializers.CharField(read_only=True)
    included = serializers.IntegerField(read_only=True, allow_null=True)
    additional = serializers.IntegerField(read_only=True)
    limit = serializers.IntegerField(
        read_only=True, allow_null=True, help_text="Null means unlimited."
    )
    recurring_charge = serializers.CharField(read_only=True)
    used = serializers.IntegerField(read_only=True, required=False)
    remaining = serializers.IntegerField(
        read_only=True, required=False, allow_null=True
    )


class CurrentSubscriptionSerializer(serializers.Serializer):
    """Everything a billing screen renders, in one response.

    Returned for a lapsed subscription too — an expired tenant needs to see what
    they had and what it costs to come back, and a 409 cannot explain either.
    """

    id = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    plan = SubscriptionPlanRefSerializer(read_only=True)
    start_date = serializers.DateTimeField(read_only=True)
    end_date = serializers.DateTimeField(read_only=True)
    days_remaining = serializers.IntegerField(
        read_only=True, help_text="Floors at 0; never negative."
    )
    renewable_now = serializers.BooleanField(
        read_only=True,
        help_text=(
            "Whether the renewal window is open. Reported rather than derived, so "
            "the client does not reimplement the window rule and show the button on "
            "a different day from the one the server accepts."
        ),
    )
    scheduled_change = ScheduledChangeSerializer(read_only=True, allow_null=True)
    credits = serializers.DictField(read_only=True)
    entitlements = EntitlementUsageSerializer(many=True, read_only=True)
    addons_recurring_net = serializers.CharField(
        read_only=True, help_text="Pre-tax rupees add-ons add to every renewal."
    )


class EligiblePlanSerializer(serializers.Serializer):
    """One plan, tagged with what moving to it would mean and cost."""

    id = serializers.UUIDField(
        read_only=True,
        help_text="Pass as `plan_id` to preview-change/ or change-plan/.",
    )
    code = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    billing_cycle = serializers.CharField(read_only=True)
    total_price = serializers.CharField(read_only=True)
    allocated_credits = serializers.IntegerField(read_only=True)
    is_current = serializers.BooleanField(read_only=True)
    is_most_popular = serializers.BooleanField(read_only=True)
    change_type = serializers.CharField(
        read_only=True,
        help_text=(
            "current, paid_change, scheduled_change, immediate_free, or unavailable."
        ),
    )
    amount_due = serializers.CharField(read_only=True, required=False)
    effective_at = serializers.CharField(read_only=True, required=False)
    reason = serializers.CharField(
        read_only=True, required=False,
        help_text="Why this plan cannot be priced, when change_type is unavailable.",
    )


class EligiblePlansSerializer(serializers.Serializer):
    """Every active plan, priced against the tenant's current subscription."""

    plans = EligiblePlanSerializer(many=True, read_only=True)


class ScheduledChangeCancelledSerializer(serializers.Serializer):
    """Nothing to return but the acknowledgement."""

    message = serializers.CharField(read_only=True)


class PlanChangeAppliedSerializer(serializers.Serializer):
    """A change that took effect without a payment.

    Covers both a scheduled downgrade and a sub-rupee change applied outright — the
    two cases where there is no order to hand back.
    """

    change_type = serializers.CharField(read_only=True)
    # Echoed back so a client that fired the request optimistically can reconcile the
    # answer without holding request state -- and so cancelling a scheduled change
    # does not need a separate lookup to know what it is cancelling.
    plan_id = serializers.UUIDField(read_only=True)
    plan_code = serializers.CharField(read_only=True)
    effective_at = serializers.DateTimeField(read_only=True, required=False)
    charged = serializers.BooleanField(read_only=True)


class PlanChangePreviewSerializer(serializers.Serializer):
    """What a plan change would cost, and what it would do to the credits.

    Both halves, because either alone leaves the tenant deciding blind.
    """

    money = serializers.DictField(read_only=True)
    credits = serializers.DictField(read_only=True)


class AddonAppliedSerializer(serializers.Serializer):
    """Capacity granted without a charge.

    Late in a cycle the prorated amount falls below the gateway minimum, so the
    capacity is granted outright and the full price starts at the next renewal.
    """

    charged = serializers.BooleanField(read_only=True)
    breakdown = serializers.DictField(read_only=True)


class VerifyBillingPaymentSerializer(serializers.Serializer):
    """The three values Razorpay Checkout hands back to the browser.

    Same shape as the onboarding flow's, deliberately — the frontend already knows
    how to collect these, and a second spelling of the same three fields would be a
    trap.
    """

    razorpay_order_id = serializers.CharField(
        help_text="The order id returned when the purchase was created."
    )
    razorpay_payment_id = serializers.CharField(
        help_text="Razorpay's id for the payment that settled the order."
    )
    razorpay_signature = serializers.CharField(
        help_text=(
            "HMAC Razorpay returns alongside the payment. Proves the browser is not "
            "reporting a success that never happened."
        )
    )


class PaymentVerifiedSerializer(serializers.Serializer):
    """What the tenant has once the payment has been applied."""

    razorpay_order_id = serializers.CharField()
    transaction_type = serializers.CharField(
        help_text="top_up, addon, renewal or upgrade."
    )
    amount = serializers.CharField(help_text="Rupees charged, inclusive of tax.")
    already_applied = serializers.BooleanField(
        help_text=(
            "True when the webhook had already applied this payment. The purchase is "
            "still complete — it means this call changed nothing, not that it failed."
        )
    )
    credits = serializers.DictField(
        allow_null=True,
        help_text="Wallet balance after the purchase. Null if it could not be read.",
    )
    entitlements = serializers.ListField(
        allow_null=True,
        child=serializers.DictField(),
        help_text=(
            "Every resource with its limit and live usage, the same shape "
            "GET /billing/subscription/ returns."
        ),
    )

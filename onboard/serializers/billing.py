from rest_framework import serializers

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
    """Which plan to move to."""

    plan_id = serializers.UUIDField(help_text="The plan being moved to.")


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

    code = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    billing_cycle = serializers.CharField(read_only=True)


class ScheduledChangeSerializer(serializers.Serializer):
    """A downgrade waiting for the cycle to end."""

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

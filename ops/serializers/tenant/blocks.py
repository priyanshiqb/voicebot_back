"""The blocks of the 360, one serializer each.

Split out of ``detail.py`` because they are genuinely different things — a payment
row and an entitlement row share nothing — and because a future payments screen wants
``PaymentRowSerializer`` without the envelope around it.
"""

from rest_framework import serializers


class TenantProfileSerializer(serializers.Serializer):
    """The tenant's own record."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    subdomain = serializers.CharField(read_only=True)
    phone_number = serializers.CharField(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    is_deleted = serializers.BooleanField(read_only=True)
    deleted_at = serializers.DateTimeField(read_only=True, allow_null=True)
    referred_by_code = serializers.CharField(read_only=True, allow_null=True)
    tenant_referral_code = serializers.CharField(read_only=True, allow_null=True)
    timezone = serializers.SerializerMethodField()
    language = serializers.SerializerMethodField()

    def get_timezone(self, obj):
        return str(obj.timezone) if obj.timezone_id else None

    def get_language(self, obj):
        return str(obj.language) if obj.language_id else None


class OwnerSerializer(serializers.Serializer):
    """Who to contact. See ``detail.owner_of`` for why this is derived."""

    id = serializers.UUIDField(read_only=True)
    email = serializers.EmailField(read_only=True)
    name = serializers.SerializerMethodField()
    last_login = serializers.DateTimeField(read_only=True, allow_null=True)
    is_verified = serializers.BooleanField(read_only=True)
    is_active = serializers.BooleanField(read_only=True)

    def get_name(self, obj):
        return (obj.get_full_name() or obj.username or "").strip()


class SubscriptionBlockSerializer(serializers.Serializer):
    """What they bought, and until when.

    ``plan`` is the **snapshot** — the frozen contract — with the live plan offered
    separately so the console can show that a plan has been edited since. Reading
    ``plan.name`` as *the* answer is how an admin's rename silently rewrites what a
    customer is told they are paying for.
    """

    id = serializers.UUIDField(read_only=True)
    status = serializers.CharField(read_only=True)
    start_date = serializers.DateTimeField(read_only=True)
    end_date = serializers.DateTimeField(read_only=True)
    is_expired = serializers.BooleanField(read_only=True)
    is_current = serializers.BooleanField(read_only=True)
    days_remaining = serializers.SerializerMethodField()
    plan = serializers.SerializerMethodField()
    live_plan = serializers.SerializerMethodField()
    rules = serializers.JSONField(read_only=True)
    scheduled_change = serializers.SerializerMethodField()
    superseded_by = serializers.SerializerMethodField()
    canceled_at = serializers.DateTimeField(read_only=True, allow_null=True)

    def get_days_remaining(self, obj):
        """Whole days left in the cycle. Negative once it has lapsed.

        Signed deliberately: "-3" says the cycle ended three days ago, which is the
        number support needs, and clamping it to 0 would hide exactly that.

        **Truncated toward zero, not ``timedelta.days``.** That property floors, so a
        cycle that ended three days and one second ago reports -4 — a day worse than
        the truth, on the exact number a support agent reads out to a customer.
        """
        from django.utils import timezone

        if not obj.end_date:
            return None
        # `int()` truncates toward zero for both signs, which is what reads correctly:
        # 3.5 days left is "3 days left", and 3.5 days ago is "ended 3 days ago".
        return int((obj.end_date - timezone.now()).total_seconds() / 86400)

    def get_plan(self, obj):
        return obj.plan_snapshot or None

    def get_live_plan(self, obj):
        if not obj.plan_id:
            return None
        return {"id": str(obj.plan_id), "code": obj.plan.code, "name": obj.plan.name}

    def get_scheduled_change(self, obj):
        if not obj.scheduled_plan_id:
            return None
        return {
            "plan": {
                "id": str(obj.scheduled_plan_id),
                "code": obj.scheduled_plan.code,
                "name": obj.scheduled_plan.name,
            },
            "at": obj.scheduled_change_at,
        }

    def get_superseded_by(self, obj):
        """The subscription that replaced this one, if any.

        The first thing support asks for when a tenant disputes a rollover.
        """
        return str(obj.superseded_by_id) if obj.superseded_by_id else None


class WalletBlockSerializer(serializers.Serializer):
    """The balance, its two buckets, and the rate card in force.

    ``rate_snapshot`` is carried in full. It is the answer to "what does a minute cost
    them?", and it is not derivable from anything else on the screen — the live
    ``CreditRate`` rows may have been superseded since this card was stamped.
    """

    id = serializers.UUIDField(read_only=True)
    balance = serializers.IntegerField(read_only=True)
    allocated_balance = serializers.IntegerField(read_only=True)
    purchased_balance = serializers.IntegerField(read_only=True)
    lifetime_granted = serializers.IntegerField(read_only=True)
    lifetime_consumed = serializers.IntegerField(read_only=True)
    is_frozen = serializers.BooleanField(read_only=True)
    frozen_reason = serializers.CharField(read_only=True, allow_null=True)
    frozen_at = serializers.DateTimeField(read_only=True, allow_null=True)
    low_balance_threshold = serializers.IntegerField(read_only=True)
    low_balance_notified_at = serializers.DateTimeField(read_only=True, allow_null=True)
    current_plan = serializers.SerializerMethodField()
    rate_snapshot = serializers.JSONField(read_only=True)
    rate_snapshot_at = serializers.DateTimeField(read_only=True, allow_null=True)

    def get_current_plan(self, obj):
        if not obj.current_plan_id:
            return None
        return {
            "id": str(obj.current_plan_id),
            "code": obj.current_plan.code,
            "name": obj.current_plan.name,
        }


#: What a tenant with no wallet row looks like. The model's own docstring says an
#: absent row behaves like an empty wallet; this is that, said out loud, rather than a
#: `null` the console has to special-case in six places.
EMPTY_WALLET = {
    "exists": False,
    "balance": 0,
    "allocated_balance": 0,
    "purchased_balance": 0,
    "lifetime_granted": 0,
    "lifetime_consumed": 0,
    "is_frozen": False,
    "frozen_reason": None,
    "frozen_at": None,
    "low_balance_threshold": 0,
    "low_balance_notified_at": None,
    "current_plan": None,
    "rate_snapshot": None,
    "rate_snapshot_at": None,
}


class PaymentRowSerializer(serializers.Serializer):
    """One order.

    ``amount_breakdown`` is included because, per the model, it is "the only record of
    how the number was produced" when a prorated charge is disputed six weeks later.
    """

    id = serializers.UUIDField(read_only=True)
    razorpay_order_id = serializers.CharField(read_only=True)
    razorpay_payment_id = serializers.CharField(read_only=True, allow_null=True)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    status = serializers.CharField(read_only=True)
    transaction_type = serializers.CharField(read_only=True)
    credits_purchased = serializers.IntegerField(read_only=True)
    amount_breakdown = serializers.JSONField(read_only=True)
    expires_at = serializers.DateTimeField(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)
    plan = serializers.SerializerMethodField()
    target_plan = serializers.SerializerMethodField()
    bonus_days = serializers.SerializerMethodField()

    def get_plan(self, obj):
        return obj.plan.name if obj.plan_id else None

    def get_target_plan(self, obj):
        return obj.target_plan.name if obj.target_plan_id else None

    def get_bonus_days(self, obj):
        """Extra days this order bought, from the breakdown that recorded them.

        **Not a field on the subscription.** Bonus days are computed at pricing time,
        applied to ``end_date`` by ``activate()``, and survive only here. Surfacing
        them off the order that caused them is honest; adding a subscription field
        that nothing writes would not be.
        """
        breakdown = obj.amount_breakdown or {}
        return breakdown.get("bonus_days")


class LedgerRowSerializer(serializers.Serializer):
    """One credit movement, with the rating that produced it.

    The rating fields are what answer "why was this call 12 credits?" — the third of
    the three layers, after the plan's advertised rate and the wallet's stamped card.
    """

    id = serializers.UUIDField(read_only=True)
    entry_type = serializers.CharField(read_only=True)
    bucket = serializers.CharField(read_only=True)
    amount = serializers.IntegerField(read_only=True)
    balance_after = serializers.IntegerField(read_only=True)
    operation = serializers.CharField(read_only=True, allow_null=True)
    raw_quantity = serializers.IntegerField(read_only=True, allow_null=True)
    units = serializers.IntegerField(read_only=True, allow_null=True)
    unit_type = serializers.CharField(read_only=True, allow_null=True)
    rate_applied = serializers.IntegerField(read_only=True, allow_null=True)
    reference_id = serializers.CharField(read_only=True, allow_null=True)
    subscription = serializers.SerializerMethodField()
    notes = serializers.CharField(read_only=True, allow_null=True)
    metadata = serializers.JSONField(read_only=True)
    is_deleted = serializers.BooleanField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)

    def get_subscription(self, obj):
        return str(obj.subscription_id) if obj.subscription_id else None

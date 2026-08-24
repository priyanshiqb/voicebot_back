from rest_framework import serializers

from wallet.models.wallet import Wallet


class WalletBalanceSerializer(serializers.ModelSerializer):
    """What a tenant sees when they ask "how many credits do I have?".

    Both buckets are exposed, not just the total, because the difference is something
    the tenant can act on: plan credits may expire at the cycle boundary while
    purchased ones never do. Hiding the split makes "why did my balance drop at
    renewal?" unanswerable from the API.
    """

    plan_code = serializers.SerializerMethodField()
    plan_name = serializers.CharField(source="current_plan.name", read_only=True, default=None)
    is_low = serializers.BooleanField(read_only=True)
    is_spendable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Wallet
        fields = (
            "balance",
            "allocated_balance",
            "purchased_balance",
            "is_frozen",
            "frozen_reason",
            "frozen_at",
            "is_spendable",
            "is_low",
            "low_balance_threshold",
            "plan_code",
            "plan_name",
            "lifetime_granted",
            "lifetime_consumed",
            "updated_at",
        )
        read_only_fields = fields

    def get_plan_code(self, obj):
        return (obj.rate_snapshot or {}).get("plan_code")


class WalletRateSerializer(serializers.Serializer):
    """One operation's price, as stamped on the wallet."""

    operation = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    unit = serializers.CharField(read_only=True)
    credits_per_unit = serializers.IntegerField(read_only=True)
    rounding = serializers.CharField(read_only=True)
    minimum_units = serializers.IntegerField(read_only=True)


class WalletRateCardSerializer(serializers.Serializer):
    """The whole stamped card. Read from the wallet, never from the catalogue."""

    plan_code = serializers.CharField(read_only=True, allow_null=True)
    stamped_at = serializers.CharField(read_only=True, allow_null=True)
    operations = WalletRateSerializer(many=True, read_only=True)


class CreditQuoteRequestSerializer(serializers.Serializer):
    """Ask what something would cost before doing it."""

    operation = serializers.CharField(
        help_text="A call tier: static_reel, conversation_high_latency, conversation_low_latency.",
    )
    quantity = serializers.IntegerField(
        min_value=1,
        help_text="Quantity in the operation's natural unit — seconds, for a call.",
    )


class CreditQuoteSerializer(serializers.Serializer):
    """A price, with the arithmetic that produced it.

    The same ``RatingResult`` the consumption path uses, so a quote and the charge
    that follows it cannot disagree.
    """

    operation = serializers.CharField(read_only=True)
    credits = serializers.IntegerField(read_only=True)
    units = serializers.IntegerField(read_only=True)
    raw_quantity = serializers.IntegerField(read_only=True)
    unit_type = serializers.CharField(read_only=True)
    rate_applied = serializers.IntegerField(read_only=True)
    affordable = serializers.BooleanField(read_only=True)
    balance = serializers.IntegerField(read_only=True)

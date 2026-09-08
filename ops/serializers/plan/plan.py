"""The 21 plan fields, plus the money arithmetic derived from them.

Not the public `PlanSerializer`. That one serves the pricing page with ``AllowAny``,
so every field on it is public — and this needs `max_additional`, cost inputs and
retired plans, none of which belong on a page anonymous visitors read.

``status`` is deliberately **read-only here**. A plan's lifecycle moves through
``activate`` and ``retire``, which check completeness and count subscribers; letting a
PATCH set it would be a way round both.
"""

from rest_framework import serializers

from onboard.models import Plan


class PlanFieldsSerializer(serializers.ModelSerializer):
    """The plan record itself. Every field required on create."""

    # Derived, and shown because `list - discount + tax = charged` is the arithmetic
    # people get wrong.
    discount_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    net_base_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    tax_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    total_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )

    sort_order = serializers.IntegerField(
        required=True, min_value=0,
        help_text=(
            "Left-to-right order on the pricing page. **Required**, because the model "
            "defaults it to 0 and every plan sharing that value orders arbitrarily "
            "against the others."
        ),
    )

    class Meta:
        model = Plan
        fields = (
            "id",
            "status",
            # identity
            "code", "name", "description", "sort_order", "is_most_popular",
            "plan_type",
            # price
            "discount_percentage", "tax_percentage", "currency",
            "billing_cycle",
            # minutes — the band this plan sells and the tier it is sold at
            "min_minutes", "max_minutes", "credit_basis",
            # credits
            "cost_per_additional_credit",
            "low_balance_threshold", "low_balance_percent",
            # rollover
            "allow_rollover_on_renewal", "allow_rollover_on_upgrade",
            "allow_rollover_on_downgrade", "max_rollover_credits",
            "max_rollover_ratio",
            # what happens when a cycle ends
            "allow_use_after_expiry", "renews_without_payment",
            # derived
            "discount_amount", "net_base_price", "tax_amount", "total_price",
        )
        read_only_fields = ("id", "status")

        # **Every settable field is required.** ModelSerializer makes a field optional
        # whenever the model carries a default, which is exactly backwards for this
        # module: a default is a decision nobody made, and both ways a plan can be
        # silently broken come from a decision nobody made.
        #
        # `allow_null` / `allow_blank` where empty is a real answer, so "unlimited" and
        # "I forgot" stop looking the same on the wire. PATCH is unaffected —
        # `partial=True` ignores `required` — so this binds creation only.
        extra_kwargs = {
            "description": {"required": True, "allow_blank": True},
            "is_most_popular": {"required": True},
            "plan_type": {"required": True},
            "discount_percentage": {"required": True},
            "tax_percentage": {"required": True},
            "currency": {"required": True},
            "billing_cycle": {"required": True},
            # Not required: a plan may be legacy (fixed price, fixed credits) or
            # minutes-based, and demanding a band would make the first unexpressible.
            # Completeness is enforced at activation, where the whole ladder is
            # visible, rather than field by field on the wire.
            "min_minutes": {"required": False, "allow_null": True},
            "max_minutes": {"required": False, "allow_null": True},
            "credit_basis": {"required": False, "allow_null": True},
            "cost_per_additional_credit": {"required": True},
            "low_balance_threshold": {"required": True},
            "low_balance_percent": {"required": False, "allow_null": True},
            "max_rollover_ratio": {"required": False, "allow_null": True},
            "allow_rollover_on_renewal": {"required": True},
            "allow_rollover_on_upgrade": {"required": True},
            "allow_rollover_on_downgrade": {"required": True},
            "max_rollover_credits": {"required": True, "allow_null": True},
            "allow_use_after_expiry": {"required": True},
            "renews_without_payment": {"required": True},
        }

    def validate(self, attrs):
        # The 'a paid plan may not renew itself' rule lives in the readiness
        # gate now (RENEWS_FREE_BUT_PRICED). The price is on the basis rate
        # row, so it is a cross-table question a field serializer cannot see.
        return attrs


class PlanListSerializer(serializers.ModelSerializer):
    """A row in the plan list, with the counts that make a plan safe to judge.

    ``entitlements_configured`` and ``rates_configured`` are annotated by the view,
    not counted per row — the list renders every plan and a property here would be two
    queries each.
    """

    total_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    subscriber_count = serializers.IntegerField(read_only=True)
    entitlements_configured = serializers.IntegerField(read_only=True)
    rates_configured = serializers.IntegerField(read_only=True)
    is_complete = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = (
            "id", "code", "name", "status", "billing_cycle", "sort_order",
            "total_price", "is_most_popular",
            "subscriber_count", "entitlements_configured", "rates_configured",
            "is_complete",
        )
        read_only_fields = fields

    def get_is_complete(self, obj):
        """Whether this plan could be activated as it stands.

        An active plan showing false is a bug worth shouting about — activation is
        gated on exactly this — so the column earns its place on every row rather
        than only on drafts.
        """
        return (
            getattr(obj, "entitlements_configured", 0) == 7
            and getattr(obj, "rates_configured", 0) == 3
        )

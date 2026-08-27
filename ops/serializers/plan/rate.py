"""The rate card — 3 call tiers × 7 settings.

Same rule as the capacity grid: every field required, nothing defaulted on the wire.

The difference is that **rates are versioned**. A plan's live card is the set of rows
with ``effective_to IS NULL``, one per operation, held by
``uniq_current_rate_per_plan_operation``. Changing a rate is therefore not an update —
it closes the old row and inserts a new one, which is what keeps every historical
charge reproducible.
"""

from rest_framework import serializers

from wallet.models import CreditRate


class CreditRateSerializer(serializers.ModelSerializer):
    """One call tier's price on one plan."""

    operation = serializers.ChoiceField(choices=CreditRate.Operation.choices)
    credits_per_unit = serializers.IntegerField(
        required=True, min_value=0,
        help_text="Credits per billable unit. 0 makes the tier free on this plan.",
    )
    unit = serializers.ChoiceField(
        choices=CreditRate.Unit.choices, required=True,
        help_text="What one billable unit is. per_event is a flat price per call.",
    )
    rounding = serializers.ChoiceField(
        choices=CreditRate.Rounding.choices, required=True,
        help_text=(
            "How a partial unit resolves. Rounding up is the telecoms standard: a "
            "90-second call on a per-minute rate bills two minutes."
        ),
    )
    minimum_units = serializers.IntegerField(
        required=True, min_value=0,
        help_text="Smallest billable quantity. With 1, a 4-second call bills a minute.",
    )
    notes = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text=(
            "Why this rate exists, or why it replaced the last one. The only "
            "optional field here — it is for humans, not for billing."
        ),
    )

    class Meta:
        model = CreditRate
        fields = (
            "operation",
            "credits_per_unit",
            "unit",
            "rounding",
            "minimum_units",
            "notes",
        )


class CreditRateCardSerializer(serializers.Serializer):
    """All three tiers at once.

    Every tier, every time — a card missing one cannot price that kind of call, and
    ``resolve_rate()`` raises at the point of the call rather than charging zero.
    """

    rates = CreditRateSerializer(many=True)

    def validate_rates(self, rows):
        seen = [row["operation"] for row in rows]

        duplicates = sorted({o for o in seen if seen.count(o) > 1})
        if duplicates:
            raise serializers.ValidationError(
                "Repeated tier: " + ", ".join(duplicates)
            )

        missing = sorted(set(CreditRate.Operation.values) - set(seen))
        if missing:
            raise serializers.ValidationError(
                "Every call tier must be priced. Missing: "
                + ", ".join(m.replace("_", " ") for m in missing)
                + ". Calls on an unpriced tier fail at the point of the call."
            )

        return rows

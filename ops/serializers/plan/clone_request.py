"""What a clone needs decided before it can be made."""

from rest_framework import serializers

from onboard.models import Plan


class PlanCloneSerializer(serializers.Serializer):
    """`code` and `name` are unique, so a copy cannot inherit them."""

    code = serializers.CharField(max_length=50)
    name = serializers.CharField(max_length=100)
    billing_cycle = serializers.ChoiceField(
        choices=Plan.BillingCycleChoices.choices,
        required=False,
        help_text=(
            "Switch the copy to a different cycle. **Prices are not converted** — "
            "every one is charged per cycle, so each buys a different amount of time "
            "on the new plan and needs deciding rather than calculating."
        ),
    )
    sort_order = serializers.IntegerField(
        required=False, min_value=0,
        help_text=(
            "Where the copy sits on the pricing page. Left out, it inherits the "
            "original's and the two order arbitrarily against each other."
        ),
    )

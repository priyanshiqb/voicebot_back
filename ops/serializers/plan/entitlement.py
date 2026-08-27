"""The capacity grid — 7 resources × 6 settings.

**Every field is required**, including the ones with model defaults. A default is a
decision nobody made, and the failure this module exists to prevent is caused by
exactly that: a plan reaching a customer with an allowance nobody set.

``allow_null`` where blank is meaningful, ``required`` everywhere. So a resource left
unlimited arrives as ``"included_quantity": null`` — **present and null, not absent** —
and "I chose unlimited" stops looking identical to "I forgot this row".
"""

from rest_framework import serializers

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.constants.reset_period import ResetPeriod
from onboard.models import PlanEntitlement


class PlanEntitlementSerializer(serializers.ModelSerializer):
    """One resource's allowance on one plan."""

    resource = serializers.ChoiceField(choices=EntitlementResource.choices)
    included_quantity = serializers.IntegerField(
        required=True,
        allow_null=True,
        min_value=0,
        help_text="How many the plan grants. Null means unlimited — send it explicitly.",
    )
    reset_period = serializers.ChoiceField(
        choices=ResetPeriod.choices,
        required=True,
        help_text=(
            "What the number means. 'none' is a standing total; anything else makes "
            "it an allowance that refills, counted from when the act happened."
        ),
    )
    is_addon_available = serializers.BooleanField(required=True)
    price_per_additional = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=True,
        help_text="Pre-tax rupees per block, charged every cycle. Not a one-off.",
    )
    addon_step = serializers.IntegerField(
        required=True, min_value=1,
        help_text="Block size. A constraint refuses anything below 1.",
    )
    max_additional = serializers.IntegerField(
        required=True, allow_null=True, min_value=0,
        help_text="Ceiling on extras, in units. Null means uncapped.",
    )

    class Meta:
        model = PlanEntitlement
        fields = (
            "resource",
            "included_quantity",
            "reset_period",
            "is_addon_available",
            "price_per_additional",
            "addon_step",
            "max_additional",
        )

    def validate(self, attrs):
        # `ck_entitlement_no_addon_when_unlimited` refuses this at the database. Left
        # to the database it surfaces as a 500 with an opaque constraint name; caught
        # here it is a sentence somebody can act on.
        if attrs.get("included_quantity") is None and attrs.get("is_addon_available"):
            raise serializers.ValidationError({
                "is_addon_available": (
                    "An unlimited allowance cannot also be purchasable — set a "
                    "quantity, or turn add-ons off."
                )
            })
        return attrs


class PlanEntitlementGridSerializer(serializers.Serializer):
    """All seven rows at once, which is how the screen edits them.

    **A collection, not a row.** The grid is saved once, so seven round trips would
    let a plan exist half-updated between them — and the completeness rule this module
    enforces is about the set, not any single row.
    """

    entitlements = PlanEntitlementSerializer(many=True)

    def validate_entitlements(self, rows):
        seen = [row["resource"] for row in rows]

        duplicates = sorted({r for r in seen if seen.count(r) > 1})
        if duplicates:
            raise serializers.ValidationError(
                "Repeated resource: " + ", ".join(duplicates)
            )

        missing = sorted(set(EntitlementResource.values) - set(seen))
        if missing:
            raise serializers.ValidationError(
                "Every resource must be configured. Missing: "
                + ", ".join(m.replace("_", " ") for m in missing)
                + ". A resource with no allowance is not limited to zero — it is not "
                "limited at all."
            )

        return rows

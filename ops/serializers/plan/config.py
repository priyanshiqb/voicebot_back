"""The whole plan in one payload — create with it, read it back with it.

Creation takes all three models at once: the 21 plan fields, all 7 capacity rows, all
3 rate rows. **84 settings, none of them defaulted on the wire.**

That is stricter than "create the plan and fill the rest in later", deliberately. Both
ways a plan can be silently broken come from a decision nobody made:

* an allowance that is absent rather than set, which reads as *unlimited*
* a tier that is unpriced rather than free, which fails at the point of a call

Requiring the whole thing means neither can be created. The plan is still a **draft**
afterwards — complete is not the same as correct, and somebody still has to look at it.
"""

from rest_framework import serializers

from ops.serializers.plan.entitlement import PlanEntitlementSerializer
from ops.serializers.plan.plan import PlanFieldsSerializer
from ops.serializers.plan.rate import CreditRateSerializer
from onboard.constants.entitlement_resource import EntitlementResource
from wallet.models import CreditRate


class PlanConfigurationSerializer(serializers.Serializer):
    """Everything needed to create a plan, in one request."""

    plan = PlanFieldsSerializer()
    entitlements = PlanEntitlementSerializer(many=True)
    rates = CreditRateSerializer(many=True)

    def validate_entitlements(self, rows):
        return _complete_set(
            rows,
            key="resource",
            required=set(EntitlementResource.values),
            noun="resource",
            consequence=(
                "A resource with no allowance is not limited to zero — it is not "
                "limited at all, and nothing would report it."
            ),
        )

    def validate_rates(self, rows):
        return _complete_set(
            rows,
            key="operation",
            required=set(CreditRate.Operation.values),
            noun="call tier",
            consequence=(
                "Calls on an unpriced tier cannot be billed and fail at the point of "
                "the call."
            ),
        )


def _complete_set(rows, *, key, required, noun, consequence):
    """Reject a collection that repeats or omits any member of ``required``."""
    seen = [row[key] for row in rows]

    duplicates = sorted({v for v in seen if seen.count(v) > 1})
    if duplicates:
        raise serializers.ValidationError(
            f"Repeated {noun}: " + ", ".join(v.replace("_", " ") for v in duplicates)
        )

    missing = sorted(required - set(seen))
    if missing:
        raise serializers.ValidationError(
            f"Every {noun} must be configured. Missing: "
            + ", ".join(v.replace("_", " ") for v in missing)
            + ". " + consequence
        )

    return rows


class PlanDetailSerializer(serializers.Serializer):
    """A plan read back whole — the editor loads once and holds all three models.

    Read-only. Writes go through the three focused endpoints, because a single
    write-everything endpoint would make a rate change indistinguishable from a name
    change, and rates have to supersede rather than update.
    """

    plan = PlanFieldsSerializer(read_only=True)
    entitlements = PlanEntitlementSerializer(many=True, read_only=True)
    rates = CreditRateSerializer(many=True, read_only=True)
    readiness = serializers.DictField(read_only=True)
    subscriber_count = serializers.IntegerField(read_only=True)

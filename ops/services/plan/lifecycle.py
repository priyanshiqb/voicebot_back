"""Moving a plan between draft, active and retired.

Separate from :mod:`config` because the transitions have rules and the fields do not.
``activate`` refuses an incomplete plan; ``retire`` reports who stays behind; a PATCH
on the 21 fields does neither. Keeping them apart is what stops somebody adding a
completeness check to a field update because it happened to be in the same file.
"""

from django.db import transaction

from onboard.models import Plan
from ops.services.plan import config, readiness


class PlanNotReady(Exception):
    """Activation refused. Carries what is missing, so the caller can say so."""

    def __init__(self, problems):
        self.problems = problems
        super().__init__("This plan is not complete enough to be activated.")


@transaction.atomic
def activate(plan):
    """Make a plan sellable. **Refused unless it is complete.**

    This is the gate the whole module is built around. A plan reaches customers
    through exactly one door, and the two ways it can be silently broken — an
    allowance that is absent rather than set, a tier that is unpriced rather than
    free — are both stopped here rather than warned about.

    Works from ``DRAFT`` and from ``RETIRED``. A plan withdrawn a year ago goes
    through the same check on the way back, because a resource may have been added to
    the system since it was last live.
    """
    problems = readiness.blocking_problems(plan)
    if problems:
        raise PlanNotReady(problems)

    plan.status = Plan.Status.ACTIVE
    plan.save(update_fields=["status", "updated_at"])
    return plan


@transaction.atomic
def retire(plan):
    """Withdraw a plan. **Stops new subscriptions and nothing else.**

    Not refused when subscribers remain, because there is nothing to protect them
    from: their terms are frozen on their own subscription, not read from the
    catalogue row. They keep the plan, keep their price and keep renewing on it.

    Returns the count so the console can say *"12 tenants stay on this plan"* rather
    than leaving somebody to wonder what they just did.
    """
    plan.status = Plan.Status.RETIRED
    plan.save(update_fields=["status", "updated_at"])
    return {"plan": plan, "subscribers_remaining": config.subscriber_count(plan)}

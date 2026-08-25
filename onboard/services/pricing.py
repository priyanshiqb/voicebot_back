"""What a mid-cycle purchase costs.

Two products, one rule: the tenant pays at the price that applies to their plan, plus
tax at their plan's rate. They differ in whether proration applies — see below.
"""

import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal("0.01")


class PricingError(Exception):
    """The purchase cannot be priced — disabled, capped, or nonsensical."""


def _money(value):
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Quote:
    """A price, with every number that produced it.

    Stored verbatim on ``Transaction.amount_breakdown``. When a tenant disputes a
    charge weeks later, this is the only record of how it was calculated — the plan's
    price may have moved since, so recomputing would give a different answer.
    """

    net: Decimal
    tax: Decimal
    total: Decimal
    tax_percentage: Decimal
    detail: dict = field(default_factory=dict)

    def as_breakdown(self):
        return {
            "net": str(self.net),
            "tax": str(self.tax),
            "total": str(self.total),
            "tax_percentage": str(self.tax_percentage),
            **{key: str(value) for key, value in self.detail.items()},
        }


def _with_tax(net, plan):
    tax_percentage = Decimal(str(plan.tax_percentage or 0))
    net = _money(net)
    tax = _money(net * tax_percentage / Decimal("100"))
    return net, tax, _money(net + tax), tax_percentage


def unused_ratio(subscription, *, now=None):
    """How much of the current cycle is still to come, as a 0-1 ratio.

    Capacity bought on day 25 of 30 should cost a sixth of a cycle, not a whole one.
    """
    now = now or timezone.now()
    start, end = subscription.start_date, subscription.end_date
    span = (end - start).total_seconds()
    if span <= 0:
        return Decimal("0")
    remaining = max((end - now).total_seconds(), 0)
    return (Decimal(remaining) / Decimal(span)).quantize(Decimal("0.0001"))


# --------------------------------------------------------------------------- #
# Credit top-ups
# --------------------------------------------------------------------------- #

#: A single order cannot buy more than this. Not a policy so much as a guard: a
#: mistyped quantity should be refused, not charged.
MAX_TOP_UP_CREDITS = 1_000_000


def quote_top_up(subscription, credits):
    """Price a mid-cycle credit purchase.

    **Not prorated.** A credit is worth the same whenever it is bought and never
    expires out of the purchased bucket — unlike capacity, which is rented for the
    remainder of the cycle.
    """
    credits = int(credits)
    if credits <= 0:
        raise PricingError("Number of credits must be a positive whole number.")
    if credits > MAX_TOP_UP_CREDITS:
        raise PricingError(
            f"A single top-up is limited to {MAX_TOP_UP_CREDITS:,} credits."
        )

    unit_price = Decimal(str(subscription.rules.get("cost_per_additional_credit") or 0))
    if unit_price <= 0:
        raise PricingError(
            "This plan does not sell additional credits. Upgrading is the way to get more."
        )

    net, tax, total, tax_percentage = _with_tax(unit_price * credits, subscription.plan)
    return Quote(
        net=net, tax=tax, total=total, tax_percentage=tax_percentage,
        detail={"credits": credits, "unit_price": unit_price},
    )


# --------------------------------------------------------------------------- #
# Entitlement add-ons
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Plan changes
# --------------------------------------------------------------------------- #

#: What a plan change turns out to be, once the money is worked out.
PAID_CHANGE = "paid_change"          # they owe something; applies when it clears
SCHEDULED_CHANGE = "scheduled_change"  # they owe nothing; applies at cycle end
IMMEDIATE_FREE = "immediate_free"    # they owe less than a gateway will take


def quote_plan_change(subscription, new_plan, *, now=None):
    """Price a mid-cycle move to another plan.

    Computed on **pre-tax** values, then taxed at the *new* plan's rate — tax has to
    be charged on the actual taxable amount, not on a figure that already includes
    someone else's tax.

    Prices for the outgoing plan come from ``plan_snapshot``, never the live plan: the
    tenant is owed credit for what they actually paid, and the catalogue may have
    moved since they bought it.

    The sign of the result decides everything, which is why there is no tier ladder
    anywhere in this system:

    * owes money  -> an upgrade, applied when the payment clears
    * owes nothing -> a downgrade, applied at the end of the cycle they already paid for

    That falls out correctly for monthly-to-yearly, yearly-to-monthly and same-price
    sidegrades without anyone having to rank the plans.
    """
    from onboard.services import entitlements as entitlement_service

    if new_plan.pk == subscription.plan_id:
        raise PricingError("That is the plan you are already on.")

    snapshot = subscription.plan_snapshot or {}
    old_net = Decimal(str(snapshot.get("net_base_price") or subscription.plan.net_base_price))
    new_net = Decimal(str(new_plan.net_base_price))

    now = now or timezone.now()

    # A lapsed subscription has no unused time to credit and no cycle to schedule
    # against, so this stops being an upgrade-or-downgrade question and becomes a
    # straightforward purchase at full price.
    reactivating = subscription.status == "expired" or (
        subscription.end_date is not None and subscription.end_date < now
    )

    ratio = unused_ratio(subscription, now=now)
    credit_from_current = _money(old_net * ratio)

    # Add-ons follow the tenant across a plan change and are re-charged each cycle, so
    # the unused part of what they already paid for capacity is credited too.
    addon_recurring = entitlement_service.recurring_total(subscription.tenant_id)
    credit_from_addons = _money(addon_recurring * ratio)

    taxable = new_net - credit_from_current - credit_from_addons

    if taxable <= 0 and not reactivating:
        # Never a refund. They keep what they paid for until the cycle ends, and the
        # cheaper plan starts after that.
        change_type = SCHEDULED_CHANGE
        net = tax = total = Decimal("0.00")
        tax_percentage = Decimal(str(new_plan.tax_percentage or 0))
    else:
        net, tax, total, tax_percentage = _with_tax(max(taxable, Decimal("0")), new_plan)
        # Below the gateway minimum, charging is impossible and refusing is absurd for
        # a change the tenant wants. Apply it and move on.
        #
        # This is also the branch a lapsed tenant returning on a free plan takes:
        # nothing to charge, so activate immediately. Scheduling it would set an
        # effective date in the past.
        change_type = IMMEDIATE_FREE if total < Decimal("1.00") else PAID_CHANGE

    effective_at = (
        subscription.end_date if change_type == SCHEDULED_CHANGE else now
    )

    return Quote(
        net=net, tax=tax, total=total, tax_percentage=tax_percentage,
        detail={
            "change_type": change_type,
            "reactivation": reactivating,
            "effective_at": effective_at.isoformat(),
            "from_plan": snapshot.get("code") or subscription.plan.code,
            "to_plan": new_plan.code,
            "current_plan_net": old_net,
            "new_plan_net": new_net,
            "unused_ratio": ratio,
            "credit_from_current": credit_from_current,
            "credit_from_addons": credit_from_addons,
            "taxable_difference": _money(max(taxable, Decimal("0"))),
        },
    )


def credit_impact(subscription, new_plan, *, reason):
    """What a plan change does to the tenant's credits, before they commit to it.

    A tenant who is about to lose credits must be told before they click, not after.
    """
    from onboard.services import subscriptions as subscription_service
    from wallet.services import wallet as wallet_service

    wallet = wallet_service.for_tenant(subscription.tenant_id)
    carried, forfeited, code = subscription_service.rollover_amount(
        subscription, reason=reason
    )

    warnings = []
    if forfeited and code == "rollover_forfeit":
        warnings.append(
            f"Your {forfeited} remaining plan credits do not carry over on a {reason}."
        )
    elif forfeited and code == "rollover_cap_forfeit":
        warnings.append(
            f"{forfeited} credits are above this plan's rollover cap and will be lost."
        )

    return {
        "current_balance": wallet.balance,
        "allocated": wallet.allocated_balance,
        "purchased": wallet.purchased_balance,
        "carried_forward": carried,
        "forfeited": forfeited,
        "new_plan_allocation": new_plan.allocated_credits,
        "balance_after_change": carried + wallet.purchased_balance + new_plan.allocated_credits,
        "warnings": warnings,
    }


def _entitlement_for(plan, resource):
    from onboard.models import PlanEntitlement

    row = PlanEntitlement.objects.filter(
        plan=plan, resource=resource, is_deleted=False
    ).first()
    if row is None:
        raise PricingError(f"This plan has no {resource} allowance configured.")
    return row


def held_extra(tenant_id, resource):
    """Extra units of a resource the tenant has already bought."""
    from onboard.models import TenantEntitlement

    return (
        TenantEntitlement.objects.filter(
            tenant_id=tenant_id, resource=resource, is_deleted=False
        )
        .values_list("additional_quantity", flat=True)
        .first()
        or 0
    )


def quote_addon(subscription, resource, additional_units, *, now=None):
    """Price extra capacity for the remainder of the cycle.

    Two numbers matter and they are different. ``recurring_net`` is what this adds to
    every future renewal; ``total`` is what is charged right now, prorated for the
    part of this cycle that is left. Showing only the prorated figure would understate
    the commitment the tenant is taking on.

    Prices come from the live catalogue rather than the subscription snapshot: an
    add-on is a new agreement made today, and today's price is the one being offered.
    It is then frozen onto ``TenantEntitlement`` so a later rise does not re-price
    capacity they already hold.
    """
    additional_units = int(additional_units)
    if additional_units <= 0:
        raise PricingError("Quantity must be a positive whole number.")

    entitlement = _entitlement_for(subscription.plan, resource)
    if not entitlement.is_addon_available:
        raise PricingError(
            f"{entitlement.get_resource_display()} cannot be extended on this plan. "
            f"Upgrading is the way to get more."
        )

    held = held_extra(subscription.tenant_id, resource)
    wanted = held + additional_units
    if entitlement.max_additional is not None and wanted > entitlement.max_additional:
        raise PricingError(
            f"This plan allows at most {entitlement.max_additional} extra "
            f"{entitlement.get_resource_display()}; you already have {held}."
        )

    # Charged per whole block, rounded up. Billing a fraction of a block would mean
    # either giving away the remainder or invoicing a number the tenant cannot
    # reconcile against what they asked for. The delta is priced as
    # (price of the new total) - (price of what they already pay for), so buying in
    # two steps never costs more than buying the same amount at once.
    recurring = entitlement.price_for(wanted)
    added_recurring = recurring - entitlement.price_for(held)

    ratio = unused_ratio(subscription, now=now)
    net, tax, total, tax_percentage = _with_tax(added_recurring * ratio, subscription.plan)

    return Quote(
        net=net, tax=tax, total=total, tax_percentage=tax_percentage,
        detail={
            "resource": resource,
            "additional_units": additional_units,
            "quantity_after": wanted,
            "addon_step": entitlement.addon_step,
            "price_per_block": entitlement.price_per_additional,
            "recurring_net": recurring,
            "added_recurring_net": added_recurring,
            "unused_ratio": ratio,
        },
    )

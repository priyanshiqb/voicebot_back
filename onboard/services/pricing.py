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
def cycle_days_for(plan, *, anchor=None):
    """How many days one cycle of ``plan`` runs, from ``anchor``.

    Measured rather than assumed, because a monthly cycle is a *calendar* month:
    28, 29, 30 or 31 days depending on where it starts. Using a flat 30 would
    misprice a February upgrade against a March one.
    """
    from django.utils import timezone

    from onboard.models import Plan, Subscription

    anchor = anchor or timezone.now()
    delta = Subscription.CYCLE_DELTAS.get(
        plan.billing_cycle, Subscription.CYCLE_DELTAS[Plan.BillingCycleChoices.MONTHLY]
    )
    return Decimal((anchor + delta - anchor).days)


def bonus_days_for(value, plan, *, anchor=None, minutes=None):
    """How many days of ``plan`` ``value`` rupees buys, at the plan's daily rate.

    Both sides are **pre-tax**. Tax is charged on the new plan's full price
    separately, so mixing a tax-inclusive figure in here would buy days with money
    that was never the plan's to spend.

    ``minutes`` sets the daily rate on a minutes-based plan: a day of 2,000 minutes
    costs more than a day of 500, so unused value has to be converted against the
    quantity actually being bought rather than against a catalogue price the customer
    never agreed to.

    Returns 0 for a free plan rather than dividing by zero — a plan that costs
    nothing per day cannot be bought more of, so the remaining value simply has
    nowhere to go. That is a real edge (moving to a free tier) and the alternative
    is an infinite cycle.
    """
    if value <= 0:
        return Decimal("0.00")
    days = cycle_days_for(plan, anchor=anchor)
    if days <= 0:
        return Decimal("0.00")
    per_day = net_for(plan, minutes) / days
    if per_day <= 0:
        return Decimal("0.00")
    return (value / per_day).quantize(Decimal("0.01"))


# --------------------------------------------------------------------------- #
# Minutes-based plans
# --------------------------------------------------------------------------- #

#: Re-exported from the model, where the coercion helper lives beside it. A single
#: subscription cannot buy more minutes than this: a guard rather than a policy, in
#: the same spirit as MAX_TOP_UP_CREDITS.
from onboard.models.plan import (  # noqa: E402
    MAX_CHARGEABLE_TOTAL,
    MAX_SELECTABLE_MINUTES,
    coerce_minutes,
)


def quote_plan(plan, minutes):
    """Price ``minutes`` on ``plan``.

    The quote a signup, a renewal and the pricing page all run through, and the thing
    frozen onto ``Transaction.amount_breakdown`` when an order is raised.

    **Both per-minute figures are frozen into the detail**, not just referenced. The
    basis rate can be superseded between the moment a customer is quoted and the
    moment their payment clears; reading them back from here is what guarantees they
    are charged and granted what they were shown.
    """
    if not plan.is_minutes_based:
        raise PricingError("This plan is not sold by the minute.")

    minutes = coerce_minutes(minutes)
    if minutes is None:
        raise PricingError(
            f"Number of minutes must be a whole number between 1 and "
            f"{MAX_SELECTABLE_MINUTES:,}."
        )
    if not plan.contains_minutes(minutes):
        raise PricingError(
            f"{plan.name} sells {_band_label(plan)}, not {minutes:,} minutes."
        )

    price = plan.price_components(minutes)
    rate = plan.basis_rate()

    # A quote larger than the amount column can hold would fail at the moment the
    # order is written -- a 500 in the middle of a checkout rather than something the
    # customer can read. Only reachable through a misconfigured rate, which is exactly
    # when a clear refusal is worth most.
    if price.total > MAX_CHARGEABLE_TOTAL:
        raise PricingError(
            f"{minutes:,} minutes on {plan.name} comes to ₹{price.total}, which is "
            f"more than a single order can carry. Contact us about an invoice."
        )

    return Quote(
        net=price.net,
        tax=price.tax,
        total=price.total,
        tax_percentage=Decimal(str(plan.tax_percentage or 0)),
        detail={
            "selected_minutes": minutes,
            "credit_basis": plan.credit_basis,
            "price_per_minute_inr": rate.price_per_minute_inr,
            "credits_per_minute": plan.credits_per_minute(),
            "allocated_credits": plan.credits_for(minutes),
            "gross": price.gross,
            "discount": price.discount,
            "discount_percentage": Decimal(str(plan.discount_percentage or 0)),
        },
    )


def _band_label(plan):
    """A plan's band as something a customer can read, for an error message."""
    if plan.is_fixed_quantity:
        return f"exactly {plan.min_minutes:,} minutes"
    if plan.min_minutes is None and plan.max_minutes is None:
        return "any number of minutes"
    if plan.min_minutes is None:
        return f"up to {plan.max_minutes:,} minutes"
    if plan.max_minutes is None:
        return f"from {plan.min_minutes:,} minutes"
    return f"{plan.min_minutes:,}-{plan.max_minutes:,} minutes"


def net_for(plan, minutes=None):
    """Pre-tax rupees per cycle for ``plan`` at ``minutes``.

    The one place the legacy and minutes-based branches are joined for *pricing*, so
    every caller that needs "what does a cycle of this plan cost" gets the right
    answer without asking which generation of plan it is holding.
    """
    return plan.price_components(minutes).net


PAID_CHANGE = "paid_change"          # they owe something; applies when it clears
SCHEDULED_CHANGE = "scheduled_change"  # they owe nothing; applies at cycle end
IMMEDIATE_FREE = "immediate_free"    # they owe less than a gateway will take


def quote_plan_change(subscription, new_plan, *, minutes=None, now=None):
    """Price a mid-cycle move to another plan, or to a different quantity of one.

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

    **Buying more minutes of the same plan is a plan change too**, and the same sign
    test prices it. Only a move that changes neither the plan nor the quantity is
    refused, because that is the one request with nothing in it.

    ``minutes`` defaults to what the tenant already has, resolved into the new plan's
    band -- "the same usage, over there". On a fixed-quantity pack that is the pack's
    single value whatever they had before.
    """
    from onboard.services import entitlements as entitlement_service

    now = now or timezone.now()

    minutes = new_plan.resolve_minutes(
        minutes if minutes is not None else subscription.selected_minutes
    )

    if new_plan.pk == subscription.plan_id and minutes == subscription.selected_minutes:
        raise PricingError("That is the plan you are already on.")

    snapshot = subscription.plan_snapshot or {}
    old_net = Decimal(str(snapshot.get("net_base_price") or subscription.plan.net_base_price))
    new_net = net_for(new_plan, minutes)

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

    # The remaining value of what they already bought. Identical arithmetic to
    # "daily rate x days left" -- old_net x ratio is old_net/span x remaining.
    bonus_value = credit_from_current + credit_from_addons

    # Still computed, but no longer what is charged. It is the *signal*: owing
    # nothing means the tenant is moving somewhere cheaper than the time they have
    # left, which is a downgrade however the plans are ranked. Keeping the test here
    # is what lets the system stay free of a tier ladder.
    owes = new_net - bonus_value

    if owes <= 0 and not reactivating:
        # Never a refund, and never charged either. They keep what they paid for
        # until the cycle ends, and the cheaper plan starts after that.
        change_type = SCHEDULED_CHANGE
        net = tax = total = Decimal("0.00")
        tax_percentage = Decimal(str(new_plan.tax_percentage or 0))
        bonus_days = Decimal("0.00")
    else:
        # **Full list price, and the unused value buys days instead of a discount.**
        #
        # The alternative -- discounting the new plan by the unused value -- reduces
        # the taxable value of one supply using time already taxed on another, and
        # produces an invoice for an amount that matches no published price. This way
        # every upgrade invoices at the plan's own price, and the tenant is made whole
        # in the currency they actually care about: service.
        #
        # It is also neutral on the day it happens. Whatever date they move, the
        # total service bought is the same, because the value left over is converted
        # at the new plan's daily rate rather than written off.
        net, tax, total, tax_percentage = _with_tax(new_net, new_plan)
        bonus_days = bonus_days_for(bonus_value, new_plan, anchor=now, minutes=minutes)
        # A move to a zero-cost plan cannot be charged and cannot be paid in days.
        # Apply it outright rather than raising an order the gateway would refuse.
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
            # What is being moved to, in minutes and credits. `selected_minutes` is
            # read back at activation rather than recomputed -- the tenant paid for a
            # specific quantity, and the band could have moved since.
            "selected_minutes": minutes,
            "allocated_credits": new_plan.credits_for(minutes),
            "credits_delta": (
                new_plan.credits_for(minutes)
                - (subscription.allocated_credits or 0)
            ),
            "current_plan_net": old_net,
            "new_plan_net": new_net,
            "unused_ratio": ratio,
            "credit_from_current": credit_from_current,
            "credit_from_addons": credit_from_addons,
            # What the unused time is worth, and what it buys on the new plan.
            "bonus_value": bonus_value,
            "bonus_days": bonus_days,
            "new_plan_cycle_days": cycle_days_for(new_plan, anchor=now),
            # Kept under its old name so existing clients do not break, but it
            # is no longer what is charged -- it is what the tenant would have
            # owed under the discount model, and now only decides upgrade vs
            # scheduled downgrade.
            "taxable_difference": _money(max(owes, Decimal("0"))),
        },
    )


def credit_impact(subscription, new_plan, *, reason, minutes=None):
    """What a plan change does to the tenant's credits, before they commit to it.

    A tenant who is about to lose credits must be told before they click, not after.

    ``minutes`` decides the incoming allocation on a minutes-based plan, and defaults
    the same way ``quote_plan_change`` does. This is the number shown to a customer
    while they are still deciding, so it has to be the number they will actually get.
    """
    from onboard.services import subscriptions as subscription_service
    from wallet.services import wallet as wallet_service

    minutes = new_plan.resolve_minutes(
        minutes if minutes is not None else subscription.selected_minutes
    )
    new_allocation = new_plan.credits_for(minutes)

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
        "new_plan_allocation": new_allocation,
        "balance_after_change": carried + wallet.purchased_balance + new_allocation,
        "selected_minutes": minutes,
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

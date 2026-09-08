"""Is this plan safe to sell, and what is questionable about it?

A plan is three models and 84 settings. Two of the ways it can be incomplete are
silent, and this module exists because of them:

* **A missing entitlement row is not a limit of zero — it is no limit at all.**
  ``limit_for()`` returns ``None`` and ``check()`` returns without comparing anything.
  Nothing raises. The resource is simply unlimited, for as long as nobody notices.
* **A missing rate row cannot price a call.** ``resolve_rate()`` raises
  ``RateNotConfigured`` deliberately rather than charging zero, so the failure lands
  on a live call to a real customer.

Both are *blocking*: a plan with either cannot be activated. Everything else here is a
warning — accepted by the database, usually a mistake, and occasionally what somebody
actually meant.

Three callers, which is why this is a service and not a helper on the view: the
activate endpoint, the plan list's completeness column, and the console's own review
screen.
"""

from decimal import Decimal

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.constants.reset_period import ResetPeriod
from onboard.models import Plan
from onboard.services import plan_selection
from wallet.models import CreditRate

#: Every resource a plan must price. A plan missing one grants it without limit.
REQUIRED_RESOURCES = set(EntitlementResource.values)

#: Every call tier a plan must price. A plan missing one cannot bill that kind of call.
REQUIRED_OPERATIONS = set(CreditRate.Operation.values)


def _entitlements(plan):
    return {row.resource: row for row in plan.entitlements.filter(is_deleted=False)}


def _rates(plan):
    return {
        row.operation: row
        for row in plan.credit_rates.filter(is_deleted=False, effective_to__isnull=True)
    }


def blocking_problems(plan, entitlements=None, rates=None, ladder=None):
    """What stops this plan being activated. Empty means it may go live.

    Rows are passed in by callers that already loaded them — the plan list checks
    every plan at once and must not re-query per row.
    """
    entitlements = _entitlements(plan) if entitlements is None else entitlements
    rates = _rates(plan) if rates is None else rates

    problems = []

    missing_resources = sorted(REQUIRED_RESOURCES - set(entitlements))
    if missing_resources:
        problems.append({
            "code": "MISSING_ENTITLEMENTS",
            "resources": missing_resources,
            "message": (
                "No allowance is configured for "
                + ", ".join(r.replace("_", " ") for r in missing_resources)
                + ". A missing allowance is not a limit of zero — it is no limit at "
                "all, and nothing would report it."
            ),
        })

    missing_operations = sorted(REQUIRED_OPERATIONS - set(rates))
    if missing_operations:
        problems.append({
            "code": "MISSING_RATES",
            "operations": missing_operations,
            "message": (
                "No credit rate is configured for "
                + ", ".join(o.replace("_", " ") for o in missing_operations)
                + ". Calls on that tier cannot be priced and would fail at the point "
                "of the call."
            ),
        })

    problems.extend(_minutes_problems(plan, rates, ladder=ladder))
    return problems


def _minutes_problems(plan, rates, *, ladder=None):
    """What stops a plan being sold by the minute -- which is now every plan."""
    # **Every plan needs a basis, not only one that is trying to be minutes-based.**
    #
    # This used to return early for a plan with no band and no basis, so both
    # generations could be activated side by side while the migration ran. With
    # `Plan.base_price` and `Plan.allocated_credits` gone there is nothing left for
    # such a plan to be priced or granted from -- it would activate, appear on the
    # pricing page and sell for nothing. The legacy model is retired, so the gate says
    # so.
    problems = []

    if not plan.credit_basis:
        problems.append({
            "code": "CREDIT_BASIS_UNSET",
            "message": (
                "This plan has no tier to sell it at. Both the price and the credit "
                "grant come from the basis tier's rate row, so without one the plan "
                "can neither be priced nor grant anything."
            ),
        })
        return problems

    basis = rates.get(plan.credit_basis)
    if basis is None:
        problems.append({
            "code": "CREDIT_BASIS_RATE_MISSING",
            "operation": plan.credit_basis,
            "message": (
                f"The plan is sold at {plan.credit_basis.replace('_', ' ')}, but has "
                f"no open rate for it. Nothing can price a minute or grant a credit."
            ),
        })
        return problems

    if basis.unit == CreditRate.Unit.PER_EVENT:
        problems.append({
            "code": "CREDIT_BASIS_PER_EVENT",
            "operation": plan.credit_basis,
            "message": (
                "The basis tier is priced per call, which has no per-minute meaning. "
                "A plan sold by the minute needs a per-minute or per-second basis."
            ),
        })

    if basis.price_per_minute_inr is None:
        problems.append({
            "code": "CREDIT_BASIS_NO_INR_PRICE",
            "operation": plan.credit_basis,
            "message": (
                "The basis tier has no rupees-per-minute price, so a sale cannot be "
                "priced at all. Credits alone do not make a plan sellable."
            ),
        })
    elif basis.price_per_minute_inr > 0 and plan.renews_without_payment:
        # **This used to be a database constraint on `base_price`.** With the price on
        # the rate row it is a cross-table question, so no column can answer it and the
        # gate has to. A priced plan that renews itself for free would silently stop
        # billing -- the tenant keeps the service and no order is ever raised again.
        problems.append({
            "code": "RENEWS_FREE_BUT_PRICED",
            "message": (
                f"This plan renews without payment but is sold at "
                f"₹{basis.price_per_minute_inr}/min. It would give the product away "
                f"after the first cycle. Only a plan that costs nothing may renew "
                f"itself."
            ),
        })
    elif basis.price_per_minute_inr <= 0 and plan.max_minutes is None:
        # A free plan open at the top would grant unbounded credits for nothing.
        problems.append({
            "code": "FREE_MINUTES_PLAN_UNCAPPED",
            "message": (
                "This plan is free per minute and has no maximum, so a customer "
                "could ask for any number of minutes and be granted the credits for "
                "them at no charge. Set a maximum."
            ),
        })

    # --- the rest of the ladder ------------------------------------------
    # Cross-row, so it cannot be a database constraint. Loaded once and passed in by
    # callers checking several plans at a time.
    if ladder is None:
        ladder = plan_selection.ladder(
            billing_cycle=plan.billing_cycle, currency=plan.currency
        )
    # The plan being checked stands in for its saved self: it may be a draft that is
    # not in the ladder yet, or an edited copy of one that is.
    others = [p for p in ladder if p.pk != plan.pk]
    problems.extend(plan_selection.ladder_problems(others + [plan]))

    return problems


def warnings(plan, entitlements=None, rates=None):
    """Things the database accepts that are nearly always mistakes.

    Deliberately *not* blocking. Every one of these is a legitimate configuration in
    some business, and a rule that refuses them would be wrong more often than the
    mistake it prevents — but silently accepting them is how a plan ships giving
    storage away for nothing.
    """
    entitlements = _entitlements(plan) if entitlements is None else entitlements
    rates = _rates(plan) if rates is None else rates

    found = []

    def warn(code, message, **extra):
        found.append({"code": code, "message": message, **extra})

    # --- the plan itself ---------------------------------------------------
    if (plan.discount_percentage or 0) > 100:
        warn(
            "DISCOUNT_OVER_100",
            f"A discount of {plan.discount_percentage}% produces a negative price. "
            "The column accepts up to 999.99; 100 is the real ceiling.",
        )

    basis = rates.get(plan.credit_basis) if plan.credit_basis else None
    is_paid = bool(basis and (basis.price_per_minute_inr or 0) > 0)

    if is_paid and not (plan.cost_per_additional_credit or 0):
        warn(
            "TOP_UPS_DISABLED",
            "Top-ups are disabled (₹0.00 per credit), so upgrading is the only way "
            "for a subscriber to get more credits.",
        )

    if not plan.sort_order:
        warn(
            "NO_SORT_ORDER",
            "Sort order is 0, so this plan ties with every other plan at 0 and the "
            "pricing page will order them arbitrarily.",
        )

    # --- capacity ----------------------------------------------------------
    for resource, row in sorted(entitlements.items()):
        noun = resource.replace("_", " ")

        if row.is_addon_available and not (row.price_per_additional or 0):
            warn(
                "ADDON_PRICED_AT_ZERO",
                f"Extra {noun} is purchasable at ₹0.00 — subscribers can add as much "
                "as the cap allows for nothing.",
                resource=resource,
            )

        if row.is_addon_available and row.max_additional is None:
            warn(
                "ADDON_UNCAPPED",
                f"Extra {noun} has no ceiling, so a subscriber on this plan can "
                "assemble a larger footprint than the plan above it at add-on prices.",
                resource=resource,
            )

        if (
            resource == EntitlementResource.CAMPAIGN_MONTHLY
            and row.reset_period == ResetPeriod.NONE
        ):
            warn(
                "CAMPAIGN_NOT_WINDOWED",
                "Campaigns reset period is “never”, which makes the allowance a "
                "standing total — deleting a campaign would hand it back, even after "
                "the calls went out.",
                resource=resource,
            )

        if resource == EntitlementResource.CAMPAIGN_MONTHLY and row.is_addon_available:
            warn(
                "CAMPAIGNS_SOLD_AS_ADDON",
                "Campaigns are purchasable. Add-ons are prorated by time remaining, "
                "so a subscriber buying late in the window pays a fraction of the "
                "price for the whole allowance.",
                resource=resource,
            )

    # --- rate card ---------------------------------------------------------
    if is_paid:
        free_tiers = sorted(o for o, r in rates.items() if not r.credits_per_unit)
        if free_tiers:
            warn(
                "TIER_PRICED_AT_ZERO",
                "Calls are free on "
                + ", ".join(o.replace("_", " ") for o in free_tiers)
                + " — those minutes cost the platform money and earn nothing.",
                operations=free_tiers,
            )

    # --- minutes ----------------------------------------------------------
    if plan.credit_basis:
        ratios = plan_selection.rupees_per_credit(plan)
        if len(set(ratios.values())) > 1:
            warn(
                "RUPEES_PER_CREDIT_INCONSISTENT",
                "This plan sells credits at "
                + ", ".join(f"₹{v} on {k.replace('_', ' ')}" for k, v in sorted(ratios.items()))
                + ". Which price a customer gets depends on the basis tier, which "
                "they never chose. Deliberate subsidy is fine; an accident here is "
                "invisible.",
                ratios={k: str(v) for k, v in ratios.items()},
            )

        unpriced = sorted(
            operation for operation, row in rates.items()
            if row.price_per_minute_inr is None
        )
        if unpriced and len(unpriced) < len(rates):
            warn(
                "RATE_MISSING_INR_PRICE",
                "No rupees-per-minute price on "
                + ", ".join(o.replace("_", " ") for o in unpriced)
                + ". The pricing page cannot tell a customer what a minute of those "
                "tiers costs them.",
                operations=unpriced,
            )

    # --- the yearly mispricing --------------------------------------------
    if plan.billing_cycle == Plan.BillingCycleChoices.YEARLY:
        priced = {
            resource: row.price_per_additional
            for resource, row in entitlements.items()
            if row.is_addon_available and row.price_per_additional
        }
        if priced and _looks_like_monthly_pricing(plan, priced):
            warn(
                "YEARLY_ADDONS_LOOK_MONTHLY",
                "Add-on prices on this yearly plan match a monthly plan's. Block "
                "prices are charged **per cycle**, so the same figure buys a year "
                "here and a month there — a twelfth of the price for the same "
                "capacity.",
                resources=sorted(priced),
            )

    return found


def _looks_like_monthly_pricing(plan, priced):
    """Whether a yearly plan's add-on prices match some monthly plan's, row for row.

    A heuristic, and only ever a warning. Matching a monthly plan exactly is far more
    likely to be a copy-paste than a decision — but a business may genuinely price
    them the same, so this must not block.
    """
    monthly_prices = (
        Plan.objects.filter(
            billing_cycle=Plan.BillingCycleChoices.MONTHLY, is_deleted=False
        )
        .exclude(pk=plan.pk)
        .values_list("entitlements__resource", "entitlements__price_per_additional")
    )
    monthly = {}
    for resource, price in monthly_prices:
        if resource and price:
            monthly.setdefault(resource, set()).add(Decimal(str(price)))

    matches = [
        resource
        for resource, price in priced.items()
        if Decimal(str(price)) in monthly.get(resource, set())
    ]
    return len(matches) == len(priced)


def report(plan):
    """The whole picture, in one payload, for the console's review screen.

    ``can_activate`` is the answer the activate endpoint acts on. Warnings never
    change it.
    """
    entitlements = _entitlements(plan)
    rates = _rates(plan)
    problems = blocking_problems(plan, entitlements, rates)

    ladder = plan_selection.ladder(
        billing_cycle=plan.billing_cycle, currency=plan.currency
    )
    others = [p for p in ladder if p.pk != plan.pk]

    return {
        "status": plan.status,
        "can_activate": not problems,
        # The ladder this plan is joining, not the plan alone. A band is only correct
        # relative to its neighbours, and a review screen that shows one without the
        # others is asking somebody to hold the catalogue in their head.
        "ladder": {
            "bands": [
                {
                    "code": p.code,
                    "name": p.name,
                    "min_minutes": p.min_minutes,
                    "max_minutes": p.max_minutes,
                    "is_fixed_quantity": p.is_fixed_quantity,
                    "is_this_plan": p.pk == plan.pk,
                }
                for p in sorted(
                    others + ([plan] if plan.credit_basis else []),
                    key=lambda p: (p.min_minutes is not None, p.min_minutes or 0),
                )
            ],
            "boundaries": plan_selection.boundary_prices(others + [plan]),
            "warnings": plan_selection.ladder_warnings(others + [plan]),
        },
        "entitlements_configured": len(entitlements),
        "entitlements_required": len(REQUIRED_RESOURCES),
        "rates_configured": len(rates),
        "rates_required": len(REQUIRED_OPERATIONS),
        "blocking": problems,
        "warnings": warnings(plan, entitlements, rates),
    }

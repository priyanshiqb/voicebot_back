"""Which plan sells a given number of minutes.

The customer names a quantity; this names the plan. For that to work at all,
``minutes -> plan`` has to be a **function** — two plans claiming 1,200 minutes would
give the frontend a coin-flip between two prices, and a customer who reloaded the page
could be quoted a different number for the same request.

So the ladder is validated as well as read, and both halves live here:

* :func:`for_minutes` and :func:`ladder` answer the question
* :func:`ladder_problems` is what the activation gate uses to refuse a plan that would
  make the question unanswerable

**Legacy plans are not in the ladder.** ``is_minutes_based`` is false for them, so they
never collide with a band and never get selected by quantity. That is what lets both
generations of plan be active in the same catalogue while the frontend migrates.
"""

import logging
from decimal import Decimal

from django.db.models import Prefetch

from onboard.models.plan import coerce_minutes

logger = logging.getLogger(__name__)


def _default_cycle():
    from onboard.models import Plan

    return Plan.BillingCycleChoices.MONTHLY


# --------------------------------------------------------------------------- #
# Reading the ladder
# --------------------------------------------------------------------------- #

def ladder(*, billing_cycle=None, currency="INR"):
    """Every active plan sold by the minute, in band order.

    Ordered with the open-below plan first, then by ascending minimum — the order a
    pricing page renders, and the order the gap check needs.

    Rates are prefetched onto ``open_rates``, which ``Plan.basis_rate()`` reads, so a
    whole catalogue costs two queries rather than one per plan.
    """
    from onboard.models import Plan
    from wallet.models import CreditRate

    plans = (
        Plan.objects.filter(
            status=Plan.Status.ACTIVE,
            is_deleted=False,
            billing_cycle=billing_cycle or _default_cycle(),
            currency=currency,
            credit_basis__isnull=False,
        )
        .prefetch_related(
            Prefetch(
                "credit_rates",
                queryset=CreditRate.objects.filter(
                    is_deleted=False, effective_to__isnull=True
                ),
                to_attr="open_rates",
            )
        )
    )
    # `is_minutes_based` needs the rate row, so the last filter is in Python. A plan
    # with a basis but no rupee price on it is not sellable by the minute and must
    # not appear in a ladder that is about to be checked for gaps.
    return sorted((p for p in plans if p.is_minutes_based), key=_band_key)


def _band_key(plan):
    """Sort key placing the open-below band first, then ascending minimum."""
    return (0 if plan.min_minutes is None else 1, plan.min_minutes or 0)


def for_minutes(minutes, *, billing_cycle=None, currency="INR", plans=None):
    """The plan that sells ``minutes``, and the quantity it sells them as.

    Returns ``(plan, sold_minutes)``, or ``(None, None)`` when nothing covers the
    request. Two steps:

    1. **exact containment** — the one band that contains the quantity;
    2. **round up** — the plan that sells the smallest quantity at or above it.

    Step 2 exists for ladders built from fixed-quantity packs, which have gaps by
    construction: a request for 1,200 against packs of 1,000 / 5,000 / 10,000 has to
    resolve to the 5,000 pack or the customer cannot buy at all. It can only ever
    land on a pack, because a gap between two *ranged* bands is refused at activation
    — so step 1 never fails inside the continuous part of a ladder.

    **``sold_minutes`` is the number that must be priced, granted and stored.** What
    the customer asked for is worth reporting back, but it is not what they bought.
    """
    minutes = coerce_minutes(minutes)
    if minutes is None:
        return None, None

    plans = ladder(billing_cycle=billing_cycle, currency=currency) if plans is None else plans

    for plan in plans:
        if plan.contains_minutes(minutes):
            return plan, minutes

    # Nothing contains it. Whoever sells the smallest quantity that still covers the
    # requirement is the honest answer -- never a larger pack than necessary.
    covering = [
        plan for plan in plans
        if plan.max_minutes is None or plan.max_minutes >= minutes
    ]
    if not covering:
        return None, None

    plan = min(covering, key=lambda p: p.resolve_minutes(minutes))
    return plan, plan.resolve_minutes(minutes)


def nearest_bands(minutes, *, billing_cycle=None, currency="INR", plans=None):
    """What sits either side of a quantity nothing sells.

    So a 404 can say "our largest pack is 10,000 minutes" instead of "not found".
    """
    minutes = coerce_minutes(minutes)
    if minutes is None:
        return {"below": None, "above": None}

    plans = ladder(billing_cycle=billing_cycle, currency=currency) if plans is None else plans

    below = [p for p in plans if p.max_minutes is not None and p.max_minutes < minutes]
    above = [p for p in plans if p.min_minutes is not None and p.min_minutes > minutes]

    return {
        "below": _band_summary(max(below, key=lambda p: p.max_minutes)) if below else None,
        "above": _band_summary(min(above, key=lambda p: p.min_minutes)) if above else None,
    }


def _band_summary(plan):
    return {
        "id": str(plan.pk),
        "code": plan.code,
        "name": plan.name,
        "min_minutes": plan.min_minutes,
        "max_minutes": plan.max_minutes,
    }


# --------------------------------------------------------------------------- #
# Validating the ladder
# --------------------------------------------------------------------------- #

def ladder_problems(plans):
    """What stops this ladder answering a quantity unambiguously. ``[]`` is clean.

    Two failures, and they are not symmetrical:

    * **An overlap** makes the answer ambiguous, always. Reported whatever kind of
      bands are involved.
    * **A gap** is only a problem *between two ranged bands*, where it is an accident:
      round-up would silently sell somebody a bigger band than they asked for. Beside
      a fixed-quantity pack a gap is the entire point — packs of 1,000 and 5,000 are
      meant to have nothing between them.

    Shared by the activation gate, the ops review screen and the readiness audit.
    """
    problems = []
    ordered = sorted(plans, key=_band_key)

    for i, first in enumerate(ordered):
        for second in ordered[i + 1:]:
            if _overlaps(first, second):
                problems.append({
                    "code": "BAND_OVERLAPS",
                    "plans": [first.code, second.code],
                    "message": (
                        f"{first.name} ({_label(first)}) and {second.name} "
                        f"({_label(second)}) both sell the same quantities. A "
                        f"customer asking for one of them would get two prices."
                    ),
                })

    for first, second in zip(ordered, ordered[1:]):
        if first.is_fixed_quantity or second.is_fixed_quantity:
            continue
        if first.max_minutes is None or second.min_minutes is None:
            continue
        if second.min_minutes > first.max_minutes + 1:
            problems.append({
                "code": "BAND_GAP",
                "plans": [first.code, second.code],
                "from_minutes": first.max_minutes + 1,
                "to_minutes": second.min_minutes - 1,
                "message": (
                    f"Nothing sells {first.max_minutes + 1:,}-"
                    f"{second.min_minutes - 1:,} minutes. A customer asking for that "
                    f"would be rounded up into {second.name} and charged for more "
                    f"than they wanted."
                ),
            })

    return problems


def _overlaps(first, second):
    """Whether two bands claim any quantity in common. Nulls are infinities."""
    low = max(first.min_minutes or 0, second.min_minutes or 0)
    highs = [b for b in (first.max_minutes, second.max_minutes) if b is not None]
    high = min(highs) if highs else None
    return high is None or low <= high


def _label(plan):
    if plan.is_fixed_quantity:
        return f"exactly {plan.min_minutes:,}"
    low = f"{plan.min_minutes:,}" if plan.min_minutes is not None else "any"
    high = f"{plan.max_minutes:,}" if plan.max_minutes is not None else "any"
    return f"{low}-{high}"


def ladder_warnings(plans):
    """Configurations the ladder accepts that are usually not what was meant."""
    found = []
    if not plans:
        return found

    if not any(p.min_minutes is None for p in plans):
        smallest = min(p.min_minutes for p in plans)
        found.append({
            "code": "LADDER_OPEN_BOTTOM_MISSING",
            "message": (
                f"Nothing sells below {smallest:,} minutes, so a customer who needs "
                f"less than that cannot buy at all."
            ),
        })

    if not any(p.max_minutes is None for p in plans):
        largest = max(p.max_minutes for p in plans)
        found.append({
            "code": "LADDER_OPEN_TOP_MISSING",
            "message": (
                f"Nothing sells above {largest:,} minutes. Larger customers get no "
                f"answer at all — which may be deliberate, if they are meant to talk "
                f"to sales."
            ),
        })

    return found


def boundary_prices(plans):
    """What a customer pays either side of every band boundary.

    **Reported as information, not as a warning, and that is a deliberate
    correction.** A boundary where buying *more* minutes costs *less* money looks
    like a misconfiguration, and an earlier draft of this flagged it. It is not: with
    per-minute pricing, no cliff at a boundary requires

        rate_next >= rate_prev * max / (max + 1)

    which is essentially ``rate_next >= rate_prev`` -- a ladder that never gets
    cheaper per minute as volume rises, which is a ladder with no volume discount at
    all. So every legitimate volume ladder has a cliff at every boundary, and warning
    about it would fire on all of them. A warning that is always on is noise, and
    noise is how the warnings that matter get ignored.

    What commercial actually needs is to *see* the four numbers and decide whether
    the dead zone at the top of each band is acceptable. That is what this returns.
    """
    from onboard.services import pricing

    rows = []
    ordered = sorted(plans, key=_band_key)

    for first, second in zip(ordered, ordered[1:]):
        if first.max_minutes is None or second.min_minutes is None:
            continue
        try:
            before = pricing.net_for(first, first.max_minutes)
            after = pricing.net_for(second, second.min_minutes)
        except Exception:  # noqa: BLE001
            logger.warning("could not price a band boundary", exc_info=True)
            continue

        rows.append({
            "from_plan": first.code,
            "to_plan": second.code,
            "at_minutes": first.max_minutes,
            "price_below": str(before),
            "next_minutes": second.min_minutes,
            "price_above": str(after),
            # True on essentially every volume ladder. Surfaced so the console can
            # show where a customer is better off buying one more minute, not so
            # anybody is asked to fix it.
            "cheaper_above": after < before,
        })

    return rows


def rupees_per_credit(plan):
    """What each of a plan's tiers charges per credit, as ``{operation: Decimal}``.

    One number across all three means the plan sells credits at a single price and
    the tier is purely how the customer chooses to spend them. Three different
    numbers means the price a customer gets depends on a basis they never chose --
    legal, occasionally intended, never worth leaving unreported.

    Tiers with no rupee price or no credit cost are omitted rather than reported as
    zero: nothing can be concluded from them.
    """
    rates = getattr(plan, "open_rates", None)
    if rates is None:
        rates = plan.credit_rates.filter(is_deleted=False, effective_to__isnull=True)

    ratios = {}
    for rate in rates:
        if rate.price_per_minute_inr is None or not rate.credits_per_unit:
            continue
        ratios[rate.operation] = (
            Decimal(str(rate.price_per_minute_inr)) / Decimal(rate.credits_per_unit)
        ).quantize(Decimal("0.0001"))
    return ratios

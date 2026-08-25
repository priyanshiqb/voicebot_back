"""How often an allowance refills.

Most allowances are **standing totals** — three agents means three agents, and
deleting one gives the seat back. Those have no reset period at all.

A few are **windowed**: what is limited is an *act*, not a holding. Starting a
campaign consumes something whether or not the campaign still exists afterwards, so
the allowance refills on a clock instead of when a row is deleted.

Which of the two an allowance is, and how fast a windowed one refills, is a
commercial decision — so it lives on ``PlanEntitlement`` rather than in code.
"""

from datetime import timedelta

from dateutil.relativedelta import relativedelta
from django.db import models


class ResetPeriod(models.TextChoices):
    """How often a windowed allowance goes back to zero."""

    NONE = "none", "Never — a standing total"
    DAILY = "daily", "Daily"
    WEEKLY = "weekly", "Weekly"
    MONTHLY = "monthly", "Monthly"
    QUARTERLY = "quarterly", "Quarterly"
    HALF_YEARLY = "half_yearly", "Every six months"
    YEARLY = "yearly", "Yearly"


#: Periods measured in fixed-length spans.
_DAYS = {
    ResetPeriod.DAILY: 1,
    ResetPeriod.WEEKLY: 7,
}

#: Periods measured in calendar months, so a window that opens on the 31st closes on
#: the 30th or the 28th rather than drifting three days every quarter.
_MONTHS = {
    ResetPeriod.MONTHLY: 1,
    ResetPeriod.QUARTERLY: 3,
    ResetPeriod.HALF_YEARLY: 6,
    ResetPeriod.YEARLY: 12,
}


def is_windowed(period):
    return bool(period) and period != ResetPeriod.NONE


def window_start(period, anchor, now):
    """When the allowance containing ``now`` last refilled.

    **Anchored to the tenant's own subscription, not to the calendar.** Anchoring to
    the 1st of the month is what let a customer whose cycle ran 5 Aug — 4 Sep collect
    two months of allowance inside one billing cycle, and twelve inside a yearly one.
    Counting from the subscription means every window sits wholly inside a cycle, and
    a period longer than the cycle spans cycles the way it should.

    ``anchor`` should be a date that does *not* move at renewal — renewal extends the
    same subscription and pushes ``start_date`` forward, which would reset a quarterly
    window every month.

    Returns ``None`` when the allowance is a standing total.
    """
    if not is_windowed(period):
        return None
    if anchor is None or anchor >= now:
        return anchor

    if period in _DAYS:
        span = timedelta(days=_DAYS[period])
        return anchor + (now - anchor) // span * span

    months = _MONTHS[period]
    # Estimate the elapsed window count, then walk to the exact one. The estimate is
    # right or one out; the two loops cost at most a step each and remove any doubt
    # about month-length arithmetic.
    elapsed = (now.year - anchor.year) * 12 + (now.month - anchor.month)
    count = max(elapsed // months, 0)

    while anchor + relativedelta(months=months * count) > now:
        count -= 1
    while anchor + relativedelta(months=months * (count + 1)) <= now:
        count += 1

    return anchor + relativedelta(months=months * count)

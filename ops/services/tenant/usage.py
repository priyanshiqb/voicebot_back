"""What a tenant has actually done lately. Three queries, thirty days.

Deliberately small. The umbrella plan builds two analytics endpoints for the platform
as a whole (milestone 7); this is the "is this account alive, and on what?" block on
one tenant's screen, and it stops where that begins.
"""

from datetime import timedelta

from django.db.models import Count, Sum
from django.utils import timezone

from core.models.agent_call_log import AgentCallLog
from core.models.campaign import Campaign
from onboard.services.entitlements import campaign_consumes_allowance
from wallet.models import CreditLedger

WINDOW_DAYS = 30

#: The two entry types that mean credits left the wallet for work done. A forfeit is
#: not usage — the tenant lost credits without getting anything, and counting it here
#: would make a lapsed cycle look like a busy month.
CONSUMPTION_TYPES = [
    CreditLedger.EntryType.USAGE_DEDUCTION,
    CreditLedger.EntryType.USAGE_ADJUSTMENT,
]


def report(tenant_id, *, days=WINDOW_DAYS):
    """Calls, minutes, credits burned and live campaigns. Three queries."""
    since = timezone.now() - timedelta(days=days)

    by_tier = {}
    calls = 0
    seconds = 0
    for row in (
        AgentCallLog.objects.filter(tenant_id=tenant_id, created_at__gte=since)
        .values("service_tier")
        .annotate(calls=Count("id"), seconds=Sum("duration_seconds"))
    ):
        tier_seconds = row["seconds"] or 0
        by_tier[row["service_tier"] or "unknown"] = {
            "calls": row["calls"],
            "minutes": round(tier_seconds / 60, 1),
        }
        calls += row["calls"]
        seconds += tier_seconds

    # Negative in the ledger, because credits left. Negated here so the screen shows
    # "burned 400" rather than "burned -400".
    burned = (
        CreditLedger.all_objects.filter(
            tenant_id=tenant_id,
            entry_type__in=CONSUMPTION_TYPES,
            created_at__gte=since,
        ).aggregate(total=Sum("amount"))["total"]
        or 0
    )

    # The same Q the allowance checker uses. If this counted campaigns differently,
    # the 360 would show 4 active beside an entitlement row reading 5 / 5.
    campaigns_active = (
        Campaign.all_objects.filter(tenant_id=tenant_id)
        .filter(campaign_consumes_allowance())
        .count()
    )

    return {
        "window_days": days,
        "since": since,
        "calls": calls,
        "minutes": round(seconds / 60, 1),
        "credits_burned": abs(burned),
        "campaigns_active": campaigns_active,
        "by_tier": by_tier,
    }

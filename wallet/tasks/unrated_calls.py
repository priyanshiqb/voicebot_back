"""Sweep for calls that completed but were never charged."""

import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from core.tasks import ReportedTask

logger = logging.getLogger(__name__)

#: Ignore very recent calls — a log posted seconds ago may simply not have been
#: charged yet, because the request that charges it is still in flight.
GRACE_MINUTES = 15


@shared_task(base=ReportedTask, name="wallet.tasks.report_unrated_calls")
def report_unrated_calls(hours=24):
    """Find completed calls with no matching usage deduction.

    Revenue quietly not collected, and the only way to notice it. Three causes, all
    worth knowing about:

    * the wallet service raised and the charge hook swallowed it,
    * the plan had no rate for the call's tier,
    * the tenant could not afford it while enforcement was off — expected during the
      metering period, and the reason this reports rather than alerts.

    **Reports only.** Charging retroactively would bill a tenant days later at
    whatever rate is stamped now, which is not the rate that applied when the call
    happened.
    """
    from core.models.agent_call_log import AgentCallLog
    from wallet.models import CreditLedger

    since = timezone.now() - timedelta(hours=hours)
    cutoff = timezone.now() - timedelta(minutes=GRACE_MINUTES)

    charged = set(
        CreditLedger.all_objects.filter(
            entry_type=CreditLedger.EntryType.USAGE_DEDUCTION,
            created_at__gte=since - timedelta(hours=1),
        ).values_list("reference_id", flat=True)
    )

    rows = list(
        AgentCallLog.objects.filter(created_at__gte=since, created_at__lte=cutoff)
        .exclude(Q(duration_seconds=0) | Q(duration_seconds__isnull=True))
        .exclude(provider_call_id__in=charged)
        .values_list("provider_call_id", "tenant_id", "duration_seconds")
    )

    for provider_call_id, tenant_id, duration in rows:
        logger.error(
            "unrated call: %s (tenant=%s, %ss) — completed but never charged",
            provider_call_id, tenant_id, duration,
            extra={"provider_call_id": provider_call_id, "tenant_id": str(tenant_id)},
        )

    if rows:
        logger.error("%s unrated call(s) in the last %sh", len(rows), hours)
    return len(rows)

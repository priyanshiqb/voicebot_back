"""Sweep for calls that completed but were never charged."""

import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from core.tasks import ReportedTask

logger = logging.getLogger(__name__)

#: How far back the sweep and every console that asks the same question look.
#: One definition: the nightly task, the ops tenant-health panel and the wallet
#: admin action all bound their window by this, so none of them can report a
#: different answer for the same tenant at the same moment.
DEFAULT_WINDOW_HOURS = 24

#: Ignore very recent calls — a log posted seconds ago may simply not have been
#: charged yet, because the request that charges it is still in flight.
GRACE_MINUTES = 15


def unrated_for(tenant_id=None, *, hours=24):
    """Completed calls with no matching usage deduction, newest first.

    **One rule, two callers.** The nightly sweep below runs it across every tenant;
    the admin console asks the same question about one tenant while support has them
    on the phone. A second copy would let the screen and the job disagree about
    whether revenue went uncollected, which is the one thing neither may do.

    ``tenant_id`` narrows it to one tenant; ``None`` is the whole platform. The grace
    window and the charged-set lookback are the same either way, so a call that the
    sweep considers still in flight is not reported as a problem on the console
    thirty seconds later.

    Returns tuples of ``(provider_call_id, tenant_id, duration_seconds, created_at)``.
    """
    from core.models.agent_call_log import AgentCallLog
    from wallet.models import CreditLedger

    since = timezone.now() - timedelta(hours=hours)
    cutoff = timezone.now() - timedelta(minutes=GRACE_MINUTES)

    charged = CreditLedger.all_objects.filter(
        entry_type=CreditLedger.EntryType.USAGE_DEDUCTION,
        created_at__gte=since - timedelta(hours=1),
    )
    if tenant_id is not None:
        charged = charged.filter(tenant_id=tenant_id)

    rows = AgentCallLog.objects.filter(created_at__gte=since, created_at__lte=cutoff)
    if tenant_id is not None:
        rows = rows.filter(tenant_id=tenant_id)

    return list(
        rows.exclude(Q(duration_seconds=0) | Q(duration_seconds__isnull=True))
        .exclude(provider_call_id__in=set(charged.values_list("reference_id", flat=True)))
        .order_by("-created_at")
        .values_list("provider_call_id", "tenant_id", "duration_seconds", "created_at")
    )


@shared_task(base=ReportedTask, name="wallet.tasks.report_unrated_calls")
def report_unrated_calls(hours=DEFAULT_WINDOW_HOURS):
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
    rows = unrated_for(hours=hours)

    for provider_call_id, tenant_id, duration, _created_at in rows:
        logger.error(
            "unrated call: %s (tenant=%s, %ss) — completed but never charged",
            provider_call_id, tenant_id, duration,
            extra={"provider_call_id": provider_call_id, "tenant_id": str(tenant_id)},
        )

    if rows:
        logger.error("%s unrated call(s) in the last %sh", len(rows), hours)
    return len(rows)

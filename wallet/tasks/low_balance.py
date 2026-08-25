"""Warning tenants before their credits run out."""

import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import F
from django.utils import timezone

from core.tasks import ReportedTask

logger = logging.getLogger(__name__)

#: Do not re-warn more often than this. A tenant hovering near their threshold would
#: otherwise be told the same thing every day.
QUIET_DAYS = 3


@shared_task(base=ReportedTask, name="wallet.tasks.send_low_balance_alerts")
def send_low_balance_alerts():
    """Notify tenants at or below their plan's low-balance threshold.

    Worth doing before enforcement is switched on, not after: while credits do not
    yet block anything, this is the only signal a tenant gets that their usage is
    outrunning their plan.
    """
    from onboard.services import billing_notifications
    from wallet.models import Wallet

    cutoff = timezone.now() - timedelta(days=QUIET_DAYS)
    candidates = Wallet.objects.filter(
        low_balance_threshold__gt=0,
        balance__lte=F("low_balance_threshold"),
        is_frozen=False,
    ).exclude(low_balance_notified_at__gt=cutoff)

    sent = 0
    for wallet in candidates:
        try:
            if billing_notifications.notify_low_balance(wallet):
                sent += 1
            Wallet.objects.filter(pk=wallet.pk).update(
                low_balance_notified_at=timezone.now()
            )
        except Exception:
            logger.exception("low-balance alert failed for wallet %s", wallet.pk)

    if sent:
        logger.info("sent %s low-balance alert(s)", sent)
    return sent

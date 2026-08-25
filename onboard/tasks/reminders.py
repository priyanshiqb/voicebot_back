"""Telling tenants their cycle is ending, before it does."""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from core.tasks import ReportedTask

logger = logging.getLogger(__name__)

#: Days before expiry to nudge. Three touches, spread out — one reminder is missed,
#: five is nagging.
REMINDER_DAYS = (7, 3, 1)


@shared_task(base=ReportedTask, name="onboard.tasks.send_renewal_reminders")
def send_renewal_reminders():
    """Notify tenants whose cycle ends in 7, 3 or 1 days.

    Renewals are manual — there is no auto-pay mandate — so a tenant who forgets
    simply loses access. That makes these reminders the difference between a renewal
    and accidental churn, not a nicety.

    Deduplicated by day: the task can run several times a day without sending twice.
    """
    from onboard.models import Subscription
    from onboard.services import billing_notifications

    now = timezone.now()
    sent = 0

    for days in REMINDER_DAYS:
        target = (now + timedelta(days=days)).date()
        due = Subscription.objects.filter(
            status=Subscription.SubscriptionStatusChoices.ACTIVE,
            end_date__date=target,
        ).select_related("tenant", "plan")

        for subscription in due:
            try:
                if billing_notifications.notify_renewal_due(subscription, days_left=days):
                    sent += 1
            except Exception:
                logger.exception(
                    "could not send renewal reminder for subscription %s", subscription.pk
                )

    if sent:
        logger.info("sent %s renewal reminder(s)", sent)
    return sent

"""The clock that moves subscriptions through their lifecycle."""

import logging

from celery import shared_task
from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone

from core.tasks import ReportedTask

logger = logging.getLogger(__name__)


@shared_task(base=ReportedTask, name="onboard.tasks.process_billing_cycle")
def process_billing_cycle():
    """Apply scheduled plan changes, then expire what has lapsed.

    **One task, one pass, in this order — deliberately.** Two independent jobs, one
    expiring subscriptions and one applying scheduled downgrades, race at exactly the
    moment both fire: a tenant who scheduled a downgrade for the end of their cycle
    would get expired instead, losing access they had paid to keep.

    Each subscription is handled in its own transaction so one bad row cannot stop
    the sweep.
    """
    from onboard.models import Subscription
    from onboard.services import subscriptions as subscription_service

    now = timezone.now()
    changed = renewed = expired = failed = 0

    due = Subscription.objects.filter(
        status=Subscription.SubscriptionStatusChoices.ACTIVE
    ).filter(
        Q(scheduled_change_at__lte=now) | Q(end_date__lt=now)
    ).values_list("pk", flat=True)

    for subscription_id in list(due):
        try:
            with db_transaction.atomic():
                subscription = (
                    Subscription.objects.select_for_update()
                    .filter(pk=subscription_id)
                    .first()
                )
                # Re-read under the lock: another worker may already have taken it.
                if subscription is None or subscription.status != "active":
                    continue

                if (
                    subscription.scheduled_plan_id
                    and subscription.scheduled_change_at
                    and subscription.scheduled_change_at <= now
                ):
                    new_plan = subscription.scheduled_plan
                    subscription_service.apply_plan_change(
                        subscription,
                        new_plan,
                        # None keeps the current quantity, which is what a plan-only
                        # downgrade wants. Set when the tenant scheduled a drop in
                        # minutes -- without passing it the change would fire having
                        # moved nothing.
                        minutes=subscription.scheduled_minutes,
                        reason="downgrade",
                    )
                    changed += 1
                    continue

                if subscription.end_date and subscription.end_date < now:
                    # A zero-cost plan that renews itself has nothing to charge, so
                    # the cycle simply rolls. This is what separates a trial that is
                    # meant to end from pay-as-you-go, which must not.
                    if subscription.rules.get("renews_without_payment"):
                        subscription_service.renew(subscription)
                        renewed += 1
                    else:
                        subscription_service.expire(subscription)
                        expired += 1
        except Exception:
            failed += 1
            logger.exception("billing cycle failed for subscription %s", subscription_id)

    if changed or renewed or expired or failed:
        logger.info(
            "billing cycle: %s changed, %s auto-renewed, %s expired, %s failed",
            changed, renewed, expired, failed,
        )
    return {"changed": changed, "renewed": renewed, "expired": expired, "failed": failed}


@shared_task(base=ReportedTask, name="onboard.tasks.expire_stale_billing_orders")
def expire_stale_billing_orders():
    """Cancel unpaid orders past their TTL.

    One open order at a time is what stops a tenant paying for two upgrades and
    getting one. That rule only works if abandoned orders eventually clear — without
    this, a single closed checkout tab locks the tenant out of buying anything, and
    the support request looks like a bug in the payment page.
    """
    from onboard.models import Transaction

    stale = Transaction.objects.filter(
        status=Transaction.TransactionStatusChoices.PENDING,
        expires_at__lt=timezone.now(),
    )
    count = stale.update(status=Transaction.TransactionStatusChoices.CANCELED)
    if count:
        logger.info("cancelled %s stale billing order(s)", count)
    return count

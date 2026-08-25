"""In-app notices about money and credits.

Deliberately not routed through ``core.services.notifications``: that module is
booking-shaped throughout — it loads a booking, resolves attendees, builds .ics
attachments. Billing shares none of that, and bending it to fit would make both
harder to follow.

Every function is best-effort. A notification that fails must never be the reason a
renewal, expiry or credit grant does not happen.
"""

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def _recipients(tenant_id):
    """Users who should hear about billing for this tenant.

    Everyone mapped to the tenant, for now. When roles arrive this narrows to owners
    and billing contacts — a support agent does not need to know the card expired.
    """
    from tenant.models import TenantUserMapping

    return [
        mapping.user
        for mapping in TenantUserMapping.objects.filter(
            tenant_id=tenant_id, is_deleted=False
        ).select_related("user")
        if mapping.user_id
    ]


def _notify(tenant_id, *, notification_type, title, body, dedup_key=None, route=None,
            push=True):
    """Notify every recipient in-app, and push where a device is registered.

    **In-app is the record; push is a nudge.** The in-app row is written first and
    synchronously, so the notice exists even if FCM is unreachable or the tenant has
    no devices. Push is queued after, and a failure there is logged rather than
    raised — a billing reminder that could not be pushed is still a billing reminder
    the tenant will see next time they open the dashboard.

    Deduplicated per recipient per day: the tasks that call this run on a schedule and
    can fire more than once in a day without meaning to say the same thing twice.
    """
    from core.models.in_app_notification import InAppNotification

    recipients = _recipients(tenant_id)
    if not recipients:
        logger.info("tenant %s has no users to notify", tenant_id)
        return 0

    created = 0
    for user in recipients:
        if dedup_key:
            already = InAppNotification.objects.filter(
                tenant_id=tenant_id, user=user, notification_type=notification_type,
                title=title,
            ).filter(created_at__date=timezone.now().date()).exists()
            if already:
                continue

        InAppNotification.objects.create(
            tenant_id=tenant_id, user=user,
            notification_type=notification_type, title=title, body=body,
        )
        created += 1

        if push:
            _push(tenant_id, user, title, body, notification_type, route)

    return created


def _push(tenant_id, user, title, body, notification_type, route):
    """Queue a push notification. Never raises.

    Queued rather than sent inline: this runs inside the billing tasks and the
    webhook, and neither should wait on FCM or fail because of it.
    """
    from core.tasks import send_push_to_user_task

    try:
        send_push_to_user_task.delay(
            str(tenant_id),
            str(user.pk),
            title,
            body,
            data={"type": notification_type, "route": route or "/billing"},
        )
    except Exception:
        logger.warning(
            "could not queue billing push for user %s (in-app notice was still created)",
            user.pk,
            exc_info=True,
        )


def notify_renewal_due(subscription, *, days_left):
    """"Your plan ends in N days." """
    from core.models.notification_log import NotificationLog

    plan = subscription.plan_snapshot or {}
    when = "tomorrow" if days_left == 1 else f"in {days_left} days"
    title = f"Your {plan.get('name', 'plan')} ends {when}"
    body = (
        f"Renew before {subscription.end_date:%d %b %Y} to keep your bot running. "
        f"Renewals are manual, so nothing is charged automatically — if the cycle "
        f"ends without payment, calls stop until you renew."
    )
    return _notify(
        subscription.tenant_id,
        notification_type=NotificationLog.NotificationType.BILLING,
        title=title, body=body, dedup_key=f"renewal:{days_left}",
        route="/billing/subscription",
    )


def notify_expired(subscription):
    """"Your plan has ended." """
    from core.models.notification_log import NotificationLog

    plan = subscription.plan_snapshot or {}
    return _notify(
        subscription.tenant_id,
        notification_type=NotificationLog.NotificationType.BILLING,
        route="/billing/subscription",
        title=f"Your {plan.get('name', 'plan')} has ended",
        body=(
            "Calls are paused until you renew. Any credits you purchased are kept and "
            "become available again the moment you do."
        ),
        dedup_key="expired",
    )


def notify_low_balance(wallet):
    """"You are running low on credits." """
    from core.models.notification_log import NotificationLog

    return _notify(
        wallet.tenant_id,
        notification_type=NotificationLog.NotificationType.CREDITS,
        route="/billing/wallet",
        title=f"{wallet.balance} credits remaining",
        body=(
            "Top up or upgrade to avoid interrupting calls mid-campaign. A campaign "
            "that stops halfway cannot un-call the contacts it already reached."
        ),
        dedup_key="low_balance",
    )


def notify_credits_added(tenant_id, *, credits, reason):
    """"Your credits have arrived."

    Sent after a top-up clears or a cycle allocates. Worth sending because the payment
    and the credits are separated by a webhook — a tenant who paid and saw nothing
    change opens a support ticket, and this closes that gap.
    """
    from core.models.notification_log import NotificationLog

    return _notify(
        tenant_id,
        notification_type=NotificationLog.NotificationType.CREDITS,
        route="/billing/wallet",
        title=f"{credits} credits added",
        body=f"{reason}. They are available now.",
        dedup_key=None,  # a real purchase should always be acknowledged
    )


def notify_plan_changed(subscription, *, previous_plan_code=None):
    """"You are now on <plan>."

    Covers both an upgrade applying on payment and a scheduled downgrade firing on its
    own — the second is the one that matters, because nobody is watching when a cycle
    boundary passes at 3am.
    """
    from core.models.notification_log import NotificationLog

    plan = subscription.plan_snapshot or {}
    moved_from = f" from {previous_plan_code}" if previous_plan_code else ""
    return _notify(
        subscription.tenant_id,
        notification_type=NotificationLog.NotificationType.BILLING,
        route="/billing/subscription",
        title=f"You are now on {plan.get('name', 'a new plan')}",
        body=(
            f"Your plan changed{moved_from}. Your next cycle ends "
            f"{subscription.end_date:%d %b %Y}."
        ),
        dedup_key=None,
    )

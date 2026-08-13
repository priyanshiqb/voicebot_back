import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Fallback for a purpose with no configured window — including one added to the
# model later and not yet given a setting. Erring short is deliberate: an
# unclassified record should expire on the shorter schedule, not accumulate
# forever because somebody forgot a line in settings.
DEFAULT_RETENTION_DAYS = 180
DEFAULT_BATCH = 5000


@shared_task
def purge_email_delivery_logs():
    """Beat task: delete platform email records past their retention window.

    Nothing else prunes this table, and every row holds an email address plus the
    IP of whoever requested it. That makes retention a privacy obligation first and
    a disk one second.

    **Windows differ by purpose.** Password reset and email change are
    security-audit records — the evidence someone reaches for after an account is
    compromised, which is rarely the week it happens. Signup OTP is support data
    with a shorter useful life. SMTP tests are operational noise.

    **Hard delete, via ``all_objects``.** A soft delete would leave the addresses
    and IPs in place, which defeats the point entirely; ``objects`` also returns a
    queryset whose ``delete()`` raises by design.

    Unlike ``core.tasks.purge_notification_logs``, there is **no guard on what may
    be removed**. That asymmetry is the whole reason these are two tables: a
    ``NotificationLog`` row *is* the idempotency mechanism, so deleting one re-arms
    a booking notification. Nothing here re-sends anything. Deleting a row here is
    simply forgetting.
    """
    from user.models.email_delivery_log import EmailDeliveryLog

    now = timezone.now()
    windows = getattr(settings, "EMAIL_DELIVERY_LOG_RETENTION_DAYS", {}) or {}
    default_days = int(
        getattr(
            settings, "EMAIL_DELIVERY_LOG_DEFAULT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS
        )
    )
    batch = int(getattr(settings, "EMAIL_DELIVERY_LOG_PURGE_BATCH", DEFAULT_BATCH))

    deleted = 0
    for purpose in EmailDeliveryLog.Purpose.values:
        days = int(windows.get(purpose, default_days))
        cutoff = now - timedelta(days=days)
        queryset = EmailDeliveryLog.all_objects.filter(
            purpose=purpose, created_at__lt=cutoff
        )

        # Chunked so one run cannot take a table-wide lock or build a DELETE with a
        # million ids in it.
        while True:
            ids = list(queryset.values_list("pk", flat=True)[:batch])
            if not ids:
                break
            removed, _ = EmailDeliveryLog.all_objects.filter(pk__in=ids).delete()
            deleted += removed
            if len(ids) < batch:
                break

    logger.info("purged %s email delivery log rows", deleted)
    return deleted


@shared_task
def purge_expired_sessions():
    """Beat task: prune login sessions and SimpleJWT's refresh-token blacklist.

    Two tables that grew without bound until this existed. ``user_sessions`` gains a
    row per login and holds an IP address and device name, so retention is a privacy
    obligation as much as a disk one. The blacklist gains a row pair per *refresh* —
    with rotation on and a 60-minute access token, roughly eight per active user per
    day — and a blacklist entry for an already-expired token protects nothing.

    Neither deletion can re-send or re-authorise anything, unlike
    ``core.tasks.purge_notification_logs`` whose rows are an idempotency mechanism.
    Here a deleted row is simply forgotten.
    """
    from user.services import sessions as session_service

    result = session_service.sweep()
    logger.info(
        "session sweep: %s deactivated, %s purged, blacklist flushed=%s",
        result["deactivated"],
        result["purged"],
        result["blacklist_flushed"],
    )
    return result["purged"]

"""
Recording platform-sent email.

The row is created here, at the call site, because this is the only place the
context exists — who requested it, from what address, and which in-progress signup
it belongs to. The Celery task then marks the outcome on the same row. That split
mirrors how ``core.tasks.send_scheduling_email_task`` treats ``NotificationLog``:
the enqueue knows the intent, only the worker knows whether it was delivered.
"""

import logging

from user.models.email_delivery_log import EmailDeliveryLog

logger = logging.getLogger(__name__)

USER_AGENT_MAX = 1000


def record(
    purpose,
    recipient,
    *,
    request=None,
    user=None,
    temp_user_id=None,
    tenant_id=None,
):
    """Open a PENDING delivery record and return its id, or ``None``.

    **Never raises.** Logging that an email was sent must not be able to stop the
    email being sent — a failure here degrades an audit trail, while a failure
    propagated to the caller would block a password reset. The return value is
    ``None`` on failure, which the task treats as "nothing to mark".

    ``recipient`` is captured as passed rather than re-read from ``user`` or the
    ``TempUser``: for an email change the message goes to the *new* address while
    the record still points at the same signup, and re-deriving it would quietly
    log the wrong one.
    """
    try:
        row = EmailDeliveryLog.objects.create(
            purpose=purpose,
            recipient=recipient,
            user=user if getattr(user, "pk", None) else None,
            temp_user_id=temp_user_id,
            tenant_id=tenant_id,
            status=EmailDeliveryLog.Status.PENDING,
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
        )
        return str(row.id)
    except Exception:  # noqa: BLE001
        logger.exception("could not record %s email to %s", purpose, recipient)
        return None


def mark(delivery_log_id, status, error=""):
    """Write the delivery outcome. Also never raises, for the same reason."""
    if not delivery_log_id:
        return
    try:
        from django.utils import timezone

        fields = {"status": status}
        if status == EmailDeliveryLog.Status.SENT:
            fields["sent_at"] = timezone.now()
        if error:
            fields["error"] = error[:2000]
        EmailDeliveryLog.objects.filter(pk=delivery_log_id).update(**fields)
    except Exception:  # noqa: BLE001
        logger.exception("could not update email delivery log %s", delivery_log_id)


def _client_ip(request):
    if request is None:
        return None
    try:
        from utils.device_utils import get_client_ip

        return get_client_ip(request) or None
    except Exception:  # noqa: BLE001
        return None


def _user_agent(request):
    if request is None:
        return ""
    return (request.META.get("HTTP_USER_AGENT") or "")[:USER_AGENT_MAX]

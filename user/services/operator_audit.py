"""Recording what platform staff did.

One entry point, used by every operator action. Two properties matter more than
convenience here:

**A failed action is still recorded.** An operator repeatedly failing to do something
is a signal, and it disappears entirely if only successes are kept. The failure is the
interesting row.

**Writing the log never breaks the action.** An audit failure must not roll back a
credit grant the customer is already expecting — that trades a missing row for a
missing grant, which is the worse of the two. So the write is best-effort and loud,
and reconciliation is what catches a gap.
"""

import logging

from user.models import OperatorAuditLog

logger = logging.getLogger(__name__)


def _client(request):
    """Where the request came from, as far as it can be trusted.

    ``REMOTE_ADDR`` behind a proxy is the proxy. ``X-Forwarded-For`` is client-settable
    and only meaningful when a proxy you control appends to it — so the left-most entry
    is taken, and it is recorded as evidence rather than relied upon.
    """
    if request is None:
        return None, ""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip = forwarded.split(",")[0].strip() or request.META.get("REMOTE_ADDR")
    return ip or None, request.META.get("HTTP_USER_AGENT", "")[:512]


def record(
    *,
    actor,
    action,
    reason,
    tenant=None,
    target="",
    payload=None,
    result=None,
    outcome=OperatorAuditLog.Outcome.SUCCESS,
    request=None,
):
    """Write one audit row. Returns it, or ``None`` if the write failed.

    ``reason`` is required by the caller's serializer, not defaulted here — an action
    that reaches this function without one has already lost the thing worth recording.
    """
    ip, agent = _client(request)
    try:
        return OperatorAuditLog.objects.create(
            actor=actor,
            action=action,
            tenant=tenant,
            target=str(target or ""),
            reason=reason,
            payload=payload or {},
            result=result or {},
            outcome=outcome,
            ip_address=ip,
            user_agent=agent,
        )
    except Exception:
        # Loud, but never fatal. See the module docstring.
        logger.exception(
            "operator audit write failed: actor=%s action=%s tenant=%s target=%s",
            getattr(actor, "pk", actor), action, getattr(tenant, "pk", tenant), target,
        )
        return None


def failed(*, actor, action, reason, error, **kwargs):
    """Shorthand for recording an action that did not work."""
    return record(
        actor=actor,
        action=action,
        reason=reason,
        outcome=OperatorAuditLog.Outcome.FAILED,
        result={"error": str(error)},
        **kwargs,
    )

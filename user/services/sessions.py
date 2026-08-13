"""
Session and refresh-token housekeeping.

Two tables grow with every login and every token refresh, and until this module
existed nothing pruned either:

``user_sessions``
    One row per login, forever. ``is_active`` was only ever set ``False`` by an
    explicit logout, so an expired session stayed ``is_active=True`` indefinitely —
    the column could not distinguish "logged out" from "long gone". Rows hold an IP
    address and a device name, which makes retention a privacy obligation before a
    disk one, and the two other logs in this codebase
    (``core.NotificationLog``, ``user.EmailDeliveryLog``) both already have one.

``token_blacklist_*`` (SimpleJWT)
    ``ROTATE_REFRESH_TOKENS`` and ``BLACKLIST_AFTER_ROTATION`` are both on, so
    **every refresh** writes an outstanding-token row and blacklists the old one.
    With a 60-minute access token, one active user generates roughly eight of each
    per day. SimpleJWT ships ``flushexpiredtokens`` for this; nothing was calling
    it.

Deleting from either table cannot re-send or re-authorise anything — unlike
``NotificationLog``, whose rows *are* an idempotency mechanism. Here a deleted row
is simply forgotten, so the sweep needs no guard beyond age.
"""

import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

def create_for_tokens(user, request, refresh, access):
    """Create the ``UserSession`` row that makes a token pair usable. **Raises on failure.**

    Every authenticated request resolves its access token's ``jti`` against this row
    (see :meth:`UserSession.validate_session_jti`), so a token pair without one is not
    a degraded login — it is a **dead** login. Every request will 401.

    That is why this raises rather than logging. The three call sites used to wrap the
    write in ``except Exception: logger.warning(...)`` and return the tokens anyway, so
    a transient database failure produced credentials that looked fine and worked
    nowhere. A 503 the client can retry is strictly better than a 200 it cannot use.

    The JTI/timestamp extraction lives here because it was duplicated between the login
    view and the post-payment onboarding view, and a copy that drifts is a session row
    that silently stops matching its token.
    """
    import datetime
    from datetime import timezone as dt_timezone

    from user.models.user_session import UserSession
    from utils.device_utils import get_client_ip, parse_user_agent

    def _moment(token, claim):
        value = token.get(claim)
        return (
            datetime.datetime.fromtimestamp(value, tz=dt_timezone.utc) if value else None
        )

    refresh_exp = _moment(refresh, "exp")

    return UserSession.objects.create(
        user=user,
        refresh_jti=refresh.get("jti"),
        access_jti=access.get("jti"),
        access_iat=_moment(access, "iat"),
        access_exp=_moment(access, "exp"),
        refresh_iat=_moment(refresh, "iat"),
        refresh_exp=refresh_exp,
        device_name=parse_user_agent(request.headers.get("User-Agent")),
        ip_address=get_client_ip(request),
        # A pair with no refresh expiry should not become an immortal session.
        expires_at=refresh_exp
        or (datetime.datetime.now(tz=dt_timezone.utc) + datetime.timedelta(days=1)),
    )


#: How long an expired session is kept before deletion. Measured from expiry, not
#: creation: a 7-day session deleted 30 days after it expired has been visible for
#: 37 days, which is the window an "unrecognised device" investigation needs.
DEFAULT_SESSION_RETENTION_DAYS = 30

DEFAULT_BATCH = 5000


def session_retention_days():
    return int(
        getattr(settings, "USER_SESSION_RETENTION_DAYS", DEFAULT_SESSION_RETENTION_DAYS)
    )


def deactivate_expired_sessions(now=None):
    """Mark past-expiry sessions inactive. Returns the number updated.

    Cheap bookkeeping that makes ``is_active`` mean what it says. Before this, an
    expired row stayed ``is_active=True`` and every consumer had to remember to add
    ``expires_at__gt=now`` — the session list does, and anything that forgot would
    show a device the user stopped using months ago.

    Not a security control: an expired *token* is already refused by SimpleJWT on
    signature and ``exp`` long before this row is consulted. This is about the
    column telling the truth.
    """
    from user.models.user_session import UserSession

    now = now or timezone.now()
    return UserSession.objects.filter(is_active=True, expires_at__lte=now).update(
        is_active=False
    )


def purge_expired_sessions(older_than_days=None, now=None, batch=None, dry_run=False):
    """Delete sessions whose expiry is further back than the retention window.

    Chunked, so one run cannot take a table-wide lock or build a ``DELETE`` with a
    million ids in it — the same shape as ``purge_email_delivery_logs``.

    Hard delete through ``all_objects``: a soft delete would leave the IP and device
    name in place, which defeats the purpose, and ``objects.delete()`` is refused by
    ``BaseQuerySet`` anyway.
    """
    from user.models.user_session import UserSession

    now = now or timezone.now()
    days = session_retention_days() if older_than_days is None else int(older_than_days)
    batch = int(batch or getattr(settings, "USER_SESSION_PURGE_BATCH", DEFAULT_BATCH))
    cutoff = now - timezone.timedelta(days=days)

    queryset = UserSession.all_objects.filter(expires_at__lt=cutoff)

    if dry_run:
        return queryset.count()

    deleted = 0
    while True:
        ids = list(queryset.values_list("pk", flat=True)[:batch])
        if not ids:
            break
        removed, _ = UserSession.all_objects.filter(pk__in=ids).delete()
        deleted += removed
        if len(ids) < batch:
            break

    if deleted:
        logger.info("purged %s expired user session row(s)", deleted)
    return deleted


def flush_expired_blacklist_tokens():
    """Run SimpleJWT's ``flushexpiredtokens``. Returns True if it ran.

    Deletes outstanding/blacklisted refresh tokens whose ``exp`` has passed. A
    blacklist entry for an already-expired token protects nothing — the token is
    refused on ``exp`` before the blacklist is consulted — so this is pure
    accumulation, and it accumulates fast: one row pair per refresh, per session.

    Tolerant of the app being absent so a deployment without
    ``token_blacklist`` installed does not fail the whole sweep.
    """
    from django.apps import apps
    from django.core.management import call_command

    if not apps.is_installed("rest_framework_simplejwt.token_blacklist"):
        logger.info("token_blacklist is not installed; nothing to flush")
        return False

    call_command("flushexpiredtokens")
    return True


def sweep(now=None, dry_run=False):
    """Everything above, in the order that matters. Returns a summary dict.

    Deactivate before purge so a run with a short retention window still leaves the
    column consistent for whatever it does not delete.
    """
    now = now or timezone.now()

    deactivated = 0 if dry_run else deactivate_expired_sessions(now=now)
    # After the demotion above and before the purge below. A device whose session
    # expired should stop receiving pushes — the client cannot tell us itself,
    # because an expired refresh token leaves it with no credentials to call any
    # endpoint with. Once purge_expired_sessions deletes the session row the link
    # is nulled and the signal is gone, so the order here is load-bearing.
    devices = 0 if dry_run else _deactivate_orphaned_devices()
    purged = purge_expired_sessions(now=now, dry_run=dry_run)
    flushed = False if dry_run else flush_expired_blacklist_tokens()

    return {
        "deactivated": deactivated,
        "devices_deactivated": devices,
        "purged": purged,
        "blacklist_flushed": flushed,
        "retention_days": session_retention_days(),
    }


def _deactivate_orphaned_devices():
    """Push devices behind now-dead sessions. Tolerant of core being unavailable."""
    try:
        from core.services import device_tokens as token_service

        return token_service.deactivate_for_dead_sessions()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to deactivate device tokens for expired sessions")
        return 0
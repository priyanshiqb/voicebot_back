"""
Device-token liveness, staleness and retention.

An FCM registration token carries **no expiry and cannot be validated offline** —
it is an opaque string that stays valid until the app is uninstalled, its data is
cleared, the browser drops its service worker, permission is revoked, or FCM reaps
the app instance for inactivity (~270 days). None of those events notify us.

Before this module the only feedback loop was reactive: a token was retired when a
real send failed with ``UNREGISTERED``. That is necessary but not sufficient, for
two reasons:

*   **It only fires when we have something to say.** Push here is a low-traffic
    internal alert channel, so a device that died in March can sit ``is_active=True``
    until the next reschedule alert months later.
*   **Nothing ever deleted a row.** ``core_device_tokens`` grew forever — the same
    unbounded-growth bug ``user_sessions`` and SimpleJWT's blacklist had, and web
    tokens churn hardest, so the accumulation is mostly permanent junk.

So liveness is decided three ways, cheapest first:

1.  **Heartbeat.** Clients re-register on every app start, refreshing ``last_seen_at``.
2.  **Age.** A token not re-registered within ``DEVICE_TOKEN_STALE_DAYS`` is treated
    as unreachable and stops receiving sends. Google's own guidance is ~2 months.
3.  **Probe.** ``--verify`` asks FCM directly with a validate-only send, which
    confirms death without notifying anybody.

Staleness reads ``COALESCE(last_seen_at, created_at)``, never ``last_seen_at``
alone. ``last_seen_at`` is nullable, and a bare ``last_seen_at__lt=cutoff`` silently
excludes NULL rows — every row predating this feature, plus anything created through
the admin or a fixture, would have been immortal.
"""

import logging

from django.conf import settings
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.models.device_token import DeviceToken

logger = logging.getLogger(__name__)

#: Stop sending to a token not re-registered within this window. Two months is
#: Google's own staleness guidance, and it is deliberately far short of FCM's
#: ~270-day reaping: we would rather skip a live-but-silent device than keep
#: paying for a fan-out to devices that are almost certainly gone.
DEFAULT_STALE_DAYS = 60

#: How long a dead row is kept before deletion, measured from last contact. Long
#: enough that "why did my phone stop getting alerts?" is still answerable.
DEFAULT_RETENTION_DAYS = 90

#: Ceiling on live registrations per user per tenant. A human has a handful of
#: devices; a loop in a client has thousands.
DEFAULT_MAX_PER_USER = 20

#: Consecutive ambiguous failures before a token is retired. Not 1: INVALID_ARGUMENT
#: can mean a malformed *payload* rather than a malformed token, and retiring a
#: healthy device because one send was built wrong is the worse error.
DEFAULT_FAILURE_STRIKES = 3

DEFAULT_BATCH = 5000


def stale_days():
    return int(getattr(settings, "DEVICE_TOKEN_STALE_DAYS", DEFAULT_STALE_DAYS))


def retention_days():
    return int(getattr(settings, "DEVICE_TOKEN_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))


def max_per_user():
    return int(getattr(settings, "DEVICE_TOKEN_MAX_PER_USER", DEFAULT_MAX_PER_USER))


def failure_strikes():
    return int(
        getattr(settings, "DEVICE_TOKEN_FAILURE_STRIKES", DEFAULT_FAILURE_STRIKES)
    )


def _with_last_contact(queryset):
    """Annotate ``last_contact`` = last registration, falling back to creation.

    Every age comparison in this module goes through here. See the module docstring
    for why the fallback is not optional.
    """
    return queryset.annotate(last_contact=Coalesce("last_seen_at", "created_at"))


def deliverable(queryset=None, now=None):
    """Tokens we are willing to send to: active, not deleted, and not stale.

    This is the *only* queryset the send path should use. Filtering on
    ``is_active=True`` alone — which is what push.py did — treats a token last seen
    fourteen months ago as reachable.
    """
    now = now or timezone.now()
    base = DeviceToken.objects.all() if queryset is None else queryset
    cutoff = now - timezone.timedelta(days=stale_days())
    return _with_last_contact(base.filter(is_active=True, is_deleted=False)).filter(
        last_contact__gte=cutoff
    )


def stale_queryset(now=None):
    """Active tokens whose last contact is older than the staleness window."""
    now = now or timezone.now()
    cutoff = now - timezone.timedelta(days=stale_days())
    return _with_last_contact(
        DeviceToken.objects.filter(is_active=True, is_deleted=False)
    ).filter(last_contact__lt=cutoff)


def deactivate_stale(now=None, dry_run=False):
    """Retire tokens that have not checked in. Returns the number affected.

    Bookkeeping, not enforcement — :func:`deliverable` already excludes these, so
    this changes no delivery outcome. What it changes is that ``is_active`` means
    "we believe this is reachable" instead of "was reachable once", which is the
    property the retention purge and every operational question depend on.
    """
    queryset = stale_queryset(now=now)
    if dry_run:
        return queryset.count()

    # Re-filtered by pk: an annotated queryset cannot be used directly in UPDATE.
    ids = list(queryset.values_list("pk", flat=True))
    if not ids:
        return 0
    updated = DeviceToken.objects.filter(pk__in=ids).update(
        is_active=False,
        last_error=f"Not re-registered in {stale_days()} days",
    )
    if updated:
        logger.info("deactivated %s stale device token(s)", updated)
    return updated


def purge(older_than_days=None, now=None, batch=None, dry_run=False):
    """Delete dead rows whose last contact is beyond the retention window.

    "Dead" is inactive **or** soft-deleted. Active rows are never deleted here
    regardless of age — :func:`deactivate_stale` demotes them first, so a token is
    always visible as inactive for a full retention window before it disappears.

    Chunked hard delete through ``all_objects``: a soft delete would leave the row
    (and the token, which identifies a device) in place, and ``objects.delete()`` is
    refused by ``BaseQuerySet`` anyway.
    """
    now = now or timezone.now()
    days = retention_days() if older_than_days is None else int(older_than_days)
    batch = int(batch or getattr(settings, "DEVICE_TOKEN_PURGE_BATCH", DEFAULT_BATCH))
    cutoff = now - timezone.timedelta(days=days)

    queryset = _with_last_contact(
        DeviceToken.all_objects.filter(Q(is_active=False) | Q(is_deleted=True))
    ).filter(last_contact__lt=cutoff)

    if dry_run:
        return queryset.count()

    deleted = 0
    while True:
        ids = list(queryset.values_list("pk", flat=True)[:batch])
        if not ids:
            break
        removed, _ = DeviceToken.all_objects.filter(pk__in=ids).delete()
        deleted += removed
        if len(ids) < batch:
            break

    if deleted:
        logger.info("purged %s dead device token row(s)", deleted)
    return deleted


def current_session(request):
    """The ``UserSession`` behind this request's access token, or ``None``.

    Resolved from the token's ``jti`` rather than from the user, because a user may
    have several live sessions and the device belongs to exactly one of them — the
    one it registered under.

    Returns ``None`` rather than raising for every case that is not a JWT-carrying
    human request: agent tokens, ``force_authenticate`` in tests, a session that was
    revoked between authentication and here. The link is an optimisation on top of
    the staleness rule, never a precondition for registering a device.
    """
    token = getattr(request, "auth", None)
    if token is None:
        return None
    try:
        jti = token.get("jti")
    except Exception:  # noqa: BLE001 - agent tokens are not JWTs
        return None
    if not jti:
        return None

    from user.models.user_session import UserSession

    return UserSession.objects.filter(access_jti=jti, is_active=True).first()


def deactivate_for_dead_sessions(dry_run=False):
    """Retire devices whose login session is no longer active. Returns the count.

    The invariant this enforces: **a device nobody is signed in on does not receive
    pushes.** ``UserSession.revoke()`` covers logout and remote revoke immediately;
    this covers the case no client can — a refresh token that simply expired, which
    leaves the app with no credentials to call any endpoint with, so it cannot
    deactivate its own device even though it knows it has been signed out.

    Expressed as one join rather than by collecting session ids so it stays a single
    statement no matter how many sessions expired overnight.

    Tokens with no session — registered before this link existed, or whose session row
    has since been purged — are left alone and fall to the staleness rule instead.
    ``session__isnull=False`` is **redundant today**: ``session__is_active=False``
    compiles to an INNER JOIN, which already drops NULL rows. It is kept as an
    explicit guard because that is an artefact of how Django compiles a single
    ``filter()`` — combining these conditions into OR'd ``Q`` objects would promote
    the join to LEFT OUTER and start matching every unlinked token, which would
    silently retire every legacy device on the next sweep.
    """
    queryset = DeviceToken.objects.filter(
        is_active=True, session__isnull=False, session__is_active=False
    )
    if dry_run:
        return queryset.count()

    updated = queryset.update(is_active=False, last_error="Session ended")
    if updated:
        logger.info("deactivated %s device token(s) behind dead sessions", updated)
    return updated


def enforce_user_cap(tenant, user, keep=None):
    """Retire a user's oldest live tokens beyond the cap. Returns the number retired.

    Called after a successful registration. The newest are kept because the most
    recently registered token is the one most likely to be a device the user is
    actually holding.
    """
    keep = keep or max_per_user()
    # Ordered on the coalesced value, not on last_seen_at directly: Postgres sorts
    # NULLs first under DESC, so a row that had never been re-registered would rank
    # as the *newest* and survive while genuinely current devices were retired.
    live = list(
        _with_last_contact(
            DeviceToken.objects.filter(
                tenant=tenant, user=user, is_active=True, is_deleted=False
            )
        )
        .order_by("-last_contact")
        .values_list("pk", flat=True)
    )
    excess = live[keep:]
    if not excess:
        return 0
    retired = DeviceToken.objects.filter(pk__in=excess).update(
        is_active=False, last_error=f"Exceeded {keep} active devices"
    )
    logger.info(
        "retired %s device token(s) over the cap for user %s", retired, user.pk
    )
    return retired


# --- Send-path feedback -------------------------------------------------------
#
# These are keyed on the token string and are deliberately **not** tenant-scoped.
# A token identifies one app instance on one device; if FCM has disowned it, it is
# dead in every tenant that happens to hold a copy, and leaving the other copies
# active would mean re-learning the same fact on every future send.


def record_success(token):
    """Clear the strike counter after a delivery FCM accepted."""
    DeviceToken.objects.filter(token=token).exclude(failure_count=0).update(
        failure_count=0, last_error=""
    )


def retire(token, reason):
    """Deactivate a token FCM has definitively rejected."""
    updated = DeviceToken.objects.filter(token=token, is_active=True).update(
        is_active=False, last_error=reason[:255]
    )
    if updated:
        logger.info("retired device token after %s", reason)
    return updated


def record_ambiguous_failure(token, reason):
    """Strike a token whose failure *might* be its own fault; retire at the limit.

    ``INVALID_ARGUMENT`` is the case this exists for. FCM returns it both for a
    malformed registration token and for a malformed message, and the send API does
    not distinguish them. Retiring on the first one would let a single bad payload
    wipe out every device it was sent to; never retiring — the old behaviour — means
    a permanently broken token is retried on every alert forever.

    Striking splits the difference: a genuinely bad token accumulates strikes across
    unrelated sends and goes away, while a one-off payload bug costs nothing.
    """
    limit = failure_strikes()
    rows = list(DeviceToken.objects.filter(token=token).values("pk", "failure_count"))
    if not rows:
        return 0

    retired = 0
    for row in rows:
        count = (row["failure_count"] or 0) + 1
        if count >= limit:
            DeviceToken.objects.filter(pk=row["pk"]).update(
                is_active=False,
                failure_count=count,
                last_error=f"{reason} x{count}"[:255],
            )
            retired += 1
        else:
            DeviceToken.objects.filter(pk=row["pk"]).update(
                failure_count=count, last_error=reason[:255]
            )

    if retired:
        logger.warning(
            "retired %s device token(s) after %s consecutive ambiguous failures",
            retired,
            limit,
        )
    return retired


# --- Scheduled sweep ----------------------------------------------------------


def sweep(now=None, dry_run=False, verify=False, verify_limit=500):
    """Deactivate stale tokens, optionally probe them, then purge. Summary dict.

    Order matters: demote before delete, so a run with a short retention window
    still leaves ``is_active`` consistent for whatever it does not remove.

    ``verify`` adds a validate-only send per candidate — an authoritative liveness
    answer that delivers nothing. It costs one FCM call per token, so it is opt-in
    and capped rather than part of the nightly job.
    """
    now = now or timezone.now()

    probed = {"alive": 0, "dead": 0, "unknown": 0}
    if verify:
        probed = probe_stale(now=now, limit=verify_limit, dry_run=dry_run)

    # Before the age rule, because a dead session is a certainty and staleness is an
    # inference — and because the session sweep hard-deletes rows after its own
    # retention window, which would blank the link and lose the signal.
    orphaned = deactivate_for_dead_sessions(dry_run=dry_run)
    deactivated = deactivate_stale(now=now, dry_run=dry_run)
    purged = purge(now=now, dry_run=dry_run)

    return {
        "probed": probed,
        "orphaned": orphaned,
        "deactivated": deactivated,
        "purged": purged,
        "stale_days": stale_days(),
        "retention_days": retention_days(),
    }


def probe_stale(now=None, limit=500, dry_run=False):
    """Ask FCM whether each stale token is still real. Returns a verdict tally.

    A dead verdict retires the token immediately with a distinct reason, so an
    operator can tell "FCM says this is gone" apart from "we aged it out". An alive
    verdict is left alone: the token works, but the client has still stopped
    checking in, and :func:`deactivate_stale` will demote it on the same run. That
    is the intended outcome — a device we cannot confirm a client is running is not
    one we want in the fan-out — but the tally makes the distinction visible.
    """
    from core.services import push

    tally = {"alive": 0, "dead": 0, "unknown": 0}
    tokens = list(
        stale_queryset(now=now).values_list("token", flat=True)[: int(limit)]
    )
    if not tokens:
        return tally

    for token in tokens:
        verdict = push.probe_token(token)
        if verdict is True:
            tally["alive"] += 1
        elif verdict is False:
            tally["dead"] += 1
            if not dry_run:
                retire(token, "FCM validate-only probe rejected this token")
        else:
            tally["unknown"] += 1
    return tally

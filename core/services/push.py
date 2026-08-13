"""
FCM push notifications.

``firebase-admin`` is imported lazily and the app is only initialised when
``FIREBASE_CREDENTIALS`` is configured — so this module is inert (sends return 0)
until push is set up, and tests run without Firebase.

Three things here are less obvious than they look:

**Delivery is multicast, not a loop.** ``send_each_for_multicast`` takes up to 500
tokens in one call and returns a per-token result. The previous implementation sent
one HTTP request per token, which was slow *and* threw away the only signal that
makes ``INVALID_ARGUMENT`` interpretable (see below).

**Failures are classified, not merely logged.** FCM's errors fall into three groups
and only one of them means "this token is dead". Treating them alike is how a
permanently malformed token gets retried on every alert forever, and how a single
bad payload could wipe out every device it was sent to.

**Accepted is not delivered.** A success here means Google took the message. There
is no per-device delivery receipt in this API — that data only exists in FCM's
BigQuery export. Anything that reports "notified" is reporting acceptance.
"""

import base64
import hashlib
import json
import logging
import time
from datetime import timedelta

from django.conf import settings

from core.models.device_token import DeviceToken
from core.services import device_tokens as token_service

logger = logging.getLogger(__name__)

_app_ready = False

#: FCM's hard ceiling for one multicast call.
FCM_MULTICAST_LIMIT = 500

#: FCM's message size limit. Ours is checked against the data payload only, which
#: is the part callers control and the part that grows unexpectedly.
MAX_DATA_BYTES = 4096

#: How long FCM should keep trying before dropping the message. An internal alert
#: about a booking is worthless once it is stale — a "needs rescheduling" push
#: arriving three days late is actively misleading, so we would rather it expire.
DEFAULT_TTL_SECONDS = 86400

#: FCM disowned the token. Unambiguous: retire it now.
PERMANENT_ERRORS = ("UnregisteredError", "SenderIdMismatchError")

#: Might be the token, might be the message. FCM does not say which, so these
#: accumulate strikes rather than retiring anything immediately.
AMBIGUOUS_ERRORS = ("InvalidArgumentError",)

#: Definitely not the token's fault — quota, outage, or an APNs credential problem.
#: These must never count against a device.
TRANSIENT_ERRORS = (
    "QuotaExceededError",
    "UnavailableError",
    "InternalError",
    "ThirdPartyAuthError",
    "DeadlineExceededError",
)


def _ensure_app():
    """Initialise the Firebase app once. Returns False if push isn't configured."""
    global _app_ready
    if _app_ready:
        return True
    creds_path = getattr(settings, "FIREBASE_CREDENTIALS", "")
    if not creds_path:
        return False
    import firebase_admin
    from firebase_admin import credentials

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(creds_path))
    _app_ready = True
    return True


def _classify(exc):
    """Bucket an FCM exception: ``permanent``, ``ambiguous`` or ``transient``.

    Matched on the class name rather than the class itself so this keeps working
    if firebase-admin moves a symbol between modules, and so tests can exercise the
    branches without importing the SDK.
    """
    name = type(exc).__name__
    if name in PERMANENT_ERRORS:
        return "permanent"
    if name in AMBIGUOUS_ERRORS:
        return "ambiguous"
    if name in TRANSIENT_ERRORS:
        return "transient"
    return "transient"


def _is_unregistered(exc):
    """Back-compat shim for callers and tests predating :func:`_classify`."""
    return _classify(exc) == "permanent"


def _collapse_id(collapse_key):
    """Derive one collapse identifier that is legal on all three platforms.

    Web Push's ``Topic`` header is limited to 32 characters from the URL-safe
    base64 alphabet, and APNs caps ``apns-collapse-id`` at 64 bytes — so a natural
    key like ``"booking:<uuid>"`` is rejected by the first and truncated by the
    second. Hashing gives one value that satisfies both and still collapses
    consistently.
    """
    if not collapse_key:
        return None
    digest = hashlib.sha256(str(collapse_key).encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:32]


def _normalise_data(data, route=None):
    """Stringify the data payload, enforce the size ceiling, and attach the route.

    An oversized payload is **dropped, not truncated, and the message still goes**.
    Truncating would hand the client corrupt JSON; refusing outright would turn a
    developer's mistake into a missed operational alert. The human still sees the
    title and body — they just land on the dashboard instead of the deep link — and
    the error log names the offending keys.
    """
    payload = {k: str(v) for k, v in (data or {}).items()}
    if route:
        payload["route"] = str(route)
    if not payload:
        return {}

    size = len(json.dumps(payload).encode("utf-8"))
    if size > MAX_DATA_BYTES:
        logger.error(
            "push data payload is %s bytes (limit %s); sending without data. keys=%s",
            size,
            MAX_DATA_BYTES,
            sorted(payload),
        )
        return {}
    return payload


def _platform_configs(messaging, ttl_seconds, collapse_id, priority, data_only, route):
    """Per-platform envelopes: expiry, collapsing, priority, iOS wake-up."""
    ttl_seconds = int(ttl_seconds)

    android = messaging.AndroidConfig(
        priority="high" if priority == "high" else "normal",
        ttl=timedelta(seconds=ttl_seconds),
        collapse_key=collapse_id,
    )

    apns_headers = {
        "apns-priority": "10" if priority == "high" else "5",
        "apns-expiration": str(int(time.time()) + ttl_seconds),
    }
    if collapse_id:
        apns_headers["apns-collapse-id"] = collapse_id
    apns = messaging.APNSConfig(
        headers=apns_headers,
        payload=messaging.APNSPayload(
            aps=messaging.Aps(
                sound=None if data_only else "default",
                # Without this a data-only message is simply not delivered on iOS.
                content_available=True if data_only else None,
            )
        ),
    )

    webpush_headers = {"TTL": str(ttl_seconds)}
    if collapse_id:
        webpush_headers["Topic"] = collapse_id
    fcm_options = None
    link = _webpush_link(route)
    if link:
        # WebpushFCMOptions, not WebpushFcmOptions — the SDK capitalises this one
        # acronym differently from the rest of its API.
        fcm_options = messaging.WebpushFCMOptions(link=link)
    webpush = messaging.WebpushConfig(headers=webpush_headers, fcm_options=fcm_options)

    return android, apns, webpush


def _webpush_link(route):
    """Absolute HTTPS target for a web notification click, or None.

    FCM rejects a non-HTTPS ``link``, which in practice means this is inert in
    local development — deliberately, rather than failing the send.
    """
    if not route:
        return None
    base = getattr(settings, "FRONTEND_PROJECT_URL", "") or ""
    base = base.rstrip("/")
    if not base.startswith("https://"):
        return None
    return f"{base}/{str(route).lstrip('/')}"


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _send(
    tokens,
    title,
    body,
    data=None,
    ttl_seconds=None,
    collapse_key=None,
    priority="high",
    route=None,
):
    """Deliver to every token. Returns the number FCM accepted.

    Failure handling is the substance here:

    * **permanent** → retire the token immediately.
    * **ambiguous** → one strike; retired once it reaches the limit.
    * **transient** → the token is untouched. A quota error or an outage says
      nothing about whether a device exists.

    The one refinement worth spelling out: when *every* token in a multi-token batch
    fails ``INVALID_ARGUMENT``, the common factor is the message, not the devices —
    so nothing is struck and the payload is logged instead. That discrimination is
    only possible because the batch is sent as one call; the per-token loop this
    replaced could not tell the two cases apart.
    """
    tokens = [t for t in tokens if t]
    if not tokens:
        return 0
    if not _ensure_app():
        logger.info("FCM not configured; skipping %d push(es)", len(tokens))
        return 0

    from firebase_admin import messaging

    data_only = not title and not body
    payload = _normalise_data(data, route=route)
    collapse_id = _collapse_id(collapse_key)
    if ttl_seconds is None:
        ttl_seconds = getattr(settings, "PUSH_TTL_SECONDS", DEFAULT_TTL_SECONDS)
    android, apns, webpush = _platform_configs(
        messaging,
        ttl_seconds,
        collapse_id,
        priority,
        data_only,
        route,
    )
    notification = (
        None if data_only else messaging.Notification(title=title, body=body)
    )

    sent = 0
    for chunk in _chunks(tokens, FCM_MULTICAST_LIMIT):
        try:
            batch = messaging.send_each_for_multicast(
                messaging.MulticastMessage(
                    notification=notification,
                    data=payload,
                    tokens=chunk,
                    android=android,
                    apns=apns,
                    webpush=webpush,
                )
            )
        except Exception as exc:  # noqa: BLE001
            # The call itself failed — credentials, network, a malformed envelope.
            # Nothing is known about any individual token, so nothing is penalised.
            logger.warning("FCM multicast call failed for %d token(s): %s", len(chunk), exc)
            continue

        sent += _apply_results(chunk, batch.responses)

    return sent


def _apply_results(chunk, responses):
    """Attribute per-token outcomes and update the rows. Returns the accepted count."""
    accepted = 0
    permanent, ambiguous = [], []

    for token, result in zip(chunk, responses):
        if result.success:
            accepted += 1
            token_service.record_success(token)
            continue

        exc = result.exception
        kind = _classify(exc)
        if kind == "permanent":
            permanent.append((token, type(exc).__name__))
        elif kind == "ambiguous":
            ambiguous.append((token, str(exc)[:200]))
        else:
            logger.warning("FCM transient failure: %s", exc)

    for token, reason in permanent:
        token_service.retire(token, f"FCM rejected the token: {reason}")

    # See the note in _send: a whole batch failing the same ambiguous way is
    # evidence about the message, not about the devices.
    if ambiguous and len(chunk) > 1 and len(ambiguous) == len(chunk):
        logger.error(
            "every token in a %d-token batch failed INVALID_ARGUMENT; treating this "
            "as a malformed message and leaving the tokens alone: %s",
            len(chunk),
            ambiguous[0][1],
        )
    else:
        for token, reason in ambiguous:
            token_service.record_ambiguous_failure(token, f"INVALID_ARGUMENT: {reason}")

    return accepted


def probe_token(token):
    """Ask FCM whether a token is real, delivering nothing.

    Returns ``True`` (alive), ``False`` (dead), or ``None`` (could not tell —
    push unconfigured, or a transient failure).

    This is a validate-only send: FCM resolves the token and reports what it would
    have done, without queueing anything for the device. It is the only proactive
    liveness check the API offers, and it is what makes retiring an aged token a
    confirmed decision rather than a guess.
    """
    if not token or not _ensure_app():
        return None

    from firebase_admin import messaging

    try:
        messaging.send(messaging.Message(data={"ping": "1"}, token=token), dry_run=True)
        return True
    except Exception as exc:  # noqa: BLE001
        kind = _classify(exc)
        if kind == "permanent":
            return False
        if kind == "ambiguous":
            # The message is a fixed, known-good one-key payload, so an
            # INVALID_ARGUMENT here can only be about the token.
            return False
        logger.info("probe inconclusive for a device token: %s", exc)
        return None


def send_to_user(tenant, user, title, body, data=None, **envelope):
    tokens = list(
        token_service.deliverable(
            DeviceToken.objects.filter(tenant=tenant, user=user)
        ).values_list("token", flat=True)
    )
    return _send(tokens, title, body, data, **envelope)


def notify_tenant_admins(tenant, title, body, data=None, **envelope):
    from user.models import CustomUser

    tokens = list(
        token_service.deliverable(
            DeviceToken.objects.filter(
                tenant=tenant, user__user_type=CustomUser.UserType.ADMIN
            )
        ).values_list("token", flat=True)
    )
    return _send(tokens, title, body, data, **envelope)

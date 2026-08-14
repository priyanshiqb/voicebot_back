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

#: Why the credential failed to load, when one is set but unusable. Surfaced by
#: the diagnostic endpoint so "push is on but nothing arrives" has an answer.
_init_error = None

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


def _build_credential(credentials, value):
    """Accept either a path to the service-account JSON, or the JSON itself.

    A path is what the documentation asks for and what a mounted secret gives you.
    Inline JSON is accepted because plenty of platforms only offer environment
    variables — and because pasting the file's contents into ``FIREBASE_CREDENTIALS``
    is the obvious thing to try, so it should either work or say why, not blow up
    inside ``Certificate()`` with a message about a filename.
    """
    value = value.strip()
    if value.startswith("{"):
        return credentials.Certificate(json.loads(value))
    return credentials.Certificate(value)


def _ensure_app():
    """Initialise the Firebase app once. Returns False if push isn't usable.

    Distinguishes *unset* from *broken*. An unset credential is a deliberate
    "push is off here" and stays quiet; a credential that is set but cannot be
    loaded is a misconfiguration, so it is recorded in ``_init_error`` and logged
    at error level. Returning False either way keeps a bad value from raising
    through every send — the diagnostic endpoint reports the reason instead.
    """
    global _app_ready, _init_error
    if _app_ready:
        return True
    raw = getattr(settings, "FIREBASE_CREDENTIALS", "")
    if not raw:
        return False

    import firebase_admin
    from firebase_admin import credentials

    try:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(_build_credential(credentials, raw))
    except Exception as exc:  # noqa: BLE001
        _init_error = f"{type(exc).__name__}: {exc}"[:300]
        logger.error("FCM credentials are set but could not be loaded: %s", _init_error)
        return False

    _app_ready = True
    _init_error = None
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
    detailed=False,
):
    """Deliver to every token. Returns the number FCM accepted.

    ``detailed=True`` returns ``(accepted, outcomes)`` instead, where ``outcomes``
    maps each token to what happened to it. Only the diagnostic *test* endpoint
    asks for that — a caller sending a real notification has nothing useful to do
    with per-device verdicts, and the default keeps the plain-integer contract every
    other call site and test relies on.

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
        return (0, {}) if detailed else 0
    if not _ensure_app():
        logger.info("FCM not configured; skipping %d push(es)", len(tokens))
        return (0, {}) if detailed else 0

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
    outcomes = {}
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
            for token in chunk:
                outcomes[token] = {
                    "accepted": False,
                    "kind": "transient",
                    "detail": f"The send call itself failed: {exc}"[:200],
                }
            continue

        accepted, chunk_outcomes = _apply_results(chunk, batch.responses)
        sent += accepted
        outcomes.update(chunk_outcomes)

    return (sent, outcomes) if detailed else sent


def _apply_results(chunk, responses):
    """Attribute per-token outcomes and update the rows.

    Returns ``(accepted, outcomes)``. ``outcomes`` is built for every token whether
    or not anyone asked for it — it costs a dict entry, and building it only on
    demand would mean the diagnostic path and the production path take different
    branches through the exact logic a diagnostic exists to inspect.
    """
    accepted = 0
    permanent, ambiguous = [], []
    outcomes = {}

    for token, result in zip(chunk, responses):
        if result.success:
            accepted += 1
            token_service.record_success(token)
            outcomes[token] = {"accepted": True, "kind": None, "detail": None}
            continue

        exc = result.exception
        kind = _classify(exc)
        outcomes[token] = {
            "accepted": False,
            "kind": kind,
            "detail": f"{type(exc).__name__}: {exc}"[:200],
        }
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

    return accepted, outcomes


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


def is_configured():
    """Whether push is set up on this server at all.

    Distinct from a send returning ``0``. Without this, a test that reaches zero
    devices is indistinguishable from a server where ``FIREBASE_CREDENTIALS`` was
    never set — and telling those two apart is the entire point of a test endpoint.
    """
    return bool(getattr(settings, "FIREBASE_CREDENTIALS", ""))


def configuration_error():
    """The reason a *set* credential is unusable, or None.

    Forces initialisation so the answer reflects reality rather than whatever the
    module happened to try earlier. "Configured" and "working" are different
    claims, and only this one distinguishes them.
    """
    if not is_configured():
        return None
    if _ensure_app():
        # Initialisation succeeded, so whatever _init_error holds is stale — an
        # earlier failed attempt, or another test. Success is the authority.
        return None
    return _init_error or "Firebase credentials could not be loaded."


#: Deliberately short. A test push that arrives tomorrow proves nothing about
#: whether the device was reachable when the user pressed the button, and an alert
#: surfacing hours later reads as a real notification rather than a diagnostic.
TEST_TTL_SECONDS = 300

TEST_TITLE = "Test notification"
TEST_BODY = "Push notifications are working on this device."


def send_test_to_tokens(tokens):
    """Send the diagnostic push to explicit tokens. Returns ``(accepted, outcomes)``.

    The single definition of what a test push *is*, shared by the API endpoint and
    the admin action. Two copies would drift, and the first symptom would be a
    developer proving push works from one of them while it fails from the other.
    """
    return _send(
        list(tokens),
        TEST_TITLE,
        TEST_BODY,
        data={"type": "test"},
        ttl_seconds=TEST_TTL_SECONDS,
        # No collapse key: two presses of "send test" must produce two
        # notifications, or the button appears broken the second time.
        collapse_key=None,
        detailed=True,
    )


def send_test_to_user(tenant, user, token=None):
    """Send a diagnostic push to the caller's own devices. Returns a report.

    Reports rather than returning a bare count, because every interesting answer
    here is a *why*: push is switched off server-side, you have devices but none we
    would deliver to, or this specific handset was rejected by FCM.

    Deliberately **not** deduplicated through ``NotificationLog`` — this is a
    diagnostic, and a dedup key would make the second press of a "send test" button
    silently do nothing, which is the opposite of what a test is for.

    Scoped to ``user`` by the caller. Nothing here widens that.
    """
    owned = DeviceToken.objects.filter(tenant=tenant, user=user, is_deleted=False)

    if token:
        # An explicitly named token is sent to **verbatim**, registered or not, and
        # regardless of staleness. The caller is holding a string and asking "does
        # this one work?" — answering "you have no deliverable device" would be
        # true and useless, and routing through the lookup makes the DB state a
        # variable in a test whose entire purpose is to isolate FCM.
        targets = [token]
        matched = {device.token: device for device in owned.filter(token=token)}
        registered = len(matched)
    else:
        devices = list(token_service.deliverable(owned).order_by("-last_seen_at"))
        targets = [device.token for device in devices]
        matched = {device.token: device for device in devices}
        registered = owned.count()

    report = {
        "push_enabled": is_configured(),
        "configuration_error": configuration_error(),
        "registered": registered,
        "targeted": len(targets),
        "accepted": 0,
        "results": [],
    }
    if not report["push_enabled"] or report["configuration_error"] or not targets:
        return report

    sent, outcomes = send_test_to_tokens(targets)

    report["accepted"] = sent
    for target in targets:
        outcome = outcomes.get(target, {})
        device = matched.get(target)
        if device is not None:
            device.refresh_from_db(fields=["is_active", "last_error"])
        report["results"].append(
            {
                "id": str(device.id) if device else None,
                "platform": device.platform if device else None,
                "last_seen_at": device.last_seen_at if device else None,
                # False means "we sent to this string but hold no row for it under
                # your account" — worth knowing, because such a device will never
                # receive a *real* notification, only this test.
                "known_device": device is not None,
                "accepted": bool(outcome.get("accepted")),
                "detail": outcome.get("detail"),
                # A failed test can retire the device on the spot. Saying so stops
                # the next call reporting "0 registered" as if it were unrelated.
                "deactivated": bool(device and not device.is_active),
            }
        )
    return report


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

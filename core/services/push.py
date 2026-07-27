"""
FCM push notifications. firebase-admin is imported lazily and the app is only
initialised when FIREBASE_CREDENTIALS is configured — so this module is inert
(sends return 0) until push is set up, and tests can run without Firebase.
"""

import logging

from django.conf import settings

from core.models.device_token import DeviceToken

logger = logging.getLogger(__name__)

_app_ready = False


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


def _is_unregistered(exc):
    return type(exc).__name__ in ("UnregisteredError", "SenderIdMismatchError")


def _send(tokens, title, body, data=None):
    tokens = [t for t in tokens if t]
    if not tokens:
        return 0
    if not _ensure_app():
        logger.info("FCM not configured; skipping %d push(es)", len(tokens))
        return 0

    from firebase_admin import messaging

    sent = 0
    for token in tokens:
        try:
            messaging.send(
                messaging.Message(
                    notification=messaging.Notification(title=title, body=body),
                    data={k: str(v) for k, v in (data or {}).items()},
                    token=token,
                )
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001
            if _is_unregistered(exc):
                DeviceToken.objects.filter(token=token).update(is_active=False)
            else:
                logger.warning("FCM send failed: %s", exc)
    return sent


def send_to_user(tenant, user, title, body, data=None):
    tokens = list(
        DeviceToken.objects.filter(
            tenant=tenant, user=user, is_active=True, is_deleted=False
        ).values_list("token", flat=True)
    )
    return _send(tokens, title, body, data)


def notify_tenant_admins(tenant, title, body, data=None):
    from user.models import CustomUser

    tokens = list(
        DeviceToken.objects.filter(
            tenant=tenant,
            is_active=True,
            is_deleted=False,
            user__user_type=CustomUser.UserType.ADMIN,
        ).values_list("token", flat=True)
    )
    return _send(tokens, title, body, data)

"""
The two emails an invitation can produce.

Which one is sent is the *only* observable difference between inviting a fresh address
and inviting one that already has an account. The API answers identically either way,
because which addresses are registered on this platform is not something a tenant admin
gets to enumerate — see `tenant.services.invitations`.

Both follow the shape `forgot_password` already uses: open an `EmailDeliveryLog` row at
the call site, hand its id to `send_email_task`, and let the worker mark the outcome.
Neither is allowed to raise: a mail failure must not undo an invitation that has already
consumed a seat.
"""

import logging

from django.conf import settings

from user.models.email_delivery_log import EmailDeliveryLog
from user.services import email_log
from utils.tasks import send_email_task

logger = logging.getLogger(__name__)


def _from_email():
    return getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@voicebot.com")


def _frontend_base():
    """Where the app a person clicks through to lives.

    `FRONTEND_BASE_URL` first, for a deployment whose app sits somewhere other than the
    origin the API allows. Then `FRONTEND_PROJECT_URL`, which every environment defines
    and `FRONTEND_BASE_URL` — defined by none of them — silently shadowed: reading only
    the first is why invitations went out linking to a bare path with no host.
    """
    base = getattr(settings, "FRONTEND_BASE_URL", "") or getattr(
        settings, "FRONTEND_PROJECT_URL", ""
    )
    return (base or "").rstrip("/")


def accept_url(raw_token):
    """Where the invitee goes to finish signing up.

    Built from a configured frontend base rather than the request host: the API and the
    app are not the same origin, and a link pointing at the API is a dead end for a
    person. With neither setting configured this still returns a bare path — an
    obviously-wrong relative link rather than one pointing at somebody else's domain —
    and says so in the log, because the invitation is unusable either way.
    """
    base = _frontend_base()
    if not base:
        logger.error(
            "neither FRONTEND_BASE_URL nor FRONTEND_PROJECT_URL is set; the invitation "
            "link has no host and cannot be opened"
        )
    return f"{base}/accept-invitation?token={raw_token}"


def send_invitation(*, invitation, raw_token, request=None):
    """Mail a live invitation token. Returns True if it was queued."""
    try:
        delivery_log_id = email_log.record(
            EmailDeliveryLog.Purpose.TENANT_INVITATION,
            invitation.email,
            request=request,
            user=invitation.user,
            tenant_id=invitation.tenant_id,
        )

        organisation = getattr(invitation.tenant, "name", "your organisation")
        link = accept_url(raw_token)

        send_email_task.delay(
            subject=f"You have been invited to join {organisation}",
            message=(
                f"You have been invited to join {organisation} on VoiceBot.\n\n"
                f"Set your password to get started: {link}\n\n"
                f"This invitation expires on "
                f"{invitation.expires_at.strftime('%d %b %Y')}."
            ),
            from_email=_from_email(),
            recipient_list=[invitation.email],
            title="You're invited",
            lead_text=f"Join {organisation} on VoiceBot",
            body_text=(
                f"This invitation expires on "
                f"{invitation.expires_at.strftime('%d %b %Y')}."
            ),
            # The link as something to click, and printed underneath to copy. It lives
            # only in this mail, so the HTML part — the one a mail client shows — has to
            # carry it; a plain-text-only link reaches almost nobody.
            action_url=link,
            action_label="Set your password",
            delivery_log_id=delivery_log_id,
        )
        return True
    except Exception:
        # Logged without the token. The raw value exists only in the message body and
        # must not reach a log file, where it would outlive the invitation itself.
        logger.exception(
            "invitation email could not be queued for invitation %s", invitation.pk
        )
        return False


def send_already_registered(*, email, tenant, request=None):
    """Tell an existing account holder that somebody tried to add them.

    Sent instead of an invitation, and it deliberately carries no token and creates no
    account. The person learns they were invited; the inviter learns nothing.
    """
    try:
        delivery_log_id = email_log.record(
            EmailDeliveryLog.Purpose.TENANT_INVITATION,
            email,
            request=request,
            tenant_id=getattr(tenant, "id", None),
        )

        organisation = getattr(tenant, "name", "an organisation")

        send_email_task.delay(
            subject=f"{organisation} tried to add you on VoiceBot",
            message=(
                f"{organisation} tried to invite this address to their VoiceBot "
                "account, but it is already registered.\n\n"
                "Sign in with your existing account, or contact them if you expected "
                "to join a different organisation."
            ),
            from_email=_from_email(),
            recipient_list=[email],
            title="You already have an account",
            lead_text=f"{organisation} tried to add you",
            body_text=(
                "This address is already registered, so no new account was created. "
                "Sign in with your existing credentials."
            ),
            delivery_log_id=delivery_log_id,
        )
        return True
    except Exception:
        logger.exception("already-registered email could not be queued for %s", email)
        return False

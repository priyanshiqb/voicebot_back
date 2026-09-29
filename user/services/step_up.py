"""Step-up verification: a fresh authenticator code for one sensitive action.

Signing in to the admin already required a TOTP code (``AdminSiteOTPRequired``), but a
session can sit open for hours on an unattended laptop. Actions that create money or
customers ask again, at the moment of the action, so an open session alone is not
enough to perform them.

TOTP devices only. Static backup codes are for getting back into an account after
losing a phone, not for confirming an action; accepting them here would turn a
recovery mechanism into a second way to act.

``TOTPDevice.verify_token`` does the security-relevant work itself: it refuses a code
that is not newer than the last one it accepted (so a code cannot be replayed), and it
throttles after failures with a growing delay.
"""

from django_otp.plugins.otp_totp.models import TOTPDevice


class StepUpError(Exception):
    """Why a step-up code was not accepted, phrased for the operator."""


def verify_totp(user, token):
    """Accept ``token`` if it is a current code from one of ``user``'s confirmed TOTP
    devices. Returns the device that matched; raises ``StepUpError`` otherwise.
    """
    token = (token or "").replace(" ", "").strip()
    if not token:
        raise StepUpError("Enter the 6-digit code from your authenticator app.")
    if not token.isdigit():
        raise StepUpError("The code is digits only.")

    devices = list(TOTPDevice.objects.devices_for_user(user, confirmed=True))
    if not devices:
        raise StepUpError(
            "Your account has no authenticator app set up, so this action cannot be "
            "confirmed. Set one up under two-factor authentication first."
        )

    for device in devices:
        allowed, _details = device.verify_is_allowed()
        if not allowed:
            continue
        if device.verify_token(token):
            return device

    if not any(device.verify_is_allowed()[0] for device in devices):
        raise StepUpError(
            "Too many incorrect codes. Wait a moment before trying again."
        )
    raise StepUpError(
        "That code is incorrect or has already been used. Wait for the next code and "
        "try again."
    )

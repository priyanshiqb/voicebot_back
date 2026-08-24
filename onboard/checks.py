"""Deploy-time validation for payment settings whose failure mode is silent.

``manage.py check`` runs in CI and before every ``migrate``, which makes it the right
place to catch a payment configuration that would otherwise look like it was working.
Both checks below describe states that produce no error, no exception and no log line
— just money moving when it should not, or not moving when it should.
"""

import sys

from django.conf import settings
from django.core.checks import Error, Warning, register


def _is_test_run():
    """Whether this process is the test suite.

    Django forces ``DEBUG = False`` while testing, so "not DEBUG" cannot stand in for
    "production" here — without this, the checks below fail the very suite that has to
    run with signature verification stubbed. Same detection the payment views already
    use, so there is one notion of "this is a test" in the codebase rather than two.
    """
    return getattr(settings, "TESTING", False) or "test" in sys.argv


@register()
def check_payment_signature_verification(app_configs, **kwargs):
    """Signatures must be verified anywhere that is not obviously a dev box.

    ``PAYMENTS_ALLOW_UNSIGNED`` makes the webhook accept any request as genuine and
    stubs order creation. That is correct for local work and required by the test
    suite; in production it means anyone who can reach the endpoint can mint a paid
    subscription, and nothing anywhere would report a problem.

    Historically this was inferred from ``RAZORPAY_KEY_ID == "rzp_test_dummykey"``,
    which was also the settings default — so *forgetting* the credential silently
    disabled verification. Both the default and the inference are gone; this check
    guards the setting that replaced them.
    """
    if _is_test_run():
        return []

    if not settings.DEBUG and getattr(settings, "PAYMENTS_ALLOW_UNSIGNED", False):
        return [
            Error(
                "PAYMENTS_ALLOW_UNSIGNED is on while DEBUG is False. Razorpay "
                "webhooks and payment callbacks are being accepted without "
                "verifying their signature.",
                hint=(
                    "Remove PAYMENTS_ALLOW_UNSIGNED from this environment (it "
                    "defaults to False) and set RAZORPAY_WEBHOOK_SECRET to the "
                    "signing secret from the Razorpay dashboard."
                ),
                id="onboard.E001",
            )
        ]
    return []


@register()
def check_razorpay_webhook_secret(app_configs, **kwargs):
    """A secret is required whenever signatures are actually checked.

    Without one, ``verify_webhook_signature`` fails for every genuine delivery, so
    every webhook is rejected as unsigned — payments capture at Razorpay and nothing
    happens here. The first symptom is a customer reporting that they paid and got
    nothing, which is a slow and expensive way to discover a missing env var.
    """
    if _is_test_run() or getattr(settings, "PAYMENTS_ALLOW_UNSIGNED", False):
        return []

    if not getattr(settings, "RAZORPAY_WEBHOOK_SECRET", ""):
        message = (
            "RAZORPAY_WEBHOOK_SECRET is empty while signature verification is on. "
            "Every incoming webhook will be rejected."
        )
        hint = "Set RAZORPAY_WEBHOOK_SECRET, or set PAYMENTS_ALLOW_UNSIGNED=True for local work."
        # An Error outside DEBUG, a Warning inside it: a developer who has not set up
        # payments at all should not be blocked from running unrelated commands.
        problem = Error if not settings.DEBUG else Warning
        return [problem(message, hint=hint, id="onboard.E002" if not settings.DEBUG else "onboard.W002")]

    return []

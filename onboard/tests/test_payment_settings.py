"""Section 16 of the billing test plan — PS-01 to PS-14.

The security hole this section exists to keep closed:

``RAZORPAY_KEY_ID`` used to **default** to ``"rzp_test_dummykey"``, and three views
treated that exact value as "skip signature verification". A production deploy that
forgot the environment variable therefore accepted any unsigned webhook as genuine —
anyone able to POST to the endpoint could mint a paid subscription, and nothing
anywhere would report a problem.

Both the default and the inference are gone. Verification is now keyed on an explicit
``PAYMENTS_ALLOW_UNSIGNED`` flag that defaults to ``False``, guarded by production
settings and by ``manage.py check``. These tests are what stop either coming back.
"""

import ast
import io
import sys
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.checks import Error, Warning
from django.test import TestCase, override_settings

from onboard.checks import (
    check_payment_signature_verification,
    check_razorpay_webhook_secret,
)

#: The credential whose value used to decide whether signatures were checked.
DUMMY_KEY = "rzp_test_dummykey"

REPO = Path(settings.BASE_DIR)
BASE_SETTINGS = REPO / "config" / "settings" / "base.py"
PRODUCTION_SETTINGS = REPO / "config" / "settings" / "production.py"


def source(path):
    return io.open(path, encoding="utf-8").read()


def uses_dummy_key(path):
    """Whether this file *assigns* or *compares against* the dummy credential.

    Parsed rather than grepped. Both ``base.py`` and ``checks.py`` still explain the
    hole in prose, and deleting those comments would lose the only record of why
    there is no default here — but a docstring is not a code path, and the AST is
    what tells the two apart.
    """
    tree = ast.parse(source(path))

    def is_dummy(node):
        return isinstance(node, ast.Constant) and node.value == DUMMY_KEY

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            if any(is_dummy(operand) for operand in [node.left, *node.comparators]):
                return True
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and is_dummy(node.value):
            return True
        if isinstance(node, ast.keyword) and is_dummy(node.value):
            return True
    return False


class CredentialTests(TestCase):
    """PS-01 to PS-05 — no credential may have a working default."""

    def test_ps01_the_key_id_has_no_default(self):
        """PS-01: **Django must refuse to boot without it.**

        ``env.str("RAZORPAY_KEY_ID")`` with no ``default=`` raises at import. A
        missing credential has to be a deploy that fails loudly, not one that comes
        up and quietly accepts unsigned payments.
        """
        text = source(BASE_SETTINGS)

        self.assertIn('RAZORPAY_KEY_ID = env.str("RAZORPAY_KEY_ID")', text)
        self.assertNotIn('env.str("RAZORPAY_KEY_ID", default=', text)

    def test_ps02_the_key_secret_has_no_default(self):
        """PS-02."""
        text = source(BASE_SETTINGS)

        self.assertIn('RAZORPAY_KEY_SECRET = env.str("RAZORPAY_KEY_SECRET")', text)
        self.assertNotIn('env.str("RAZORPAY_KEY_SECRET", default=', text)

    def test_ps03_the_dummy_credential_is_no_longer_a_working_value(self):
        """PS-03: **the exact string that used to disable verification.**

        Matched as a *pattern* rather than as a substring: the settings file still
        explains the history in a comment, and deleting that comment would lose the
        only record of why there is no default here.
        """
        self.assertFalse(
            uses_dummy_key(BASE_SETTINGS),
            "the dummy credential is still being assigned or compared against",
        )
        self.assertNotEqual(settings.RAZORPAY_KEY_ID, "rzp_test_dummykey")

    def test_ps04_no_code_infers_anything_from_a_credential_value(self):
        """PS-04: **the inference, not just the default.**

        Comparing a key against a known dummy is what turned a missing environment
        variable into an open webhook. Nothing in the payment path may do it again.
        """
        offenders = []
        for path in (REPO / "onboard").rglob("*.py"):
            if "tests" in path.parts or path.name.startswith("test_"):
                continue
            if uses_dummy_key(path):
                offenders.append(str(path.relative_to(REPO)))

        self.assertEqual(
            offenders, [],
            "a payment credential is being compared against a hard-coded value",
        )

    def test_ps05_unsigned_payments_are_off_unless_asked_for(self):
        """PS-05: **fails closed.** The replacement flag must not repeat the
        original mistake by defaulting to permissive."""
        text = source(BASE_SETTINGS)

        self.assertIn(
            'PAYMENTS_ALLOW_UNSIGNED = env.bool("PAYMENTS_ALLOW_UNSIGNED", default=False)',
            text,
        )


class ProductionGuardTests(TestCase):
    """PS-06 to PS-07 — production settings refuse to import when misconfigured."""

    def test_ps06_production_refuses_to_start_with_verification_off(self):
        """PS-06: **at import**, so there is no window in which a production
        process runs with verification disabled."""
        text = source(PRODUCTION_SETTINGS)

        self.assertIn("if PAYMENTS_ALLOW_UNSIGNED:", text)
        self.assertIn("raise ImproperlyConfigured", text)

    def test_ps07_production_refuses_to_start_without_a_webhook_secret(self):
        """PS-07: without one every genuine webhook fails verification, so payments
        capture at Razorpay while nothing happens here."""
        text = source(PRODUCTION_SETTINGS)

        self.assertIn("if not RAZORPAY_WEBHOOK_SECRET:", text)
        guard = text[text.index("if not RAZORPAY_WEBHOOK_SECRET:"):]
        self.assertIn("ImproperlyConfigured", guard)


class DeployCheckTests(TestCase):
    """PS-08 to PS-14 — ``manage.py check`` catches what nothing else would."""

    def not_a_test_run(self):
        """Convince the checks they are running for real.

        Django forces ``DEBUG = False`` while testing, so the checks detect a test
        run explicitly — which means exercising them requires undoing that detection
        rather than flipping ``DEBUG``.
        """
        return patch.object(sys, "argv", ["manage.py", "check"])

    @override_settings(DEBUG=False, PAYMENTS_ALLOW_UNSIGNED=True, TESTING=False)
    def test_ps08_unsigned_payments_outside_debug_are_an_error(self):
        """PS-08: **anyone who can reach the endpoint could mint a paid
        subscription.**"""
        with self.not_a_test_run():
            problems = check_payment_signature_verification(None)

        self.assertEqual(len(problems), 1)
        self.assertIsInstance(problems[0], Error)
        self.assertEqual(problems[0].id, "onboard.E001")

    @override_settings(DEBUG=True, PAYMENTS_ALLOW_UNSIGNED=True, TESTING=False)
    def test_ps09_unsigned_payments_in_debug_are_fine(self):
        """PS-09: correct for local work, and required by this very suite."""
        with self.not_a_test_run():
            self.assertEqual(check_payment_signature_verification(None), [])

    @override_settings(
        DEBUG=False, PAYMENTS_ALLOW_UNSIGNED=False, RAZORPAY_WEBHOOK_SECRET="",
        TESTING=False,
    )
    def test_ps10_verifying_without_a_secret_is_an_error(self):
        """PS-10: **the opposite failure, and the more expensive one.**

        Every webhook is rejected as unsigned, so the first symptom is a customer
        reporting they paid and got nothing.
        """
        with self.not_a_test_run():
            problems = check_razorpay_webhook_secret(None)

        self.assertEqual(len(problems), 1)
        self.assertIsInstance(problems[0], Error)
        self.assertEqual(problems[0].id, "onboard.E002")

    @override_settings(
        DEBUG=True, PAYMENTS_ALLOW_UNSIGNED=False, RAZORPAY_WEBHOOK_SECRET="",
        TESTING=False,
    )
    def test_ps11_the_same_gap_in_debug_is_only_a_warning(self):
        """PS-11: a developer who has not set up payments at all should not be
        blocked from running unrelated commands."""
        with self.not_a_test_run():
            problems = check_razorpay_webhook_secret(None)

        self.assertEqual(len(problems), 1)
        self.assertIsInstance(problems[0], Warning)
        self.assertEqual(problems[0].id, "onboard.W002")

    @override_settings(
        DEBUG=False, PAYMENTS_ALLOW_UNSIGNED=False,
        RAZORPAY_WEBHOOK_SECRET="whsec_real", TESTING=False,
    )
    def test_ps12_a_correct_configuration_is_silent(self):
        """PS-12: a check that always fires is a check people learn to ignore."""
        with self.not_a_test_run():
            self.assertEqual(check_payment_signature_verification(None), [])
            self.assertEqual(check_razorpay_webhook_secret(None), [])

    @override_settings(DEBUG=False, PAYMENTS_ALLOW_UNSIGNED=True,
                       RAZORPAY_WEBHOOK_SECRET="")
    def test_ps13_the_checks_do_not_block_the_test_suite(self):
        """PS-13: **the trap these checks fell into the first time.**

        Django forces ``DEBUG = False`` during tests, so "not DEBUG" wrongly implied
        production and the checks failed the very suite that has to run with
        verification stubbed. Both are skipped for a test run — and this runs
        without patching ``sys.argv``, so it is exercising the real detection.
        """
        self.assertEqual(check_payment_signature_verification(None), [])
        self.assertEqual(check_razorpay_webhook_secret(None), [])

    def test_ps14_verification_is_keyed_on_the_flag_not_on_a_credential(self):
        """PS-14: the webhook and the order-creation path must agree, and both must
        read the flag rather than sniff a key."""
        from onboard.services.payments import gateway_is_live
        from onboard.views.onboarding.webhook import RazorpayWebhookView

        webhook_source = source(
            REPO / "onboard" / "views" / "onboarding" / "webhook.py"
        )
        self.assertIn("settings.PAYMENTS_ALLOW_UNSIGNED", webhook_source)
        self.assertNotIn("RAZORPAY_KEY_ID ==", webhook_source)

        payments_source = source(REPO / "onboard" / "services" / "payments.py")
        self.assertIn("settings.PAYMENTS_ALLOW_UNSIGNED", payments_source)
        self.assertNotIn("RAZORPAY_KEY_ID ==", payments_source)

        # And the two behave alike: with the flag on, neither talks to Razorpay.
        with override_settings(PAYMENTS_ALLOW_UNSIGNED=True, TESTING=False):
            with patch.object(sys, "argv", ["manage.py", "runserver"]):
                self.assertFalse(gateway_is_live())
                self.assertTrue(
                    RazorpayWebhookView()._verify(b"{}", "no-signature")
                )

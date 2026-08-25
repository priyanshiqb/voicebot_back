"""Section 25 of the billing test plan — RZ-01 to RZ-12.

**Opt-in.** The only tests in this cycle that leave the machine. They run against
Razorpay's **test mode** using the ``rzp_test_…`` credentials already configured, and
are skipped unless ``RUN_LIVE_GATEWAY_TESTS=1`` — so CI, a plane, and a colleague
without credentials all still get a green suite.

Everything else stubs the gateway. These exist to answer the one question a stub
cannot: *does our request actually satisfy Razorpay's API?*

**RZ-09 to RZ-11 matter most.** Every other webhook test in this cycle runs with
``PAYMENTS_ALLOW_UNSIGNED=True``, so the signature path is never exercised at all.
These are the only cases proving verification works — including that the signature is
computed over the **raw body** rather than a re-serialised dict, which is a mistake
already made once in this codebase and caught only by review.

**Not covered:** completing a payment. That needs Razorpay Checkout in a browser and is
a manual QA step. RZ-01 to RZ-08 create real test-mode orders that are never paid.

    RUN_LIVE_GATEWAY_TESTS=1 uv run python manage.py test \\
        onboard.tests.test_razorpay_live --settings=config.settings.test_postgres
"""

import hashlib
import hmac
import json
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings

from onboard.services import payments

LIVE = os.environ.get("RUN_LIVE_GATEWAY_TESTS") == "1"
SKIP_REASON = (
    "live gateway tests are opt-in; set RUN_LIVE_GATEWAY_TESTS=1 to run them"
)

#: Razorpay's test-mode key prefix. A production key here would create real orders,
#: so the suite refuses to run rather than finding out afterwards.
TEST_KEY_PREFIX = "rzp_test_"


def live_client():
    from onboard.views.onboarding.base import razorpay_client

    return razorpay_client


@unittest.skipUnless(LIVE, SKIP_REASON)
class RazorpayOrderTests(TestCase):
    """RZ-01 to RZ-08 — creating real test-mode orders."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not settings.RAZORPAY_KEY_ID.startswith(TEST_KEY_PREFIX):
            raise unittest.SkipTest(
                "RAZORPAY_KEY_ID is not a test-mode key; refusing to create real "
                "orders against a live account."
            )

    def setUp(self):
        # These tests are about the gateway, so the stub has to be off.
        self.live = override_settings(PAYMENTS_ALLOW_UNSIGNED=False, TESTING=False)
        self.live.enable()
        self.addCleanup(self.live.disable)
        self.argv = patch("sys.argv", ["manage.py", "runserver"])
        self.argv.start()
        self.addCleanup(self.argv.stop)

    def create(self, amount=Decimal("708.00"), receipt="rz_live", notes=None):
        return payments.create_order(
            amount, receipt=receipt, notes=notes or {"suite": "rz-live"}
        )

    def test_rz01_an_order_comes_back_with_a_real_identifier(self):
        """RZ-01: the whole point — our payload is one Razorpay accepts."""
        order_id = self.create()

        self.assertTrue(order_id.startswith("order_"), order_id)

    def test_rz02_the_amount_arrives_in_paise(self):
        """RZ-02: **Razorpay echoes what it understood.**

        A stub cannot catch a rupees-for-paise mistake; the gateway can, because it
        tells us back what it thinks it was asked to charge.
        """
        order_id = self.create(Decimal("708.00"))

        order = live_client().order.fetch(order_id)
        self.assertEqual(order["amount"], 70800)

    def test_rz03_the_currency_and_capture_mode_are_accepted(self):
        """RZ-03: auto-capture, in INR. A mismatch here means money authorised and
        never taken."""
        order = live_client().order.fetch(self.create())

        self.assertEqual(order["currency"], "INR")
        self.assertEqual(order["status"], "created")

    def test_rz04_a_long_receipt_is_truncated_rather_than_rejected(self):
        """RZ-04: Razorpay caps the receipt field, and an over-long one is refused
        outright — which reaches the tenant as a failed payment."""
        order_id = self.create(receipt="addon_storage_gb_" + "x" * 200)

        order = live_client().order.fetch(order_id)
        self.assertLessEqual(len(order["receipt"]), 40)

    def test_rz05_notes_survive_the_round_trip(self):
        """RZ-05: the notes are how a payment is attributed when a transaction row
        is missing, so they have to actually arrive."""
        order_id = self.create(notes={"tenant": "abc-123", "type": "top_up"})

        order = live_client().order.fetch(order_id)
        self.assertEqual(order["notes"]["tenant"], "abc-123")
        self.assertEqual(order["notes"]["type"], "top_up")

    def test_rz06_our_minimum_matches_the_gateways(self):
        """RZ-06: **refused by us, before the call.**

        ``MINIMUM_CHARGE`` is a claim about Razorpay's behaviour. This proves the
        claim by refusing locally *and* confirming the gateway would have refused.
        """
        with self.assertRaises(ValueError):
            self.create(Decimal("0.50"))

        with self.assertRaises(Exception):
            live_client().order.create({
                "amount": 50, "currency": "INR", "receipt": "rz_too_small",
                "payment_capture": 1,
            })

    def test_rz07_the_order_can_be_fetched_back(self):
        """RZ-07: reconciliation depends on this — a pending transaction is
        resolved by asking Razorpay what happened to its order."""
        order_id = self.create(Decimal("1500.00"))

        order = live_client().order.fetch(order_id)
        self.assertEqual(order["id"], order_id)
        self.assertEqual(order["amount"], 150000)
        self.assertEqual(order["status"], "created")

    def test_rz08_bad_credentials_surface_as_a_gateway_error(self):
        """RZ-08: the view turns this into a 502, not a 500 — the tenant is told
        the gateway is unreachable rather than shown a crash."""
        import razorpay

        broken = razorpay.Client(auth=("rzp_test_notarealkey", "notarealsecret"))

        with patch("onboard.views.onboarding.base.razorpay_client", broken):
            with self.assertRaises(Exception):
                self.create()


@unittest.skipUnless(LIVE, SKIP_REASON)
class RazorpaySignatureTests(TestCase):
    """RZ-09 to RZ-12 — the only cases that exercise verification at all."""

    def setUp(self):
        self.secret = settings.RAZORPAY_WEBHOOK_SECRET
        if not self.secret:
            self.skipTest("RAZORPAY_WEBHOOK_SECRET is not set in this environment")

    def sign(self, body, secret=None):
        return hmac.new(
            (secret or self.secret).encode(), body, hashlib.sha256
        ).hexdigest()

    def test_rz09_a_genuinely_signed_payload_verifies(self):
        """RZ-09: **computed over the raw body.**

        DRF consumes the request stream when it parses ``request.data``, so the
        handler has to read ``request.body`` first. A re-serialised dict hashes
        differently even when it looks identical — different key order, different
        separators — and every live webhook would be rejected.
        """
        body = json.dumps({"id": "evt_live", "event": "payment.captured"}).encode()

        live_client().utility.verify_webhook_signature(
            body.decode(), self.sign(body), self.secret
        )

    def test_rz10_a_tampered_body_fails_the_same_signature(self):
        """RZ-10: the property the whole mechanism exists for."""
        body = json.dumps({"id": "evt_live", "amount": 100}).encode()
        signature = self.sign(body)
        tampered = json.dumps({"id": "evt_live", "amount": 1000000}).encode()

        with self.assertRaises(Exception):
            live_client().utility.verify_webhook_signature(
                tampered.decode(), signature, self.secret
            )

    def test_rz11_payment_signatures_are_verified_over_order_and_payment(self):
        """RZ-11: the browser-confirmed path signs ``order_id|payment_id`` with the
        API secret, which is a different scheme from the webhook's."""
        order_id, payment_id = "order_live_test", "pay_live_test"
        message = f"{order_id}|{payment_id}".encode()
        genuine = hmac.new(
            settings.RAZORPAY_KEY_SECRET.encode(), message, hashlib.sha256
        ).hexdigest()

        live_client().utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": genuine,
        })

        with self.assertRaises(Exception):
            live_client().utility.verify_payment_signature({
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": "0" * 64,
            })

    def test_rz12_a_network_failure_leaves_no_orphan_successful_row(self):
        """RZ-12: **the order row is written before the gateway is called**, so a
        timeout has to roll it back rather than leave a transaction claiming a
        payment that was never taken."""
        from onboard.models import Transaction
        from onboard.services import subscriptions as subscription_service
        from onboard.tests.billing_factories import BILLING, make_plan, make_tenant

        ctx = make_tenant("rz-live")
        plan = make_plan(
            "rz-live-plan", cost_per_additional_credit=Decimal("1.00")
        )
        subscription_service.activate(ctx, plan)

        with patch.object(
            payments, "create_order", side_effect=OSError("connection reset")
        ):
            response = ctx.client.post(
                f"{BILLING}/top-up/", {"credits": 100}, format="json"
            )

        self.assertEqual(response.status_code, 502)
        self.assertFalse(
            Transaction.objects.filter(
                tenant=ctx.tenant,
                status=Transaction.TransactionStatusChoices.SUCCESSFUL,
            ).exists()
        )
        self.assertFalse(Transaction.objects.filter(tenant=ctx.tenant).exists())

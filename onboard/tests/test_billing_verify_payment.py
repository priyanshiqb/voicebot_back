"""`POST /billing/verify-payment/` — the browser's side of a purchase.

The endpoint exists to close a gap the frontend could not: Checkout reports success,
and until now the effect appeared only when Razorpay's server-to-server call landed.

Everything here is really about one property — **this and the webhook must never both
apply the same payment, and either one alone must apply it fully.**
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from rest_framework import status

from onboard.constants.entitlement_resource import EntitlementResource as Resource
from onboard.models import Transaction
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    assert_invariant,
    ledger_sums,
    make_plan,
    make_tenant,
)

TxnType = Transaction.TransactionTypeChoices
TxnStatus = Transaction.TransactionStatusChoices

URL = "/api/v1/billing/verify-payment/"

GOOD = {
    "razorpay_order_id": "",  # filled per test
    "razorpay_payment_id": "pay_test_123",
    "razorpay_signature": "sig_test_123",
}


class VerifyPaymentTests(TestCase):
    def setUp(self):
        self.ctx = make_tenant("verify")
        self.tenant = self.ctx.tenant
        self.client = self.ctx.client
        self.plan = make_plan("verify-plan")
        self.subscription = subscription_service.activate(self.tenant, self.plan)

    # -- helpers ----------------------------------------------------------- #

    def order(self, *, txn_type=TxnType.TOP_UP, credits=500, tenant=None,
              status_=TxnStatus.PENDING, **extra):
        return Transaction.objects.create(
            razorpay_order_id=f"order_{txn_type}_{credits}_{id(self)}",
            amount=Decimal("590.00"),
            status=status_,
            transaction_type=txn_type,
            tenant=tenant or self.tenant,
            subscription=self.subscription,
            plan=self.plan,
            credits_purchased=credits,
            **extra,
        )

    def verify(self, transaction, **overrides):
        body = dict(GOOD, razorpay_order_id=transaction.razorpay_order_id)
        body.update(overrides)
        return self.client.post(URL, body, format="json")

    def purchased(self):
        return ledger_sums(self.tenant)[2]

    # -- the happy path ---------------------------------------------------- #

    def test_it_applies_a_top_up_the_webhook_has_not_seen(self):
        txn = self.order(credits=500)
        before = self.purchased()

        response = self.verify(txn)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["data"]["already_applied"])
        self.assertEqual(self.purchased(), before + 500)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TxnStatus.SUCCESSFUL)
        self.assertEqual(txn.razorpay_payment_id, "pay_test_123")
        assert_invariant(self, self.tenant)

    def test_it_returns_the_new_balance_so_no_second_call_is_needed(self):
        """The entire point is removing a round trip. Making the caller follow up
        with GET /billing/subscription/ would put it straight back."""
        txn = self.order(credits=750)

        data = self.verify(txn).data["data"]

        self.assertIsNotNone(data["credits"])
        self.assertIsNotNone(data["entitlements"])
        resources = {row["resource"] for row in data["entitlements"]}
        self.assertIn(Resource.CAMPAIGN_MONTHLY, resources)

    # -- the race with the webhook ----------------------------------------- #

    def test_a_payment_the_webhook_already_applied_is_not_applied_twice(self):
        """The webhook won. This must report success without double-crediting —
        the tenant paid once."""
        txn = self.order(credits=500)
        self.verify(txn)  # stands in for the webhook
        after_first = self.purchased()

        response = self.verify(txn)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["data"]["already_applied"])
        self.assertEqual(self.purchased(), after_first)
        assert_invariant(self, self.tenant)

    def test_calling_it_five_times_credits_once(self):
        txn = self.order(credits=200)
        before = self.purchased()

        for _ in range(5):
            self.assertEqual(self.verify(txn).status_code, status.HTTP_200_OK)

        self.assertEqual(self.purchased(), before + 200)
        assert_invariant(self, self.tenant)

    def test_the_webhook_after_this_endpoint_changes_nothing(self):
        """The other order of the same race. `_on_captured` returns early on a
        transaction already marked successful."""
        from onboard.views.onboarding.webhook import RazorpayWebhookView

        txn = self.order(credits=400)
        self.verify(txn)
        after_verify = self.purchased()

        RazorpayWebhookView()._on_captured({
            "payload": {"payment": {"entity": {
                "id": "pay_test_123", "order_id": txn.razorpay_order_id,
            }}}
        })

        self.assertEqual(self.purchased(), after_verify)
        assert_invariant(self, self.tenant)

    # -- what it refuses --------------------------------------------------- #

    def test_a_bad_signature_is_refused_and_the_order_marked_failed(self):
        txn = self.order()

        with patch("onboard.services.payments.verify_payment_signature",
                   return_value=False):
            response = self.verify(txn)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TxnStatus.FAILED)
        self.assertEqual(self.purchased(), 0)

    def test_another_tenants_order_reads_as_missing(self):
        """Not 403. Distinguishing "not yours" from "does not exist" is an
        enumeration oracle over every order id in the system."""
        other = make_tenant("verify-other")
        txn = self.order(tenant=other.tenant)

        response = self.verify(txn)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("TRANSACTION_NOT_FOUND", response.data["errors"])

    def test_an_unknown_order_reads_as_missing(self):
        response = self.client.post(
            URL, dict(GOOD, razorpay_order_id="order_never_existed"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_signup_payment_is_sent_to_the_onboarding_flow(self):
        """An ONBOARDING transaction belongs to a TempUser. Applying it here would
        try to promote a registrant this tenant is not."""
        txn = self.order(txn_type=TxnType.ONBOARDING)

        response = self.verify(txn)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("WRONG_ENDPOINT", response.data["errors"])

    def test_an_expired_order_is_refused_rather_than_applied(self):
        """The sweep canceled it before the browser came back. If money was taken
        this needs a human, and applying it against a dead order is worse."""
        txn = self.order(status_=TxnStatus.CANCELED)

        response = self.verify(txn)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("ORDER_EXPIRED", response.data["errors"])
        self.assertEqual(self.purchased(), 0)

    def test_missing_fields_are_a_400(self):
        response = self.client.post(
            URL, {"razorpay_order_id": "order_x"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_it_requires_authentication(self):
        from rest_framework.test import APIClient

        txn = self.order()
        response = APIClient().post(
            URL, dict(GOOD, razorpay_order_id=txn.razorpay_order_id), format="json"
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    # -- the other purchase types ------------------------------------------ #

    def test_it_applies_an_addon(self):
        from onboard.models import TenantEntitlement

        txn = self.order(
            txn_type=TxnType.ADDON,
            credits=0,
            amount_breakdown={"resource": Resource.AGENT, "additional_units": 3},
        )

        response = self.verify(txn)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = TenantEntitlement.objects.get(
            tenant=self.tenant, resource=Resource.AGENT
        )
        self.assertEqual(row.additional_quantity, 3)

    def test_a_top_up_order_with_no_credits_is_recorded_but_grants_nothing(self):
        """Loud in the log, not an exception. The payment is real and already
        recorded; refusing it here would lose track of money."""
        txn = self.order(credits=0)

        with self.assertLogs("onboard.services.payment_application", level="ERROR"):
            response = self.verify(txn)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.purchased(), 0)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TxnStatus.SUCCESSFUL)

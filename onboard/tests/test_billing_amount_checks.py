"""Checking that what was paid is what was owed.

Two paths, two different things they can prove, and that asymmetry is the whole
design:

* The **webhook** signature is an HMAC over the raw request body, so the amount in
  the payload is authenticated. Comparing it costs nothing.
* The **browser** signature covers only ``order_id|payment_id``. The amount is not in
  the signed material and is never sent, so the gateway is the only source — which is
  also the only way to learn whether the payment was *captured* rather than merely
  authorised.

Underpayment is not reachable through Razorpay as the orders are currently created
(fixed amount, partial payments off), so these guard against our own pricing bugs, an
order edited in the dashboard, and partial payments being switched on without this
code knowing.
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from rest_framework import status

from onboard.models import Transaction
from onboard.services import payment_application, subscriptions as subscription_service
from onboard.tests.billing_factories import ledger_sums, make_plan, make_tenant
from onboard.views.onboarding.webhook import RazorpayWebhookView

TxnType = Transaction.TransactionTypeChoices
TxnStatus = Transaction.TransactionStatusChoices

VERIFY = "/api/v1/billing/verify-payment/"


class AmountCheckUnitTests(TestCase):
    """`check_amount` in isolation — the rule both paths share."""

    def setUp(self):
        self.txn = Transaction(
            razorpay_order_id="order_unit", amount=Decimal("1180.00")
        )

    def test_the_exact_amount_passes(self):
        self.assertEqual(payment_application.check_amount(self.txn, 118000), "ok")

    def test_a_single_paisa_short_is_underpaid(self):
        """Zero tolerance. Razorpay reports integers and we send integers, so a
        genuine payment matches exactly — a tolerance would only mask a rounding bug
        of our own."""
        with self.assertLogs("onboard.services.payment_application", level="ERROR"):
            self.assertEqual(
                payment_application.check_amount(self.txn, 117999), "underpaid"
            )

    def test_paying_more_is_flagged_but_allowed(self):
        """The tenant paid more than they owed. Withholding what they bought as well
        would be the worse of the two mistakes."""
        with self.assertLogs("onboard.services.payment_application", level="ERROR"):
            self.assertEqual(
                payment_application.check_amount(self.txn, 118001), "overpaid"
            )

    def test_an_unavailable_amount_is_unknown_not_a_failure(self):
        self.assertEqual(payment_application.check_amount(self.txn, None), "unknown")

    def test_rupees_convert_to_paise_without_float_drift(self):
        txn = Transaction(razorpay_order_id="o", amount=Decimal("2999.99"))
        self.assertEqual(payment_application.expected_paise(txn), 299999)


class WebhookAmountTests(TestCase):
    """The webhook checks the amount from its own signed payload."""

    def setUp(self):
        self.ctx = make_tenant("amt-wh")
        self.tenant = self.ctx.tenant
        self.plan = make_plan("amt-wh-plan")
        self.subscription = subscription_service.activate(self.tenant, self.plan)

    def order(self, amount="590.00", credits=500):
        return Transaction.objects.create(
            razorpay_order_id=f"order_wh_{credits}_{id(self)}",
            amount=Decimal(amount),
            status=TxnStatus.PENDING,
            transaction_type=TxnType.TOP_UP,
            tenant=self.tenant,
            subscription=self.subscription,
            plan=self.plan,
            credits_purchased=credits,
        )

    def captured(self, txn, paise):
        return {"payload": {"payment": {"entity": {
            "id": "pay_wh_1", "order_id": txn.razorpay_order_id, "amount": paise,
        }}}}

    def purchased(self):
        return ledger_sums(self.tenant)[2]

    def test_the_right_amount_is_applied(self):
        txn = self.order(credits=500)

        RazorpayWebhookView()._on_captured(self.captured(txn, 59000))

        txn.refresh_from_db()
        self.assertEqual(txn.status, TxnStatus.SUCCESSFUL)
        self.assertEqual(self.purchased(), 500)

    def test_an_underpayment_grants_nothing(self):
        txn = self.order(credits=500)

        with self.assertLogs("onboard.services.payment_application", level="ERROR"):
            RazorpayWebhookView()._on_captured(self.captured(txn, 100))

        txn.refresh_from_db()
        self.assertEqual(txn.status, TxnStatus.UNDERPAID)
        self.assertEqual(self.purchased(), 0)

    def test_an_underpayment_is_not_marked_failed(self):
        """FAILED would say no money changed hands. It did — a human has to choose
        between refunding it and collecting the difference."""
        txn = self.order()

        with self.assertLogs("onboard.services.payment_application", level="ERROR"):
            RazorpayWebhookView()._on_captured(self.captured(txn, 100))

        txn.refresh_from_db()
        self.assertNotEqual(txn.status, TxnStatus.FAILED)
        self.assertEqual(txn.razorpay_payment_id, "pay_wh_1")

    def test_an_overpayment_is_applied_and_logged(self):
        txn = self.order(credits=500)

        with self.assertLogs("onboard.services.payment_application", level="ERROR"):
            RazorpayWebhookView()._on_captured(self.captured(txn, 99999))

        txn.refresh_from_db()
        self.assertEqual(txn.status, TxnStatus.SUCCESSFUL)
        self.assertEqual(self.purchased(), 500)

    def test_a_payload_with_no_amount_still_applies(self):
        """Fail-open. A payload shape we do not recognise must not cost a tenant a
        purchase they paid for."""
        txn = self.order(credits=500)
        event = self.captured(txn, 0)
        del event["payload"]["payment"]["entity"]["amount"]

        RazorpayWebhookView()._on_captured(event)

        txn.refresh_from_db()
        self.assertEqual(txn.status, TxnStatus.SUCCESSFUL)
        self.assertEqual(self.purchased(), 500)

    def test_an_underpaid_order_does_not_block_the_next_purchase(self):
        """Only PENDING blocks. An underpaid order is settled as far as the
        one-open-order rule is concerned, even though nothing was granted."""
        txn = self.order()
        with self.assertLogs("onboard.services.payment_application", level="ERROR"):
            RazorpayWebhookView()._on_captured(self.captured(txn, 100))

        response = self.ctx.client.post(
            "/api/v1/billing/top-up/", {"credits": 100}, format="json"
        )

        self.assertNotEqual(response.status_code, status.HTTP_409_CONFLICT)


class VerifyPaymentGatewayTests(TestCase):
    """The browser path has to ask Razorpay, because its signature proves neither
    the amount nor that the payment was captured."""

    def setUp(self):
        self.ctx = make_tenant("amt-vp")
        self.tenant = self.ctx.tenant
        self.client = self.ctx.client
        self.plan = make_plan("amt-vp-plan")
        self.subscription = subscription_service.activate(self.tenant, self.plan)
        self.txn = Transaction.objects.create(
            razorpay_order_id=f"order_vp_{id(self)}",
            amount=Decimal("590.00"),
            status=TxnStatus.PENDING,
            transaction_type=TxnType.TOP_UP,
            tenant=self.tenant,
            subscription=self.subscription,
            plan=self.plan,
            credits_purchased=500,
        )

    def verify(self):
        return self.client.post(VERIFY, {
            "razorpay_order_id": self.txn.razorpay_order_id,
            "razorpay_payment_id": "pay_vp_1",
            "razorpay_signature": "sig",
        }, format="json")

    def gateway(self, **overrides):
        entity = {
            "id": "pay_vp_1",
            "order_id": self.txn.razorpay_order_id,
            "status": "captured",
            "amount": 59000,
        }
        entity.update(overrides)
        return patch("onboard.services.payments.fetch_payment", return_value=entity)

    def purchased(self):
        return ledger_sums(self.tenant)[2]

    def test_a_matching_captured_payment_is_applied(self):
        with self.gateway():
            response = self.verify()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.purchased(), 500)

    def test_an_underpayment_is_refused_and_grants_nothing(self):
        with self.gateway(amount=100):
            with self.assertLogs("onboard.services.payment_application",
                                 level="ERROR"):
                response = self.verify()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("AMOUNT_MISMATCH", response.data["errors"])
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, TxnStatus.UNDERPAID)
        self.assertEqual(self.purchased(), 0)

    def test_an_authorised_but_uncaptured_payment_is_refused(self):
        """Money held, not taken. Granting credits against one means granting them
        for a payment that can still be voided — and a valid signature does not
        distinguish the two, which is the hole this closes."""
        with self.gateway(status="authorized"):
            response = self.verify()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("PAYMENT_NOT_CAPTURED", response.data["errors"])
        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, TxnStatus.PENDING)
        self.assertEqual(self.purchased(), 0)

    def test_an_uncaptured_payment_stays_pending_so_the_webhook_can_finish_it(self):
        """It is not a failure — the payment may still capture, and then the webhook
        applies it. Marking it terminal here would strand a real payment."""
        with self.gateway(status="authorized"):
            self.verify()

        self.txn.refresh_from_db()
        self.assertEqual(self.txn.status, TxnStatus.PENDING)

    def test_a_payment_belonging_to_another_order_is_refused(self):
        with self.gateway(order_id="order_someone_else"):
            with self.assertLogs("onboard.views.billing.verify", level="ERROR"):
                response = self.verify()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("PAYMENT_ORDER_MISMATCH", response.data["errors"])
        self.assertEqual(self.purchased(), 0)

    def test_an_unreachable_gateway_applies_on_the_signature_alone(self):
        """Fail-open, and deliberately so. In development Razorpay cannot reach this
        host, so no webhook ever arrives and this endpoint is the only thing that
        applies a purchase. Refusing because our own outbound call had a blip would
        mean a tenant who paid gets nothing, with nothing behind us to fix it."""
        with patch("onboard.services.payments.fetch_payment", return_value=None):
            with self.assertLogs("onboard.views.billing.verify", level="INFO"):
                response = self.verify()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.purchased(), 500)

    def test_an_overpayment_is_applied(self):
        with self.gateway(amount=99999):
            with self.assertLogs("onboard.services.payment_application",
                                 level="ERROR"):
                response = self.verify()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.purchased(), 500)

    def test_the_gateway_is_not_consulted_for_an_already_applied_payment(self):
        """The webhook won the race. Re-fetching would be a pointless API call on
        every duplicate confirmation the browser sends."""
        with self.gateway():
            self.verify()

        with patch("onboard.services.payments.fetch_payment") as fetch:
            response = self.verify()

        fetch.assert_not_called()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["data"]["already_applied"])

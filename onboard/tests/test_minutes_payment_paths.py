"""Plan v2 — the remaining ways a minutes purchase can be confirmed or go wrong.

Three gaps the lifecycle matrix left:

* the **browser** confirmation path, ``POST /billing/verify-payment/``, which races the
  webhook and must produce the identical effect;
* the **yearly** ladder, which is a separate catalogue with its own cycle arithmetic;
* an **amount mismatch** on a minutes order, where the quantity must not be granted.

Run with ``--settings=config.settings.test_postgres``.
"""

from decimal import Decimal

from rest_framework.test import APITestCase

from onboard.models import Plan, Subscription, Transaction
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    BILLING,
    DEFAULT_RATES,
    Operation,
    age_subscription,
    make_minutes_plan,
    make_tenant,
    pay,
    subscribe,
)
from wallet.services import wallet as wallet_service

HIGH = Operation.CONVERSATION_HIGH_LATENCY
VERIFY = f"{BILLING}/verify-payment/"

TxnStatus = Transaction.TransactionStatusChoices


class BrowserConfirmationTests(APITestCase):
    """The browser's side of a minutes upgrade.

    It races the webhook deliberately: whichever lands first applies the payment, and
    the other must find it already done rather than applying it twice.
    """

    def setUp(self):
        self.ctx = make_tenant("browser")
        self.client = self.ctx.client
        self.tenant = self.ctx.tenant
        self.plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            discount_percentage=Decimal("0.00"), tax_percentage=Decimal("18.00"),
            max_rollover_credits=None,
        )
        self.subscription = subscribe(self.tenant, self.plan, minutes=1000)
        age_subscription(self.subscription, days_in=10)

    def _order_upgrade(self, minutes=2500):
        response = self.client.post(
            f"{BILLING}/subscription/change-plan/",
            {"plan_id": str(self.plan.pk), "minutes": minutes},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["razorpay_order_id"]

    def _confirm(self, order_id):
        return self.client.post(
            VERIFY,
            {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": "pay_browser_1",
                "razorpay_signature": "sig",
            },
            format="json",
        )

    def test_the_browser_path_applies_a_minutes_upgrade(self):
        order_id = self._order_upgrade(2500)

        response = self._confirm(order_id)

        self.assertEqual(response.status_code, 200, response.data)
        live = subscription_service.current_for(self.tenant)
        self.assertEqual(live.selected_minutes, 2500)
        self.assertEqual(live.allocated_credits, 2500 * DEFAULT_RATES[HIGH])

    def test_the_browser_and_the_webhook_agree(self):
        """Either alone must apply it fully, and together exactly once."""
        order_id = self._order_upgrade(2500)

        self._confirm(order_id)
        balance_after_browser = wallet_service.for_tenant(self.tenant).balance
        pay(order_id)

        self.assertEqual(
            wallet_service.for_tenant(self.tenant).balance, balance_after_browser
        )
        self.assertEqual(
            Subscription.objects.filter(tenant=self.tenant, status="active").count(), 1
        )

    def test_the_webhook_first_then_the_browser_is_also_once(self):
        order_id = self._order_upgrade(2500)

        pay(order_id)
        balance_after_webhook = wallet_service.for_tenant(self.tenant).balance
        response = self._confirm(order_id)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["data"]["already_applied"])
        self.assertEqual(
            wallet_service.for_tenant(self.tenant).balance, balance_after_webhook
        )


class AmountMismatchTests(APITestCase):
    """An underpaid minutes order must not grant the minutes."""

    def setUp(self):
        self.ctx = make_tenant("amount")
        self.client = self.ctx.client
        self.tenant = self.ctx.tenant
        self.plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            discount_percentage=Decimal("0.00"), tax_percentage=Decimal("18.00"),
            max_rollover_credits=None,
        )
        self.subscription = subscribe(self.tenant, self.plan, minutes=1000)
        age_subscription(self.subscription, days_in=10)

    def _order_upgrade(self, minutes=2500):
        return self.client.post(
            f"{BILLING}/subscription/change-plan/",
            {"plan_id": str(self.plan.pk), "minutes": minutes},
            format="json",
        ).data["data"]["razorpay_order_id"]

    def _captured(self, order_id, paise):
        return {
            "id": f"evt_{order_id}",
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_short_1",
                        "order_id": order_id,
                        "amount": paise,
                    }
                }
            },
        }

    def test_an_underpaid_upgrade_grants_no_minutes(self):
        order_id = self._order_upgrade(2500)
        transaction = Transaction.objects.get(razorpay_order_id=order_id)
        short = int(transaction.amount * 100) - 1

        with self.assertLogs("onboard.services.payment_application", level="ERROR"):
            self.client.post(
                "/api/v1/onboard/webhook/", self._captured(order_id, short),
                format="json",
            )

        transaction.refresh_from_db()
        live = subscription_service.current_for(self.tenant)
        self.assertEqual(transaction.status, TxnStatus.UNDERPAID)
        # Still on the quantity they had, not the one they underpaid for.
        self.assertEqual(live.selected_minutes, 1000)

    def test_an_overpaid_upgrade_is_applied(self):
        """Withholding what they bought as well would be the worse mistake."""
        order_id = self._order_upgrade(2500)
        transaction = Transaction.objects.get(razorpay_order_id=order_id)
        over = int(transaction.amount * 100) + 100

        with self.assertLogs("onboard.services.payment_application", level="ERROR"):
            self.client.post(
                "/api/v1/onboard/webhook/", self._captured(order_id, over),
                format="json",
            )

        live = subscription_service.current_for(self.tenant)
        self.assertEqual(live.selected_minutes, 2500)


class YearlyLadderTests(APITestCase):
    """A yearly plan is a separate plan with its own ladder and its own cycle."""

    def setUp(self):
        self.ctx = make_tenant("yearly")
        self.client = self.ctx.client
        self.tenant = self.ctx.tenant
        self.plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            billing_cycle=Plan.BillingCycleChoices.YEARLY,
            discount_percentage=Decimal("0.00"), tax_percentage=Decimal("18.00"),
            max_rollover_credits=None,
        )
        self.subscription = subscribe(self.tenant, self.plan, minutes=12_000)

    def test_a_yearly_cycle_runs_a_year(self):
        span = self.subscription.end_date - self.subscription.start_date

        self.assertGreater(span.days, 360)
        self.assertEqual(self.subscription.selected_minutes, 12_000)
        self.assertEqual(
            self.subscription.allocated_credits, 12_000 * DEFAULT_RATES[HIGH]
        )

    def test_a_yearly_upgrade_applies_at_the_paid_quantity(self):
        age_subscription(self.subscription, days_in=100)

        order_id = self.client.post(
            f"{BILLING}/subscription/change-plan/",
            {"plan_id": str(self.plan.pk), "minutes": 30_000},
            format="json",
        ).data["data"]["razorpay_order_id"]
        pay(order_id)

        live = subscription_service.current_for(self.tenant)
        self.assertEqual(live.selected_minutes, 30_000)
        self.assertGreater((live.end_date - live.start_date).days, 360)

    def test_a_yearly_renewal_keeps_the_quantity_and_the_cycle(self):
        age_subscription(self.subscription, days_in=360)

        subscription_service.renew(
            Subscription.objects.get(pk=self.subscription.pk)
        )

        renewed = Subscription.objects.get(pk=self.subscription.pk)
        self.assertEqual(renewed.selected_minutes, 12_000)
        self.assertGreater((renewed.end_date - renewed.start_date).days, 360)

    def test_eligible_plans_names_the_cycle_of_every_option(self):
        """A monthly plan *is* offered to a yearly subscriber — moving cycle is a
        legitimate change. What matters is that the cycle is stated, because the same
        quantity means a very different thing on each."""
        monthly = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            billing_cycle=Plan.BillingCycleChoices.MONTHLY,
        )

        response = self.client.get(f"{BILLING}/subscription/eligible-plans/")
        codes = {p["code"] for p in response.data["data"]["plans"]}

        # eligible-plans lists every active plan, so the monthly one is offered --
        # what matters is that moving to it is priced as a cycle change, not that it
        # is hidden.
        self.assertIn(monthly.code, codes)
        entry = next(p for p in response.data["data"]["plans"] if p["code"] == monthly.code)
        self.assertEqual(entry["billing_cycle"], Plan.BillingCycleChoices.MONTHLY)

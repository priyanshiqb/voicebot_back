"""Plan v2 — changing plan or quantity after signup.

The sign test already decided upgrade from downgrade; these tests are about the
quantity travelling correctly through the API and the scheduler. The case that needs
the most care is a downgrade that moves **no plan at all** — fewer minutes of what you
already have — because there is nothing in ``scheduled_plan`` to express it.

Run with ``--settings=config.settings.test_postgres``.
"""

from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from onboard.models import Subscription, Transaction
from onboard.services import subscriptions as subscription_service
from onboard.tasks.billing_cycle import process_billing_cycle
from onboard.tests.billing_factories import (
    BILLING,
    DEFAULT_RATES,
    Operation,
    age_subscription,
    make_minutes_plan,
    make_tenant,
    subscribe,
)

HIGH = Operation.CONVERSATION_HIGH_LATENCY


class MinutesPlanChangeTests(APITestCase):

    def setUp(self):
        self.ctx = make_tenant()
        self.client = self.ctx.client
        self.plan = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            discount_percentage=Decimal("0.00"), tax_percentage=Decimal("18.00"),
            sort_order=1,
        )
        self.subscription = subscribe(self.ctx.tenant, self.plan, minutes=1000)
        age_subscription(self.subscription, days_in=10)

    def _preview(self, **payload):
        return self.client.post(
            f"{BILLING}/subscription/preview-change/", payload, format="json"
        )

    def _change(self, **payload):
        return self.client.post(
            f"{BILLING}/subscription/change-plan/", payload, format="json"
        )

    # -- more minutes of the same plan ------------------------------------- #

    def test_more_minutes_of_the_same_plan_is_an_upgrade(self):
        response = self._change(plan_id=str(self.plan.pk), minutes=2500)

        self.assertEqual(response.status_code, 200)
        self.assertIn("razorpay_order_id", response.data["data"])
        order = Transaction.objects.get(
            transaction_type=Transaction.TransactionTypeChoices.UPGRADE
        )
        self.assertEqual(order.amount_breakdown["selected_minutes"], "2500")

    def test_the_same_plan_and_the_same_minutes_is_still_refused(self):
        """The only request with nothing in it."""
        response = self._change(plan_id=str(self.plan.pk), minutes=1000)

        self.assertEqual(response.status_code, 409)
        self.assertIn("PLAN_UNCHANGED", response.data["errors"])

    def test_omitting_minutes_on_the_current_plan_is_refused(self):
        response = self._change(plan_id=str(self.plan.pk))

        self.assertEqual(response.status_code, 409)

    def test_preview_prices_a_quantity_change_without_side_effects(self):
        response = self._preview(plan_id=str(self.plan.pk), minutes=2500)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["money"]["selected_minutes"], "2500")
        self.assertEqual(
            response.data["data"]["credits"]["new_plan_allocation"],
            2500 * DEFAULT_RATES[HIGH],
        )
        self.assertFalse(Transaction.objects.exists())

    # -- fewer minutes: the case with no plan to move ---------------------- #

    def test_fewer_minutes_schedules_rather_than_charging(self):
        response = self._change(plan_id=str(self.plan.pk), minutes=200)

        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(data["change_type"], "scheduled_change")
        self.assertEqual(data["selected_minutes"], 200)
        self.assertFalse(data["charged"])

    def test_the_scheduled_quantity_is_recorded(self):
        """``scheduled_plan`` alone cannot express 'fewer minutes of what I have' —
        it would hold the plan the tenant is already on."""
        self._change(plan_id=str(self.plan.pk), minutes=200)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.scheduled_plan_id, self.plan.pk)
        self.assertEqual(self.subscription.scheduled_minutes, 200)

    def test_a_scheduled_quantity_downgrade_actually_fires(self):
        """The regression this field exists to prevent: without it the change fires
        at cycle end having moved nothing."""
        self._change(plan_id=str(self.plan.pk), minutes=200)
        subscription = Subscription.objects.get(pk=self.subscription.pk)
        subscription.scheduled_change_at = timezone.now() - timezone.timedelta(minutes=1)
        subscription.save(update_fields=["scheduled_change_at"])

        process_billing_cycle()

        live = subscription_service.current_for(self.ctx.tenant)
        self.assertEqual(live.selected_minutes, 200)
        self.assertEqual(live.allocated_credits, 200 * DEFAULT_RATES[HIGH])
        self.assertNotEqual(live.pk, self.subscription.pk)

    def test_cancelling_a_scheduled_change_clears_the_quantity(self):
        self._change(plan_id=str(self.plan.pk), minutes=200)

        response = self.client.delete(f"{BILLING}/subscription/scheduled-change/")

        self.assertEqual(response.status_code, 200)
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.scheduled_minutes)
        self.assertIsNone(self.subscription.scheduled_plan_id)

    # -- moving between plans ---------------------------------------------- #

    def test_moving_plan_without_a_quantity_keeps_the_current_one(self):
        target = make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            minute_prices={HIGH: Decimal("3.60")},
            discount_percentage=Decimal("0.00"), sort_order=2,
        )

        response = self._preview(plan_id=str(target.pk))

        self.assertEqual(response.data["data"]["money"]["selected_minutes"], "1000")

    def test_moving_to_a_pack_takes_the_packs_quantity(self):
        pack = make_minutes_plan(
            min_minutes=5000, max_minutes=5000, credit_basis=HIGH,
            discount_percentage=Decimal("0.00"), sort_order=2,
        )

        response = self._preview(plan_id=str(pack.pk))

        self.assertEqual(response.data["data"]["money"]["selected_minutes"], "5000")

    # -- reading the subscription back -------------------------------------- #

    def test_current_subscription_reports_the_quantity(self):
        response = self.client.get(f"{BILLING}/subscription/")

        self.assertEqual(response.data["data"]["selected_minutes"], 1000)
        self.assertEqual(
            response.data["data"]["allocated_credits"], 1000 * DEFAULT_RATES[HIGH]
        )

    def test_current_subscription_reports_a_scheduled_quantity(self):
        self._change(plan_id=str(self.plan.pk), minutes=200)

        scheduled = self.client.get(f"{BILLING}/subscription/").data["data"][
            "scheduled_change"
        ]

        self.assertEqual(scheduled["selected_minutes"], 200)

    def test_renewal_is_priced_for_the_stored_quantity(self):
        # Renewal only opens inside the last 14 days of a cycle.
        age_subscription(self.subscription, days_in=25)

        response = self.client.post(f"{BILLING}/subscription/renew/", format="json")

        # 1,000 minutes at Rs 1.80 = Rs 1,800 net, +18% = Rs 2,124.00
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["amount"], 212400)

    def test_eligible_plans_can_be_priced_at_a_hypothetical_quantity(self):
        make_minutes_plan(
            min_minutes=None, max_minutes=None, credit_basis=HIGH,
            minute_prices={HIGH: Decimal("1.20")},
            discount_percentage=Decimal("0.00"), sort_order=2,
        )

        response = self.client.get(
            f"{BILLING}/subscription/eligible-plans/", {"minutes": 3000}
        )

        others = [p for p in response.data["data"]["plans"] if not p["is_current"]]
        self.assertTrue(others)
        self.assertEqual(others[0]["selected_minutes"], 3000)

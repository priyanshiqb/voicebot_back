"""Section 12 of the billing test plan — BP-01 to BP-30.

Buying credits and buying capacity. The single most important property across both is
that **an order grants nothing**: the credits arrive and the capacity is granted when
the payment clears, so an abandoned checkout costs the platform exactly nothing. BP-11
and BP-23 are the two cases that say so.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.models import Subscription, TenantEntitlement, Transaction
from onboard.services import payments
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    BILLING,
    make_free_plan,
    make_plan,
    make_tenant,
)
from wallet.models import Wallet

Resource = EntitlementResource
TxnType = Transaction.TransactionTypeChoices
TxnStatus = Transaction.TransactionStatusChoices

TOP_UP_QUOTE_URL = f"{BILLING}/top-up/quote/"
TOP_UP_URL = f"{BILLING}/top-up/"
ADDON_QUOTE_URL = f"{BILLING}/add-ons/quote/"
ADDON_URL = f"{BILLING}/add-ons/"

DAYS_ELAPSED = 20
DAYS_TOTAL = 30


class PurchaseAPITestCase(TestCase):
    """A tenant twenty days into a ₹999 cycle that sells credits at ₹1.20."""

    def setUp(self):
        self.ctx = make_tenant("purchases")
        self.tenant = self.ctx.tenant
        self.client = self.ctx.client
        self.plan = make_plan(
            "purchases-plan",
            base_price=Decimal("999.00"),
            tax_percentage=Decimal("18.00"),
            allocated_credits=1000,
            cost_per_additional_credit=Decimal("1.20"),
        )
        self.subscription = subscription_service.activate(self.tenant, self.plan)
        self.set_cycle(DAYS_ELAPSED, DAYS_TOTAL)

    def set_cycle(self, days_elapsed, days_total, subscription=None):
        subscription = subscription or self.subscription
        now = timezone.now()
        Subscription.objects.filter(pk=subscription.pk).update(
            start_date=now - timedelta(days=days_elapsed),
            end_date=now + timedelta(days=days_total - days_elapsed),
        )
        subscription.refresh_from_db()
        return subscription

    @property
    def transactions(self):
        return Transaction.objects.filter(tenant=self.tenant)

    @property
    def wallet(self):
        return Wallet.objects.get(tenant=self.tenant)


class TopUpEndpointTests(PurchaseAPITestCase):
    """BP-01 to BP-15."""

    def test_bp01_a_quote_prices_the_credits(self):
        """BP-01: 500 × ₹1.20 + 18%."""
        response = self.client.post(
            TOP_UP_QUOTE_URL, {"credits": 500}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertEqual(data["net"], "600.00")
        self.assertEqual(data["tax"], "108.00")
        self.assertEqual(data["total"], "708.00")

    def test_bp02_quoting_creates_nothing(self):
        """BP-02: a price check is not a purchase, and a pricing widget that
        created orders would block the tenant from actually buying."""
        self.client.post(TOP_UP_QUOTE_URL, {"credits": 500}, format="json")

        self.assertEqual(self.transactions.count(), 0)

    def test_bp03_a_missing_or_zero_quantity_is_rejected(self):
        """BP-03."""
        self.assertEqual(
            self.client.post(TOP_UP_QUOTE_URL, {}, format="json").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.post(
                TOP_UP_QUOTE_URL, {"credits": 0}, format="json"
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_bp04_a_plan_that_does_not_sell_credits_says_so(self):
        """BP-04: 409 with a code the UI can turn into "upgrade to get more",
        rather than a 400 that reads as the tenant's mistake."""
        other = make_tenant("purchases-no-top-ups")
        subscription_service.activate(
            other, make_plan(
                "purchases-no-top-ups-plan",
                cost_per_additional_credit=Decimal("0.00"),
            )
        )

        response = other.client.post(
            TOP_UP_QUOTE_URL, {"credits": 500}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("TOP_UPS_UNAVAILABLE", response.json()["errors"])

    def test_bp05_buying_credits_needs_a_running_plan(self):
        """BP-05: **credits belong to a cycle.** Selling them to a lapsed tenant
        would take money for capacity they cannot use until they renew anyway."""
        stranger = make_tenant("purchases-no-plan")

        response = stranger.client.post(
            TOP_UP_URL, {"credits": 500}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("NO_ACTIVE_SUBSCRIPTION", response.json()["errors"])

    def test_bp06_anonymous_callers_are_refused(self):
        """BP-06."""
        response = APIClient().post(TOP_UP_URL, {"credits": 500}, format="json")

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_bp07_an_order_records_what_was_bought(self):
        """BP-07: the webhook reads ``credits_purchased`` off this row — without
        it, a cleared payment has nothing to grant."""
        response = self.client.post(TOP_UP_URL, {"credits": 500}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        transaction = self.transactions.get()
        self.assertEqual(transaction.status, TxnStatus.PENDING)
        self.assertEqual(transaction.transaction_type, TxnType.TOP_UP)
        self.assertEqual(transaction.credits_purchased, 500)
        self.assertEqual(transaction.amount, Decimal("708.00"))

    def test_bp08_the_gateway_amount_is_in_paise(self):
        """BP-08: **₹708.00 is 70800 paise.** Sending 708 would charge the tenant
        seven rupees and eight paise."""
        data = self.client.post(
            TOP_UP_URL, {"credits": 500}, format="json"
        ).json()["data"]

        self.assertEqual(data["amount"], 70800)

    def test_bp09_the_human_readable_amount_is_in_rupees(self):
        """BP-09."""
        data = self.client.post(
            TOP_UP_URL, {"credits": 500}, format="json"
        ).json()["data"]

        self.assertEqual(data["amount_display"], "708.00")
        self.assertEqual(data["currency"], "INR")

    def test_bp10_an_order_expires_in_half_an_hour(self):
        """BP-10: without a TTL a single abandoned checkout blocks the tenant from
        starting another purchase indefinitely."""
        before = timezone.now()

        self.client.post(TOP_UP_URL, {"credits": 500}, format="json")

        transaction = self.transactions.get()
        self.assertIsNotNone(transaction.expires_at)
        self.assertGreater(transaction.expires_at, before + timedelta(minutes=25))
        self.assertLess(transaction.expires_at, before + timedelta(minutes=35))

    def test_bp11_no_credits_arrive_before_the_payment_does(self):
        """BP-11: **an abandoned checkout must cost nothing.**"""
        before = self.wallet.balance

        self.client.post(TOP_UP_URL, {"credits": 500}, format="json")

        self.assertEqual(self.wallet.balance, before)

    def test_bp12_the_breakdown_is_stored_on_the_order(self):
        """BP-12: the plan's price may have moved by the time anyone disputes this
        charge, so recomputing would give a different answer."""
        self.client.post(TOP_UP_URL, {"credits": 500}, format="json")

        breakdown = self.transactions.get().amount_breakdown
        self.assertEqual(breakdown["credits"], "500")
        self.assertEqual(breakdown["unit_price"], "1.20")
        self.assertEqual(breakdown["total"], "708.00")

    def test_bp13_only_one_order_may_be_open_at_a_time(self):
        """BP-13: **the worst outcome available is money taken, effect missing.**

        Without this a tenant can pay two upgrade orders and have only one applied.
        The response carries the open order id so the UI can send them back to it.
        """
        first = self.client.post(TOP_UP_URL, {"credits": 500}, format="json")
        open_order = first.json()["data"]["razorpay_order_id"]

        second = self.client.post(TOP_UP_URL, {"credits": 100}, format="json")

        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("PENDING_ORDER_EXISTS", second.json()["errors"])
        self.assertEqual(second.json()["data"]["razorpay_order_id"], open_order)

    def test_bp14_a_stale_order_is_cleared_rather_than_blocking_forever(self):
        """BP-14."""
        self.client.post(TOP_UP_URL, {"credits": 500}, format="json")
        stale = self.transactions.get()
        Transaction.objects.filter(pk=stale.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        response = self.client.post(TOP_UP_URL, {"credits": 100}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        stale.refresh_from_db()
        self.assertEqual(stale.status, TxnStatus.CANCELED)

    def test_bp15_a_gateway_failure_leaves_nothing_behind(self):
        """BP-15: 502 rather than 500, and **no row claiming the tenant paid.**"""
        with patch.object(
            payments, "create_order", side_effect=RuntimeError("gateway down")
        ):
            response = self.client.post(
                TOP_UP_URL, {"credits": 500}, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertIn("GATEWAY_ERROR", response.json()["errors"])
        self.assertFalse(
            self.transactions.filter(status=TxnStatus.SUCCESSFUL).exists()
        )
        self.assertEqual(self.transactions.count(), 0)


class AddonEndpointTests(PurchaseAPITestCase):
    """BP-16 to BP-30."""

    def setUp(self):
        super().setUp()
        # 10 GB blocks at ₹199, up to 90 GB extra.
        self.storage = self.plan.entitlements.get(resource=Resource.STORAGE_GB)
        self.storage.included_quantity = 10
        self.storage.price_per_additional = Decimal("199.00")
        self.storage.addon_step = 10
        self.storage.max_additional = 90
        self.storage.is_addon_available = True
        self.storage.save()

    def quote(self, resource=Resource.STORAGE_GB, quantity=20):
        return self.client.post(
            ADDON_QUOTE_URL, {"resource": resource, "quantity": quantity},
            format="json",
        )

    def order(self, resource=Resource.STORAGE_GB, quantity=20):
        return self.client.post(
            ADDON_URL, {"resource": resource, "quantity": quantity}, format="json"
        )

    def test_bp16_a_quote_prices_the_remainder_of_the_cycle(self):
        """BP-16."""
        response = self.quote()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertEqual(data["recurring_net"], "398.00")
        self.assertEqual(data["net"], "132.65")

    def test_bp17_the_ongoing_cost_is_shown_beside_the_charge_now(self):
        """BP-17: **showing only the prorated figure understates the commitment.**

        A tenant told ₹156.53 who then sees ₹469.64 on every renewal was not given
        the price of what they bought.
        """
        data = self.quote().json()["data"]

        self.assertLess(Decimal(data["total"]), Decimal(data["added_recurring_net"]))

    def test_bp18_an_invalid_resource_is_rejected(self):
        """BP-18: a ChoiceField, so a typo is the caller's error rather than a
        confusing report that the plan has no such allowance."""
        response = self.client.post(
            ADDON_QUOTE_URL, {"resource": "teleportation", "quantity": 1},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bp19_a_quantity_of_zero_is_rejected(self):
        """BP-19."""
        self.assertEqual(self.quote(quantity=0).status_code,
                         status.HTTP_400_BAD_REQUEST)

    def test_bp20_a_resource_that_cannot_be_extended_says_so(self):
        """BP-20."""
        self.storage.is_addon_available = False
        self.storage.save(update_fields=["is_addon_available"])

        response = self.quote()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("ADDON_UNAVAILABLE", response.json()["errors"])

    def test_bp21_the_ceiling_is_named_in_the_refusal(self):
        """BP-21: an error that does not say where the limit is sends the tenant
        to support to ask."""
        response = self.quote(quantity=100)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("90", response.json()["message"])

    def test_bp22_an_order_records_the_breakdown_the_webhook_will_read(self):
        """BP-22."""
        response = self.order()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        transaction = self.transactions.get()
        self.assertEqual(transaction.transaction_type, TxnType.ADDON)
        self.assertEqual(transaction.status, TxnStatus.PENDING)
        self.assertEqual(transaction.amount, Decimal("156.53"))

    def test_bp23_no_capacity_is_granted_before_the_payment(self):
        """BP-23: **an abandoned checkout must not raise the tenant's limits.**"""
        self.order()

        self.assertFalse(
            TenantEntitlement.objects.filter(tenant=self.tenant).exists()
        )

    def test_bp24_a_sub_rupee_remainder_is_given_away_rather_than_refused(self):
        """BP-24: **late in the cycle the prorated amount falls below what a
        gateway will accept.**

        Refusing would be absurd for a purchase the tenant wants, so the capacity
        is granted outright and the full price starts at the next renewal.
        """
        self.set_cycle(DAYS_TOTAL, DAYS_TOTAL + 0.001)

        response = self.order(quantity=10)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertFalse(body["data"]["charged"])
        self.assertEqual(self.transactions.count(), 0)
        self.assertTrue(
            TenantEntitlement.objects.filter(
                tenant=self.tenant, resource=Resource.STORAGE_GB
            ).exists()
        )

    def test_bp25_only_one_order_may_be_open_at_a_time(self):
        """BP-25: the same guard as BP-13, across products — an open top-up blocks
        an add-on order too, because both settle through the same webhook."""
        self.client.post(TOP_UP_URL, {"credits": 500}, format="json")

        response = self.order()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("PENDING_ORDER_EXISTS", response.json()["errors"])

    def test_bp26_a_free_plan_cannot_rent_capacity(self):
        """BP-26: upgrading is the way to get more, which is the point of a free
        tier having hard edges."""
        other = make_tenant("purchases-free")
        subscription_service.activate(other, make_free_plan("purchases-free-plan"))

        response = other.client.post(
            ADDON_QUOTE_URL, {"resource": Resource.AGENT, "quantity": 1},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("ADDON_UNAVAILABLE", response.json()["errors"])

    def test_bp27_each_tenant_buys_against_its_own_subscription(self):
        """BP-27: a leak here charges one tenant at another's prices."""
        other = make_tenant("purchases-isolation")
        cheap_plan = make_plan(
            "purchases-isolation-plan",
            base_price=Decimal("100.00"),
            tax_percentage=Decimal("18.00"),
            cost_per_additional_credit=Decimal("0.10"),
        )
        subscription_service.activate(other, cheap_plan)

        mine = self.client.post(
            TOP_UP_QUOTE_URL, {"credits": 100}, format="json"
        ).json()["data"]
        theirs = other.client.post(
            TOP_UP_QUOTE_URL, {"credits": 100}, format="json"
        ).json()["data"]

        self.assertEqual(mine["net"], "120.00")
        self.assertEqual(theirs["net"], "10.00")

    def test_bp28_the_order_charges_exactly_what_the_quote_said(self):
        """BP-28: **a quote a tenant is then billed differently for is worse than
        no quote at all.**"""
        quoted = self.quote().json()["data"]["total"]

        ordered = self.order().json()["data"]["amount_display"]

        self.assertEqual(quoted, ordered)

    def test_bp29_the_breakdown_carries_what_the_webhook_must_grant(self):
        """BP-29: the webhook has only this row to work from when the payment
        clears — a missing resource or quantity means a paid order it cannot apply."""
        self.order(quantity=20)

        breakdown = self.transactions.get().amount_breakdown
        self.assertEqual(breakdown["resource"], Resource.STORAGE_GB)
        self.assertEqual(breakdown["additional_units"], "20")
        self.assertEqual(breakdown["quantity_after"], "20")

    def test_bp30_a_long_receipt_is_truncated_for_the_gateway(self):
        """BP-30: Razorpay caps receipts at 40 characters and rejects the order
        outright above that — a refusal that would look like a payment failure."""
        captured = {}

        class _Orders:
            @staticmethod
            def create(payload):
                captured.update(payload)
                return {"id": "order_live_stub"}

        class _Client:
            order = _Orders()

        with patch.object(payments, "gateway_is_live", return_value=True), \
             patch("onboard.views.onboarding.base.razorpay_client", _Client()):
            payments.create_order(
                Decimal("100.00"), receipt="addon_" + "x" * 200
            )

        self.assertLessEqual(len(captured["receipt"]), 40)

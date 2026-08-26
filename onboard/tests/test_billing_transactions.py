"""`/billing/transactions/` — payment history, and the two exits from an unpaid order.

The list is the easy half. The half that matters is `cancel` and `retry`: only one
unpaid order may be open at a time, and until these existed a tenant who closed the
Checkout tab was locked out of buying anything for thirty minutes with no way to act.
"""

from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework import status

from onboard.constants.entitlement_resource import EntitlementResource as Resource
from onboard.models import Transaction
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import make_plan, make_tenant

TxnType = Transaction.TransactionTypeChoices
TxnStatus = Transaction.TransactionStatusChoices

LIST = "/api/v1/billing/transactions/"


def detail(order_id):
    return f"{LIST}{order_id}/"


class TransactionHistoryTests(TestCase):
    def setUp(self):
        self.ctx = make_tenant("txn")
        self.tenant = self.ctx.tenant
        self.client = self.ctx.client
        self.plan = make_plan("txn-plan")
        self.subscription = subscription_service.activate(self.tenant, self.plan)
        self._n = 0

    def order(self, *, txn_type=TxnType.TOP_UP, status_=TxnStatus.PENDING,
              tenant=None, expires_in_minutes=30, **extra):
        self._n += 1
        expires = (
            timezone.now() + timezone.timedelta(minutes=expires_in_minutes)
            if expires_in_minutes is not None
            else None
        )
        return Transaction.objects.create(
            razorpay_order_id=f"order_{self._n}_{id(self)}",
            amount=Decimal("590.00"),
            status=status_,
            transaction_type=txn_type,
            tenant=tenant or self.tenant,
            subscription=self.subscription,
            plan=self.plan,
            credits_purchased=extra.pop("credits", 500),
            expires_at=expires,
            **extra,
        )

    def rows(self, response):
        data = response.data["data"]
        return data["results"] if isinstance(data, dict) and "results" in data else data

    # -- the list ---------------------------------------------------------- #

    def test_it_lists_this_tenants_payments_newest_first(self):
        old = self.order(status_=TxnStatus.SUCCESSFUL)
        Transaction.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=5)
        )
        recent = self.order(status_=TxnStatus.SUCCESSFUL)

        rows = self.rows(self.client.get(LIST))

        self.assertEqual(
            [r["razorpay_order_id"] for r in rows],
            [recent.razorpay_order_id, old.razorpay_order_id],
        )

    def test_another_tenants_payments_are_invisible(self):
        other = make_tenant("txn-other")
        theirs = self.order(tenant=other.tenant, status_=TxnStatus.SUCCESSFUL)
        mine = self.order(status_=TxnStatus.SUCCESSFUL)

        ids = {r["razorpay_order_id"] for r in self.rows(self.client.get(LIST))}

        self.assertIn(mine.razorpay_order_id, ids)
        self.assertNotIn(theirs.razorpay_order_id, ids)

    def test_it_can_be_filtered_to_what_is_still_unpaid(self):
        pending = self.order()
        self.order(status_=TxnStatus.SUCCESSFUL)

        rows = self.rows(self.client.get(LIST, {"status": TxnStatus.PENDING}))

        self.assertEqual([r["razorpay_order_id"] for r in rows],
                         [pending.razorpay_order_id])

    def test_each_row_says_what_the_payment_was_for(self):
        top_up = self.order(status_=TxnStatus.SUCCESSFUL, credits=1500)
        addon = self.order(
            txn_type=TxnType.ADDON, status_=TxnStatus.SUCCESSFUL, credits=0,
            amount_breakdown={"resource": Resource.AGENT, "additional_units": 3},
        )

        by_id = {r["razorpay_order_id"]: r for r in self.rows(self.client.get(LIST))}

        self.assertEqual(by_id[top_up.razorpay_order_id]["summary"], "1,500 credits")
        self.assertEqual(by_id[addon.razorpay_order_id]["summary"], "3 × agent")

    def test_signup_payments_appear_in_the_history(self):
        """Hiding the first charge would make the list look like it starts
        mid-story."""
        signup = self.order(txn_type=TxnType.ONBOARDING,
                            status_=TxnStatus.SUCCESSFUL)

        ids = {r["razorpay_order_id"] for r in self.rows(self.client.get(LIST))}

        self.assertIn(signup.razorpay_order_id, ids)

    def test_it_is_looked_up_by_order_id_not_database_id(self):
        """That is the identifier the 409 hands back, and the one Razorpay speaks."""
        txn = self.order(status_=TxnStatus.SUCCESSFUL)

        response = self.client.get(detail(txn.razorpay_order_id))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["razorpay_order_id"],
                         txn.razorpay_order_id)

    def test_gateway_diagnostics_are_not_exposed(self):
        """`raw_webhook_payload` can carry contact details from the payment method."""
        txn = self.order(status_=TxnStatus.SUCCESSFUL,
                         raw_webhook_payload={"secret": "leaked"})

        body = self.client.get(detail(txn.razorpay_order_id)).data["data"]

        self.assertNotIn("raw_webhook_payload", body)

    # -- can_cancel / can_retry -------------------------------------------- #

    def test_an_open_order_advertises_both_actions(self):
        txn = self.order()
        row = self.client.get(detail(txn.razorpay_order_id)).data["data"]
        self.assertTrue(row["can_cancel"])
        self.assertTrue(row["can_retry"])

    def test_a_settled_order_advertises_neither(self):
        txn = self.order(status_=TxnStatus.SUCCESSFUL)
        row = self.client.get(detail(txn.razorpay_order_id)).data["data"]
        self.assertFalse(row["can_cancel"])
        self.assertFalse(row["can_retry"])

    def test_an_expired_but_unswept_order_advertises_neither(self):
        """It is still PENDING in the database until something sweeps it. Offering
        "Pay now" on it would send the tenant to a dead order."""
        txn = self.order(expires_in_minutes=-5)

        row = self.client.get(detail(txn.razorpay_order_id)).data["data"]

        self.assertEqual(row["status"], TxnStatus.PENDING)
        self.assertFalse(row["can_retry"])

    # -- cancel ------------------------------------------------------------ #

    def test_cancelling_releases_the_one_open_order_lock(self):
        """The whole reason this endpoint exists. Before it, the 409 told tenants to
        cancel an order they had no way to cancel."""
        txn = self.order()
        blocked = self.client.post(
            "/api/v1/billing/top-up/", {"credits": 100}, format="json"
        )
        self.assertEqual(blocked.status_code, status.HTTP_409_CONFLICT)

        cancelled = self.client.post(f"{detail(txn.razorpay_order_id)}cancel/")

        self.assertEqual(cancelled.status_code, status.HTTP_200_OK)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TxnStatus.CANCELED)
        again = self.client.post(
            "/api/v1/billing/top-up/", {"credits": 100}, format="json"
        )
        self.assertNotEqual(again.status_code, status.HTTP_409_CONFLICT)

    def test_a_cancelled_order_stays_in_the_history(self):
        """A payment record that disappears is worse than one saying nothing
        happened."""
        txn = self.order()

        self.client.post(f"{detail(txn.razorpay_order_id)}cancel/")

        ids = {r["razorpay_order_id"] for r in self.rows(self.client.get(LIST))}
        self.assertIn(txn.razorpay_order_id, ids)
        txn.refresh_from_db()
        self.assertFalse(txn.is_deleted)

    def test_a_paid_order_cannot_be_cancelled(self):
        txn = self.order(status_=TxnStatus.SUCCESSFUL)

        response = self.client.post(f"{detail(txn.razorpay_order_id)}cancel/")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("ORDER_NOT_PENDING", response.data["errors"])
        txn.refresh_from_db()
        self.assertEqual(txn.status, TxnStatus.SUCCESSFUL)

    def test_a_signup_order_is_sent_to_the_onboarding_flow(self):
        txn = self.order(txn_type=TxnType.ONBOARDING)

        response = self.client.post(f"{detail(txn.razorpay_order_id)}cancel/")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("WRONG_ENDPOINT", response.data["errors"])

    def test_another_tenants_order_cannot_be_cancelled(self):
        other = make_tenant("txn-thief")
        txn = self.order(tenant=other.tenant)

        response = self.client.post(f"{detail(txn.razorpay_order_id)}cancel/")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TxnStatus.PENDING)

    # -- retry ------------------------------------------------------------- #

    def test_retry_returns_a_checkout_payload_for_the_same_order(self):
        """The same Razorpay order, deliberately — every idempotency guard
        downstream is keyed on that id."""
        txn = self.order()

        data = self.client.post(f"{detail(txn.razorpay_order_id)}retry/").data["data"]

        self.assertEqual(data["razorpay_order_id"], txn.razorpay_order_id)
        self.assertEqual(data["amount"], 59000)
        self.assertEqual(data["currency"], "INR")
        self.assertTrue(data["razorpay_key_id"])

    def test_retry_restarts_the_expiry_clock(self):
        """An order the tenant is actively paying should not expire mid-Checkout
        because the original window happened to be nearly up."""
        txn = self.order(expires_in_minutes=1)
        before = txn.expires_at

        self.client.post(f"{detail(txn.razorpay_order_id)}retry/")

        txn.refresh_from_db()
        self.assertGreater(txn.expires_at, before)
        self.assertGreater(
            txn.expires_at, timezone.now() + timezone.timedelta(minutes=25)
        )

    def test_retry_does_not_settle_anything(self):
        txn = self.order()

        self.client.post(f"{detail(txn.razorpay_order_id)}retry/")

        txn.refresh_from_db()
        self.assertEqual(txn.status, TxnStatus.PENDING)
        self.assertIsNone(txn.razorpay_payment_id)

    def test_a_cancelled_order_cannot_be_retried(self):
        txn = self.order(status_=TxnStatus.CANCELED)

        response = self.client.post(f"{detail(txn.razorpay_order_id)}retry/")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("ORDER_NOT_PENDING", response.data["errors"])

    def test_cancel_then_retry_is_refused(self):
        txn = self.order()
        self.client.post(f"{detail(txn.razorpay_order_id)}cancel/")

        response = self.client.post(f"{detail(txn.razorpay_order_id)}retry/")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_the_inherited_bulk_delete_route_does_not_exist(self):
        """The base viewset ships `bulk_delete`, and allowing POST for cancel/retry
        would otherwise re-expose it. A tenant erasing their own payment records is
        not a feature."""
        txn = self.order(status_=TxnStatus.SUCCESSFUL)

        response = self.client.post(
            f"{LIST}bulk_delete/", {"ids": [str(txn.id)]}, format="json"
        )

        # 405, not 404: with the action gone, the path falls through to the detail
        # route as razorpay_order_id="bulk_delete", which does not accept POST.
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        txn.refresh_from_db()
        self.assertFalse(txn.is_deleted)

    def test_the_inherited_dropdown_route_does_not_exist(self):
        self.assertEqual(
            self.client.get(f"{LIST}dropdown/").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_it_requires_authentication(self):
        from rest_framework.test import APIClient

        self.assertIn(
            APIClient().get(LIST).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

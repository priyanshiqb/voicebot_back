"""Section 14 of the billing test plan — BW-01 to BW-28.

The Razorpay webhook, which is the only place money turns into effect.

Two properties dominate everything here. **It is idempotent at two layers** — the
unique ``RazorpayWebhookLog.event_id`` catches a redelivery, and the credit ledger's
``(entry_type, reference_id)`` constraint catches a genuine redelivery carrying a
fresh event id. And **it answers 200 to almost everything**, because Razorpay retries
non-2xx forever and retrying a payload that will fail identically achieves nothing but
another chance to half-apply something.
"""

import json
import sys
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.models import (
    RazorpayWebhookLog,
    Subscription,
    TempUser,
    TenantEntitlement,
    Transaction,
)
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    WEBHOOK_URL,
    make_plan,
    make_tenant,
    make_transaction,
    pay,
)
from wallet.models import CreditLedger, Wallet
from wallet.services import wallet as wallet_service

User = get_user_model()

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Resource = EntitlementResource
Status = Subscription.SubscriptionStatusChoices
TxnStatus = Transaction.TransactionStatusChoices
TxnType = Transaction.TransactionTypeChoices


def captured_body(order_id, event_id="evt_signed", payment_id="pay_signed"):
    """The exact JSON Razorpay would post, as bytes."""
    return json.dumps({
        "id": event_id,
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id}}},
    }).encode()


class _StubUtility:
    """Stands in for ``razorpay_client.utility``, recording what it was handed."""

    def __init__(self, *, valid=True):
        self.valid = valid
        self.calls = []

    def verify_webhook_signature(self, payload, signature, secret):
        self.calls.append((payload, signature, secret))
        if not self.valid:
            raise ValueError("signature mismatch")


class _StubClient:
    def __init__(self, *, valid=True):
        self.utility = _StubUtility(valid=valid)


class WebhookTestCase(TestCase):
    """A tenant on a plan, with helpers for posting each kind of event."""

    def setUp(self):
        self.ctx = make_tenant("webhook")
        self.tenant = self.ctx.tenant
        self.plan = make_plan(
            "webhook-plan",
            base_price=Decimal("999.00"),
            tax_percentage=Decimal("18.00"),
            allocated_credits=1000,
            cost_per_additional_credit=Decimal("1.20"),
        )
        self.bigger = make_plan(
            "webhook-bigger",
            base_price=Decimal("2699.10"),
            tax_percentage=Decimal("18.00"),
            allocated_credits=3500,
        )
        self.subscription = subscription_service.activate(self.tenant, self.plan)

    # --- helpers ------------------------------------------------------------

    def order(self, **overrides):
        overrides.setdefault("tenant", self.tenant)
        overrides.setdefault("plan", self.plan)
        overrides.setdefault("status", TxnStatus.PENDING)
        return make_transaction(**overrides)

    def failed_event(self, order_id, event_id="evt_failed"):
        return APIClient().post(WEBHOOK_URL, {
            "id": event_id,
            "event": "payment.failed",
            "payload": {"payment": {"entity": {
                "id": "pay_failed", "order_id": order_id}}},
        }, format="json")

    @property
    def wallet(self):
        return Wallet.objects.get(tenant=self.tenant)

    def rows(self, **filters):
        return CreditLedger.all_objects.filter(tenant=self.tenant, **filters)


class SignatureAndIdempotencyTests(WebhookTestCase):
    """BW-01 to BW-07."""

    def test_bw01_a_valid_signature_is_accepted(self):
        """BW-01."""
        order = self.order(transaction_type=TxnType.TOP_UP, credits_purchased=100,
                           subscription=self.subscription)

        response = pay(order.razorpay_order_id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_settings(PAYMENTS_ALLOW_UNSIGNED=False, TESTING=False)
    def test_bw02_a_bad_signature_is_the_one_thing_that_gets_a_4xx(self):
        """BW-02: **the only 4xx in this handler.**

        Everything else is answered 200 so Razorpay stops retrying; an unsigned
        request is the one case where retrying is not the problem.
        """
        order = self.order(transaction_type=TxnType.TOP_UP, credits_purchased=100)
        stub = _StubClient(valid=False)

        with patch("onboard.views.onboarding.webhook.razorpay_client", stub), \
             patch.object(sys, "argv", ["manage.py", "runserver"]):
            response = APIClient().post(
                WEBHOOK_URL,
                captured_body(order.razorpay_order_id),
                content_type="application/json",
                HTTP_X_RAZORPAY_SIGNATURE="not-the-right-signature",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("SIGNATURE_CHECK_FAILED", response.json()["errors"])
        order.refresh_from_db()
        self.assertEqual(order.status, TxnStatus.PENDING)

    @override_settings(PAYMENTS_ALLOW_UNSIGNED=False, TESTING=False)
    def test_bw03_the_signature_is_checked_against_the_raw_bytes(self):
        """BW-03: **the ordering bug that would have broken every live webhook.**

        DRF consumes the request stream when it parses ``request.data``, and
        ``request.body`` raises ``RawPostDataException`` afterwards. Reading the body
        first is mandatory, and a re-serialised dict would not hash to what Razorpay
        signed even when it looks identical.
        """
        order = self.order(transaction_type=TxnType.TOP_UP, credits_purchased=100,
                           subscription=self.subscription)
        body = captured_body(order.razorpay_order_id)
        stub = _StubClient(valid=True)

        with patch("onboard.views.onboarding.webhook.razorpay_client", stub), \
             patch.object(sys, "argv", ["manage.py", "runserver"]):
            response = APIClient().post(
                WEBHOOK_URL, body, content_type="application/json",
                HTTP_X_RAZORPAY_SIGNATURE="sig",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(stub.utility.calls), 1)
        verified_payload, signature, _ = stub.utility.calls[0]
        self.assertEqual(verified_payload, body)
        self.assertEqual(signature, "sig")

    def test_bw04_a_redelivered_event_applies_nothing_a_second_time(self):
        """BW-04: **the outer guard.** Razorpay redelivers as a matter of course."""
        order = self.order(transaction_type=TxnType.TOP_UP, credits_purchased=500,
                           subscription=self.subscription)
        pay(order.razorpay_order_id, event_id="evt_dup")
        balance = self.wallet.balance

        response = pay(order.razorpay_order_id, event_id="evt_dup")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("already processed", response.json()["message"])
        self.assertEqual(self.wallet.balance, balance)

    def test_bw05_one_log_row_per_event(self):
        """BW-05."""
        order = self.order(transaction_type=TxnType.TOP_UP, credits_purchased=100,
                           subscription=self.subscription)
        for _ in range(3):
            pay(order.razorpay_order_id, event_id="evt_logged")

        self.assertEqual(
            RazorpayWebhookLog.objects.filter(event_id="evt_logged").count(), 1
        )

    def test_bw06_a_fresh_event_id_on_a_settled_order_grants_nothing(self):
        """BW-06: **the inner guard.**

        A redelivery carrying a new event id slips past the log, so the order's own
        status has to stop it — and behind that, the ledger's unique reference.
        """
        order = self.order(transaction_type=TxnType.TOP_UP, credits_purchased=500,
                           subscription=self.subscription)
        pay(order.razorpay_order_id, event_id="evt_first")
        balance = self.wallet.balance

        response = pay(order.razorpay_order_id, event_id="evt_second")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.wallet.balance, balance)
        self.assertEqual(
            self.rows(entry_type=EntryType.MANUAL_TOP_UP).count(), 1
        )

    def test_bw07_an_event_with_no_id_is_still_processed(self):
        """BW-07: Postgres permits many NULLs in a unique column, so an unidentified
        event must not be mistaken for a redelivery of the last one."""
        order = self.order(transaction_type=TxnType.TOP_UP, credits_purchased=250,
                           subscription=self.subscription)

        response = pay(order.razorpay_order_id, event_id=None)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.wallet.purchased_balance, 250)


class DispatchTests(WebhookTestCase):
    """BW-08 to BW-20 — one handler per transaction type."""

    # --- onboarding ---------------------------------------------------------

    def temp_user(self, email="newbie@example.com"):
        return TempUser.objects.create(
            email=email,
            password_hash="pbkdf2_sha256$fake",
            first_name="New",
            last_name="Bie",
            business_name="Newbie Industries",
            phone_number="+919900001234",
            selected_plan=self.plan,
            is_email_verified=True,
        )

    def test_bw08_an_onboarding_capture_builds_the_whole_account(self):
        """BW-08: user, tenant, subscription and the first cycle's credits."""
        temp = self.temp_user()
        order = self.order(
            tenant=None, temp_user=temp, transaction_type=TxnType.ONBOARDING
        )

        pay(order.razorpay_order_id)

        user = User.objects.get(email="newbie@example.com")
        order.refresh_from_db()
        self.assertIsNotNone(order.tenant_id)
        self.assertEqual(order.user_id, user.pk)

        subscription = subscription_service.current_for(order.tenant_id)
        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.plan_id, self.plan.pk)
        self.assertEqual(
            Wallet.objects.get(tenant_id=order.tenant_id).balance, 1000
        )

    def test_bw09_an_already_consumed_registrant_does_not_build_a_second_account(self):
        """BW-09: **the browser-confirmed path and this one race, and either can
        win.** Whichever arrives second must find nothing left to do."""
        order = self.order(
            tenant=None, temp_user=None, transaction_type=TxnType.ONBOARDING
        )
        before = User.objects.count()

        response = pay(order.razorpay_order_id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(User.objects.count(), before)

    # --- top-up -------------------------------------------------------------

    def test_bw10_a_top_up_lands_in_the_purchased_bucket(self):
        """BW-10: **rollover rules never touch that bucket.** These were bought
        with cash, and expiring them at a cycle boundary would be taking money for
        nothing."""
        order = self.order(transaction_type=TxnType.TOP_UP, credits_purchased=500,
                           subscription=self.subscription)

        pay(order.razorpay_order_id)

        wallet = self.wallet
        self.assertEqual(wallet.purchased_balance, 500)
        self.assertEqual(wallet.allocated_balance, 1000)

    def test_bw11_a_top_up_does_not_run_the_signup_path(self):
        """BW-11: **the bug this dispatch exists to prevent.**

        Before it, every captured payment was assumed to be a signup, so a top-up
        would have tried to promote a TempUser that does not exist.
        """
        before_users = User.objects.count()
        order = self.order(transaction_type=TxnType.TOP_UP, credits_purchased=500,
                           subscription=self.subscription)

        pay(order.razorpay_order_id)

        self.assertEqual(User.objects.count(), before_users)
        self.assertEqual(
            Subscription.objects.filter(tenant=self.tenant).count(), 1
        )

    def test_bw12_a_top_up_with_no_quantity_is_an_error_not_a_guess(self):
        """BW-12: guessing a quantity would credit an amount nobody paid for."""
        order = self.order(transaction_type=TxnType.TOP_UP, credits_purchased=0,
                           subscription=self.subscription)

        with self.assertLogs("onboard.views.onboarding.webhook", level="ERROR"):
            pay(order.razorpay_order_id)

        self.assertEqual(self.wallet.purchased_balance, 0)

    # --- add-ons ------------------------------------------------------------

    def test_bw13_an_addon_capture_grants_the_quantity_that_was_quoted(self):
        """BW-13: **from the frozen breakdown, not a fresh calculation.** The
        tenant paid for what they were quoted, and the catalogue may have moved."""
        order = self.order(
            transaction_type=TxnType.ADDON, subscription=self.subscription,
            amount_breakdown={"resource": Resource.AGENT, "additional_units": "3"},
        )

        pay(order.razorpay_order_id)

        row = TenantEntitlement.objects.get(
            tenant=self.tenant, resource=Resource.AGENT
        )
        self.assertEqual(row.additional_quantity, 3)

    def test_bw14_an_unusable_addon_breakdown_grants_nothing(self):
        """BW-14: loud, and empty-handed. Granting a default quantity would hand
        out capacity nobody was charged for."""
        order = self.order(
            transaction_type=TxnType.ADDON, subscription=self.subscription,
            amount_breakdown={"resource": "", "additional_units": "0"},
        )

        with self.assertLogs("onboard.views.onboarding.webhook", level="ERROR"):
            pay(order.razorpay_order_id)

        self.assertFalse(
            TenantEntitlement.objects.filter(tenant=self.tenant).exists()
        )

    # --- renewal ------------------------------------------------------------

    def test_bw15_a_renewal_capture_extends_funds_and_unfreezes(self):
        """BW-15: all three, or the tenant has paid and still cannot work."""
        Subscription.objects.filter(pk=self.subscription.pk).update(
            end_date=timezone.now() - timedelta(days=1)
        )
        self.subscription.refresh_from_db()
        subscription_service.expire(self.subscription)
        self.subscription.refresh_from_db()
        self.assertTrue(self.wallet.is_frozen)

        order = self.order(
            transaction_type=TxnType.RENEWAL, subscription=self.subscription
        )
        pay(order.razorpay_order_id)

        self.subscription.refresh_from_db()
        wallet = self.wallet
        self.assertEqual(self.subscription.status, Status.ACTIVE)
        self.assertGreater(self.subscription.end_date, timezone.now())
        self.assertFalse(wallet.is_frozen)
        self.assertEqual(wallet.allocated_balance, 1000)

    def test_bw16_a_renewal_with_nothing_attached_changes_nothing(self):
        """BW-16."""
        order = self.order(transaction_type=TxnType.RENEWAL, subscription=None)
        end_before = self.subscription.end_date

        with self.assertLogs("onboard.views.onboarding.webhook", level="ERROR"):
            pay(order.razorpay_order_id)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.end_date, end_before)

    # --- upgrade ------------------------------------------------------------

    def test_bw17_an_upgrade_capture_moves_the_tenant(self):
        """BW-17."""
        order = self.order(
            transaction_type=TxnType.UPGRADE, subscription=self.subscription,
            target_plan=self.bigger,
        )

        pay(order.razorpay_order_id)

        self.subscription.refresh_from_db()
        live = subscription_service.current_for(self.tenant)
        self.assertEqual(live.plan_id, self.bigger.pk)
        self.assertEqual(self.subscription.status, Status.CANCELLED)
        self.assertEqual(self.subscription.superseded_by_id, live.pk)

    def test_bw18_an_upgrade_drops_a_downgrade_scheduled_earlier(self):
        """BW-18: **otherwise a downgrade set weeks ago fires later and silently
        undoes the upgrade they just bought.**"""
        cheaper = make_plan("webhook-cheaper", base_price=Decimal("100.00"))
        subscription_service.schedule_plan_change(self.subscription, cheaper)
        order = self.order(
            transaction_type=TxnType.UPGRADE, subscription=self.subscription,
            target_plan=self.bigger,
        )

        pay(order.razorpay_order_id)

        live = subscription_service.current_for(self.tenant)
        self.assertEqual(live.plan_id, self.bigger.pk)
        self.assertIsNone(live.scheduled_plan_id)
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.scheduled_plan_id)

    def test_bw19_an_upgrade_missing_its_target_changes_nothing(self):
        """BW-19."""
        order = self.order(
            transaction_type=TxnType.UPGRADE, subscription=self.subscription,
            target_plan=None,
        )

        with self.assertLogs("onboard.views.onboarding.webhook", level="ERROR"):
            pay(order.razorpay_order_id)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.plan_id, self.plan.pk)
        self.assertEqual(self.subscription.status, Status.ACTIVE)

    def test_bw20_an_unhandled_payment_type_records_the_money_and_says_so(self):
        """BW-20: **loud rather than silent, and not routed to onboarding.**

        The payment is still marked successful, so no money is lost track of; what
        is missing is the effect it should have produced.
        """
        order = self.order(
            transaction_type="carrier_pigeon", subscription=self.subscription
        )

        with self.assertLogs("onboard.views.onboarding.webhook", level="ERROR") as logs:
            response = pay(order.razorpay_order_id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, TxnStatus.SUCCESSFUL)
        self.assertIn("no handler", "".join(logs.output))


class RobustnessTests(WebhookTestCase):
    """BW-21 to BW-28 — everything that must not become a 500."""

    def test_bw21_an_unknown_order_is_not_an_error(self):
        """BW-21: Razorpay sends events for orders this system never created, and
        test-mode traffic in particular."""
        response = pay("order_this_system_never_made")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_bw22_an_event_with_no_handler_is_accepted_quietly(self):
        """BW-22."""
        response = APIClient().post(WEBHOOK_URL, {
            "id": "evt_subscription_charged",
            "event": "subscription.charged",
            "payload": {},
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_bw23_a_malformed_payload_is_logged_rather_than_crashed_on(self):
        """BW-23: **200, never 500.**

        A 500 makes Razorpay redeliver a payload that will fail in exactly the same
        way, forever — and every redelivery is another chance to half-apply
        something.
        """
        response = APIClient().post(WEBHOOK_URL, {
            "id": "evt_malformed",
            "event": "payment.captured",
            "payload": {"payment": {}},
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        log_entry = RazorpayWebhookLog.objects.get(event_id="evt_malformed")
        self.assertIn("Processing failed", log_entry.error_message)

    def test_bw24_a_failure_mid_way_rolls_back_everything_it_started(self):
        """BW-24: the atomic block is what stops a half-applied payment — the
        order marked successful with the credits never granted."""
        order = self.order(transaction_type=TxnType.TOP_UP, credits_purchased=500,
                           subscription=self.subscription)

        with patch.object(
            wallet_service, "grant", side_effect=RuntimeError("wallet unavailable")
        ):
            response = pay(order.razorpay_order_id, event_id="evt_boom")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(
            order.status, TxnStatus.PENDING,
            "the order must not stay marked successful when its effect rolled back",
        )
        self.assertEqual(self.wallet.purchased_balance, 0)
        self.assertIn(
            "Processing failed",
            RazorpayWebhookLog.objects.get(event_id="evt_boom").error_message,
        )

    def test_bw25_a_failure_event_marks_the_order_failed(self):
        """BW-25: so the tenant can be shown a retry rather than a stuck order."""
        order = self.order(transaction_type=TxnType.TOP_UP, credits_purchased=500)

        self.failed_event(order.razorpay_order_id)

        order.refresh_from_db()
        self.assertEqual(order.status, TxnStatus.FAILED)

    def test_bw26_a_late_failure_cannot_un_succeed_a_paid_order(self):
        """BW-26: **out-of-order delivery is not a reversal.**

        Marking it failed would contradict money that was actually taken.
        """
        order = self.order(transaction_type=TxnType.TOP_UP, credits_purchased=500,
                           subscription=self.subscription)
        pay(order.razorpay_order_id, event_id="evt_capture_first")

        with self.assertLogs("onboard.views.onboarding.webhook", level="WARNING"):
            self.failed_event(order.razorpay_order_id, event_id="evt_late_failure")

        order.refresh_from_db()
        self.assertEqual(order.status, TxnStatus.SUCCESSFUL)
        self.assertEqual(self.wallet.purchased_balance, 500)

    def test_bw27_a_capture_on_a_settled_order_reapplies_nothing(self):
        """BW-27."""
        order = self.order(
            transaction_type=TxnType.UPGRADE, subscription=self.subscription,
            target_plan=self.bigger, status=TxnStatus.SUCCESSFUL,
        )

        pay(order.razorpay_order_id, event_id="evt_late_capture")

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.plan_id, self.plan.pk)
        self.assertEqual(self.subscription.status, Status.ACTIVE)

    def test_bw28_the_order_row_is_locked_while_it_is_applied(self):
        """BW-28: two deliveries of the same capture can arrive at once, and
        without the lock both would read PENDING and both would apply."""
        order = self.order(transaction_type=TxnType.TOP_UP, credits_purchased=100,
                           subscription=self.subscription)

        with CaptureQueriesContext(connection) as queries:
            pay(order.razorpay_order_id, event_id="evt_locked")

        selected_for_update = [
            q["sql"] for q in queries
            if "onboard_transaction" in q["sql"] and "FOR UPDATE" in q["sql"]
        ]
        self.assertTrue(
            selected_for_update,
            "the transaction row must be selected FOR UPDATE before it is applied",
        )

"""`GET /api/v1/ops/tenants/{id}/` — the 360.

Two halves. The blocks against a fully configured tenant, and — the half that will
actually break — the **degraded cases**: a tenant with no subscription, no wallet, no
settings row and no calls is not an edge case here, it is a signup whose payment never
cleared, which is exactly the tenant support is looking at.
"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from onboard.models import Transaction
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    expire_subscription,
    make_call_log,
    make_plan,
    make_tenant,
    make_transaction,
    subscribe,
)
from ops.tests.tenant_console_factories import console, detail_url
from tenant.models import TenantSettings, TenantUserMapping
from user.models import CustomUser
from wallet.models import Wallet


def body(response):
    return response.json()["data"]


class FullyConfiguredTests(TestCase):
    def setUp(self):
        self.client_ops = console()
        self.plan = make_plan("detail", name="Growth")
        self.ctx = make_tenant("acme")
        self.subscription = subscribe(self.ctx, self.plan)
        self.data = body(self.client_ops.get(detail_url(self.ctx.tenant)))

    def test_every_block_is_present(self):
        for block in (
            "tenant", "enforcement", "owner", "user_count", "subscription", "wallet",
            "entitlements", "payments", "ledger", "usage", "health",
        ):
            self.assertIn(block, self.data, f"missing block {block}")

    def test_tenant_block(self):
        self.assertEqual(self.data["tenant"]["name"], "acme")
        self.assertEqual(self.data["tenant"]["subdomain"], self.ctx.tenant.subdomain)
        self.assertTrue(self.data["tenant"]["is_active"])

    def test_owner_is_the_first_user_and_says_so(self):
        """There is no owner field. The claim is labelled as derived, not asserted."""
        self.assertEqual(self.data["owner"]["email"], self.ctx.user.email)
        self.assertEqual(self.data["owner"]["derived"], "first_user")
        self.assertEqual(self.data["user_count"], 1)

    def test_owner_stays_the_first_user_when_more_are_added(self):
        later = CustomUser.objects.create(email="second@acme.com", username="second")
        TenantUserMapping.objects.create(user=later, tenant=self.ctx.tenant)

        data = body(self.client_ops.get(detail_url(self.ctx.tenant)))
        self.assertEqual(data["owner"]["email"], self.ctx.user.email)
        self.assertEqual(data["user_count"], 2)

    def test_subscription_plan_is_the_frozen_snapshot(self):
        block = self.data["subscription"]
        self.assertEqual(block["status"], "active")
        self.assertEqual(block["plan"]["name"], self.subscription.plan_snapshot["name"])
        self.assertIn("allocated_credits", block["plan"])
        self.assertGreater(block["days_remaining"], 0)

    def test_subscription_carries_the_frozen_rules(self):
        """Read through `rules`, never off the live plan."""
        self.assertEqual(
            self.data["subscription"]["rules"]["allocated_credits"],
            self.plan.allocated_credits,
        )

    def test_wallet_block_carries_the_stamped_rate_card(self):
        """Not derivable from anything else on the screen."""
        block = self.data["wallet"]
        self.assertTrue(block["exists"])
        self.assertEqual(block["balance"], self.plan.allocated_credits)
        self.assertEqual(block["allocated_balance"], self.plan.allocated_credits)
        self.assertIsNotNone(block["rate_snapshot"])
        self.assertIn("operations", block["rate_snapshot"])

    def test_entitlements_carry_all_seven_resources_with_usage(self):
        rows = self.data["entitlements"]["rows"]
        self.assertEqual(len(rows), 7)
        self.assertIsNone(self.data["entitlements"]["reason"])
        for row in rows:
            self.assertIn("used", row)
            self.assertIn("remaining", row)
            self.assertIn("limit", row)

    def test_ledger_shows_the_cycle_allocation(self):
        rows = self.data["ledger"]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["entry_type"], "cycle_allocation")
        self.assertEqual(rows[0]["amount"], self.plan.allocated_credits)
        self.assertEqual(rows[0]["balance_after"], self.plan.allocated_credits)

    def test_usage_counts_calls_and_minutes(self):
        make_call_log(self.ctx, duration_seconds=90)
        make_call_log(self.ctx, duration_seconds=30)

        usage = body(self.client_ops.get(detail_url(self.ctx.tenant)))["usage"]
        self.assertEqual(usage["calls"], 2)
        self.assertEqual(usage["minutes"], 2.0)
        self.assertEqual(usage["window_days"], 30)

    def test_health_is_green_on_a_healthy_tenant(self):
        self.assertTrue(self.data["health"]["ok"], self.data["health"]["checks"])


class DegradedTests(TestCase):
    """The four "nothing configured" cases, each degrading on its own."""

    def setUp(self):
        self.client_ops = console()
        self.ctx = make_tenant("bare")
        self.data = body(self.client_ops.get(detail_url(self.ctx.tenant)))

    def test_request_succeeds_at_all(self):
        """A 360 that 500s here fails at the moment it is needed most."""
        self.assertEqual(
            self.client_ops.get(detail_url(self.ctx.tenant)).status_code, 200
        )

    def test_no_subscription_reports_why_rather_than_raising(self):
        self.assertIsNone(self.data["subscription"])
        self.assertEqual(self.data["entitlements"]["rows"], [])
        self.assertEqual(self.data["entitlements"]["reason"], "no_subscription")

    def test_no_wallet_reads_as_an_empty_wallet(self):
        self.assertFalse(self.data["wallet"]["exists"])
        self.assertEqual(self.data["wallet"]["balance"], 0)
        self.assertFalse(self.data["wallet"]["is_frozen"])
        self.assertIsNone(self.data["wallet"]["rate_snapshot"])

    def test_no_settings_row_reads_as_the_model_defaults(self):
        self.assertFalse(self.data["enforcement"]["credits_enforced"])
        self.assertFalse(self.data["enforcement"]["entitlements_enforced"])
        self.assertFalse(self.data["enforcement"]["configured"])

    def test_reading_a_360_creates_no_rows(self):
        """`for_tenant()` is a `get_or_create`. A GET must not write."""
        self.client_ops.get(detail_url(self.ctx.tenant))

        self.assertFalse(TenantSettings.objects.filter(tenant=self.ctx.tenant).exists())
        self.assertFalse(Wallet.objects.filter(tenant=self.ctx.tenant).exists())

    def test_no_calls_reads_as_zero_not_null(self):
        self.assertEqual(self.data["usage"]["calls"], 0)
        self.assertEqual(self.data["usage"]["minutes"], 0)
        self.assertEqual(self.data["usage"]["credits_burned"], 0)

    def test_health_is_green_on_an_empty_tenant(self):
        """Nothing configured is not the same as something broken."""
        self.assertTrue(self.data["health"]["ok"], self.data["health"]["checks"])


class LapsedTenantTests(TestCase):
    def setUp(self):
        self.client_ops = console()
        self.plan = make_plan("lapsed")
        self.ctx = make_tenant("lapsed")
        self.subscription = subscribe(self.ctx, self.plan)

    def test_expired_subscription_is_still_shown(self):
        """`current_or_expired`: the lapsed row is what support needs to see."""
        expire_subscription(self.subscription)

        data = body(self.client_ops.get(detail_url(self.ctx.tenant)))
        self.assertIsNotNone(data["subscription"])
        self.assertEqual(data["subscription"]["status"], "expired")
        self.assertLess(data["subscription"]["days_remaining"], 0)

    def test_days_remaining_is_signed_not_clamped(self):
        """"-3" says the cycle ended three days ago, which is the number support wants."""
        type(self.subscription).objects.filter(pk=self.subscription.pk).update(
            end_date=timezone.now() - timedelta(days=3)
        )
        data = body(self.client_ops.get(detail_url(self.ctx.tenant)))
        self.assertEqual(data["subscription"]["days_remaining"], -3)


class SupersededSubscriptionTests(TestCase):
    """A tenant with an active row and a cancelled one behind it."""

    def setUp(self):
        self.client_ops = console()
        self.old_plan = make_plan("old")
        self.new_plan = make_plan("new")
        self.ctx = make_tenant("upgraded")
        self.first = subscribe(self.ctx, self.old_plan)
        self.second = subscription_service.apply_plan_change(self.first, self.new_plan)

    def test_the_active_subscription_is_shown_not_the_cancelled_one(self):
        data = body(self.client_ops.get(detail_url(self.ctx.tenant)))
        self.assertEqual(data["subscription"]["id"], str(self.second.pk))
        self.assertEqual(data["subscription"]["status"], "active")

    def test_the_cancelled_row_points_forward(self):
        """The first thing support asks for in a rollover dispute."""
        self.first.refresh_from_db()
        self.assertEqual(self.first.superseded_by_id, self.second.pk)


class PaymentsBlockTests(TestCase):
    def setUp(self):
        self.client_ops = console()
        self.plan = make_plan("payments")
        self.ctx = make_tenant("payer")
        subscribe(self.ctx, self.plan)

    def test_orders_are_listed_newest_first(self):
        make_transaction(
            self.ctx, self.plan,
            transaction_type=Transaction.TransactionTypeChoices.RENEWAL,
        )
        make_transaction(
            self.ctx, self.plan,
            transaction_type=Transaction.TransactionTypeChoices.TOP_UP,
            credits_purchased=500,
        )
        rows = body(self.client_ops.get(detail_url(self.ctx.tenant)))["payments"]["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["transaction_type"], "top_up")
        self.assertEqual(rows[0]["credits_purchased"], 500)

    def test_bonus_days_come_from_the_breakdown_not_a_subscription_field(self):
        make_transaction(
            self.ctx, self.plan,
            transaction_type=Transaction.TransactionTypeChoices.UPGRADE,
            amount_breakdown={"bonus_days": "4.00", "unused_ratio": "0.5"},
        )
        rows = body(self.client_ops.get(detail_url(self.ctx.tenant)))["payments"]["rows"]
        self.assertEqual(rows[0]["bonus_days"], "4.00")

    def test_the_onboarding_payment_is_absent_and_the_payload_says_so(self):
        """`tenant` is NULL on an onboarding order, by check constraint."""
        make_transaction(
            None, self.plan,
            transaction_type=Transaction.TransactionTypeChoices.ONBOARDING,
        )
        payments = body(self.client_ops.get(detail_url(self.ctx.tenant)))["payments"]
        self.assertEqual(payments["rows"], [])
        self.assertIn("onboarding payment is not listed", payments["note"])


class DeletedTenantTests(TestCase):
    def setUp(self):
        self.client_ops = console()
        self.ctx = make_tenant("gone")
        self.ctx.tenant.soft_delete()

    def test_a_deleted_tenant_is_still_readable(self):
        """"My account vanished" is a question the console exists to answer."""
        response = self.client_ops.get(detail_url(self.ctx.tenant))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body(response)["tenant"]["is_deleted"])


class UnknownTenantTests(TestCase):
    def test_unknown_id_is_404(self):
        client = console()
        response = client.get("/api/v1/ops/tenants/00000000-0000-0000-0000-000000000000/")
        self.assertEqual(response.status_code, 404)

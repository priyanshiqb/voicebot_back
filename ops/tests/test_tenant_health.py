"""The health block — each check, green and red, independently.

The block that makes the 360 worth opening. Three of the four checks catch failures
that are silent by construction: nothing raises, nothing logs to a screen anybody
reads, and the tenant finds out before we do.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from onboard.models import Transaction
from onboard.tests.billing_factories import (
    expire_subscription,
    make_call_log,
    make_plan,
    make_tenant,
    make_transaction,
    subscribe,
)
from ops.services.tenant import health as health_service
from ops.tests.tenant_console_factories import console, detail_url
from wallet.models import Wallet


def check(report, code):
    return next(c for c in report["checks"] if c["code"] == code)


def age(model_or_qs, **kwargs):
    """Backdate `created_at`, which is `auto_now_add` and cannot be set on create."""
    model_or_qs.update(**kwargs)


class WalletReconcilesTests(TestCase):
    def setUp(self):
        self.plan = make_plan("reconcile")
        self.ctx = make_tenant("recon")
        subscribe(self.ctx, self.plan)
        self.wallet = Wallet.objects.get(tenant=self.ctx.tenant)

    def test_green_when_the_balance_matches_the_ledger(self):
        report = health_service.report(self.ctx.tenant.id)
        self.assertTrue(check(report, "WALLET_RECONCILES")["ok"])

    def test_red_when_the_balance_drifts_from_the_ledger(self):
        """The balance is a cache; the ledger is the truth.

        Both bucket columns move together so the model's own CheckConstraint still
        holds — this is drift against the *ledger*, which is the only thing that
        would ever be seen in production.
        """
        Wallet.objects.filter(pk=self.wallet.pk).update(
            balance=self.wallet.balance + 50,
            allocated_balance=self.wallet.allocated_balance + 50,
        )

        result = check(health_service.report(self.ctx.tenant.id), "WALLET_RECONCILES")
        self.assertFalse(result["ok"])
        self.assertIn("does not match its ledger", result["detail"])
        fields = {p["field"] for p in result["problems"]}
        self.assertEqual(fields, {"balance", "allocated_balance"})
        self.assertEqual(result["problems"][0]["drift"], -50)

    def test_the_console_and_the_sweep_use_the_same_rule(self):
        """One rule, two callers — the point of the extraction."""
        from wallet.services import reconciliation

        Wallet.objects.filter(pk=self.wallet.pk).update(
            balance=self.wallet.balance + 7,
            allocated_balance=self.wallet.allocated_balance + 7,
        )
        self.wallet.refresh_from_db()

        direct = reconciliation.check(self.wallet)
        via_console = check(
            health_service.report(self.ctx.tenant.id), "WALLET_RECONCILES"
        )["problems"]
        self.assertEqual(direct, via_console)

    def test_green_when_there_is_no_wallet_at_all(self):
        bare = make_tenant("nowallet")
        report = health_service.report(bare.tenant.id)
        self.assertTrue(check(report, "WALLET_RECONCILES")["ok"])


class UnratedCallsTests(TestCase):
    def setUp(self):
        self.plan = make_plan("unrated")
        self.ctx = make_tenant("unrated")
        subscribe(self.ctx, self.plan)

    def test_green_with_no_calls(self):
        report = health_service.report(self.ctx.tenant.id)
        self.assertTrue(check(report, "NO_UNRATED_CALLS")["ok"])

    def test_a_call_inside_the_grace_window_is_not_reported(self):
        """A log posted seconds ago may still be mid-charge."""
        make_call_log(self.ctx, duration_seconds=60)

        report = health_service.report(self.ctx.tenant.id)
        self.assertTrue(check(report, "NO_UNRATED_CALLS")["ok"])

    def test_a_call_past_the_grace_window_with_no_charge_is_reported(self):
        """Revenue quietly not collected."""
        from core.models.agent_call_log import AgentCallLog

        call = make_call_log(self.ctx, duration_seconds=60)
        age(
            AgentCallLog.objects.filter(pk=call.pk),
            created_at=timezone.now() - timedelta(hours=2),
        )

        result = check(health_service.report(self.ctx.tenant.id), "NO_UNRATED_CALLS")
        self.assertFalse(result["ok"])
        self.assertEqual(result["calls"][0]["provider_call_id"], call.provider_call_id)

    def test_a_zero_duration_call_is_not_unrated(self):
        """Nothing happened, so nothing was owed."""
        from core.models.agent_call_log import AgentCallLog

        call = make_call_log(self.ctx, duration_seconds=0)
        age(
            AgentCallLog.objects.filter(pk=call.pk),
            created_at=timezone.now() - timedelta(hours=2),
        )

        report = health_service.report(self.ctx.tenant.id)
        self.assertTrue(check(report, "NO_UNRATED_CALLS")["ok"])

    def test_a_charged_call_is_not_reported(self):
        from core.models.agent_call_log import AgentCallLog
        from wallet.models import CreditRate
        from wallet.services import wallet as wallet_service

        call = make_call_log(self.ctx, duration_seconds=60)
        wallet_service.consume(
            self.ctx.tenant,
            operation=CreditRate.Operation.CONVERSATION_HIGH_LATENCY,
            quantity=60,
            reference_id=call.provider_call_id,
        )
        age(
            AgentCallLog.objects.filter(pk=call.pk),
            created_at=timezone.now() - timedelta(hours=2),
        )

        report = health_service.report(self.ctx.tenant.id)
        self.assertTrue(check(report, "NO_UNRATED_CALLS")["ok"])

    def test_another_tenants_unrated_call_does_not_leak_in(self):
        from core.models.agent_call_log import AgentCallLog

        other = make_tenant("other")
        subscribe(other, self.plan)
        call = make_call_log(other, duration_seconds=60)
        age(
            AgentCallLog.objects.filter(pk=call.pk),
            created_at=timezone.now() - timedelta(hours=2),
        )

        report = health_service.report(self.ctx.tenant.id)
        self.assertTrue(check(report, "NO_UNRATED_CALLS")["ok"])
        self.assertFalse(
            check(health_service.report(other.tenant.id), "NO_UNRATED_CALLS")["ok"]
        )


class FailedPaymentsTests(TestCase):
    def setUp(self):
        self.plan = make_plan("payments-health")
        self.ctx = make_tenant("payer")
        subscribe(self.ctx, self.plan)

    def test_green_with_only_successful_orders(self):
        make_transaction(self.ctx, self.plan)
        report = health_service.report(self.ctx.tenant.id)
        self.assertTrue(check(report, "NO_FAILED_PAYMENTS")["ok"])

    def test_red_on_a_failed_order(self):
        make_transaction(
            self.ctx, self.plan,
            status=Transaction.TransactionStatusChoices.FAILED,
        )
        result = check(health_service.report(self.ctx.tenant.id), "NO_FAILED_PAYMENTS")
        self.assertFalse(result["ok"])
        self.assertEqual(result["payments"][0]["status"], "failed")

    def test_red_on_an_underpaid_order(self):
        """Money was taken but not the amount owed. A human has to decide."""
        make_transaction(
            self.ctx, self.plan,
            status=Transaction.TransactionStatusChoices.UNDERPAID,
        )
        result = check(health_service.report(self.ctx.tenant.id), "NO_FAILED_PAYMENTS")
        self.assertFalse(result["ok"])
        self.assertEqual(result["payments"][0]["status"], "underpaid")

    def test_an_old_failure_is_history_not_a_problem(self):
        failed = make_transaction(
            self.ctx, self.plan,
            status=Transaction.TransactionStatusChoices.FAILED,
        )
        age(
            Transaction.objects.filter(pk=failed.pk),
            created_at=timezone.now() - timedelta(days=200),
        )
        report = health_service.report(self.ctx.tenant.id)
        self.assertTrue(check(report, "NO_FAILED_PAYMENTS")["ok"])


class LapseConsistentTests(TestCase):
    """The check that has never existed anywhere else."""

    def setUp(self):
        self.plan = make_plan("lapse")
        self.ctx = make_tenant("lapse")
        self.subscription = subscribe(self.ctx, self.plan)

    def test_green_while_the_subscription_is_active_and_the_wallet_is_live(self):
        report = health_service.report(self.ctx.tenant.id)
        self.assertTrue(check(report, "LAPSE_CONSISTENT")["ok"])

    def test_green_after_a_normal_expiry(self):
        """`expire()` freezes the wallet, so the two agree."""
        expire_subscription(self.subscription)
        report = health_service.report(self.ctx.tenant.id)
        self.assertTrue(check(report, "LAPSE_CONSISTENT")["ok"])

    def test_red_when_a_cycle_ended_but_the_wallet_was_never_frozen(self):
        """The failure that produces a furious customer."""
        expire_subscription(self.subscription)
        Wallet.objects.filter(tenant=self.ctx.tenant).update(
            is_frozen=False, frozen_reason=None
        )

        result = check(health_service.report(self.ctx.tenant.id), "LAPSE_CONSISTENT")
        self.assertFalse(result["ok"])
        self.assertIn("still spend credits", result["detail"])

    def test_green_when_the_plan_allows_use_after_expiry(self):
        """Pay-as-you-go lapses without freezing, by design. Not a fault."""
        plan = make_plan("payg", allow_use_after_expiry=True)
        ctx = make_tenant("payg")
        subscription = subscribe(ctx, plan)
        expire_subscription(subscription)

        wallet = Wallet.objects.get(tenant=ctx.tenant)
        self.assertFalse(wallet.is_frozen)

        report = health_service.report(ctx.tenant.id)
        self.assertTrue(check(report, "LAPSE_CONSISTENT")["ok"])

    def test_red_when_an_active_subscription_is_frozen_for_a_lapse(self):
        """The same bug from the other side: a paying customer locked out."""
        Wallet.objects.filter(tenant=self.ctx.tenant).update(
            is_frozen=True, frozen_reason="subscription_expired"
        )
        result = check(health_service.report(self.ctx.tenant.id), "LAPSE_CONSISTENT")
        self.assertFalse(result["ok"])
        self.assertIn("locked out", result["detail"])

    def test_an_unrelated_freeze_is_not_a_lapse_inconsistency(self):
        """Support freezing for fraud must not light this check up."""
        Wallet.objects.filter(tenant=self.ctx.tenant).update(
            is_frozen=True, frozen_reason="fraud_hold"
        )
        report = health_service.report(self.ctx.tenant.id)
        self.assertTrue(check(report, "LAPSE_CONSISTENT")["ok"])

    def test_a_cycle_past_its_date_but_not_yet_expired_is_not_a_fault(self):
        """It is waiting for the expiry job, which is not a bug."""
        type(self.subscription).objects.filter(pk=self.subscription.pk).update(
            end_date=timezone.now() - timedelta(hours=2)
        )
        report = health_service.report(self.ctx.tenant.id)
        self.assertTrue(check(report, "LAPSE_CONSISTENT")["ok"])


class HealthEndpointTests(TestCase):
    def setUp(self):
        self.client_ops = console()
        self.plan = make_plan("health-endpoint")
        self.ctx = make_tenant("healthy")
        subscribe(self.ctx, self.plan)

    def test_the_standalone_endpoint_returns_the_same_block(self):
        response = self.client_ops.get(detail_url(self.ctx.tenant, "health/"))
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["ok"])
        self.assertEqual(
            {c["code"] for c in data["checks"]},
            {
                "WALLET_RECONCILES", "NO_UNRATED_CALLS", "NO_FAILED_PAYMENTS",
                "LAPSE_CONSISTENT",
            },
        )

    def test_ok_is_false_when_any_single_check_fails(self):
        make_transaction(
            self.ctx, self.plan,
            status=Transaction.TransactionStatusChoices.FAILED,
        )
        data = self.client_ops.get(detail_url(self.ctx.tenant, "health/")).json()["data"]
        self.assertFalse(data["ok"])

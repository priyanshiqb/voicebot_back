"""Section 17 of the billing test plan — CM-01 to CM-18.

Three operator commands. Two of them found real problems on this platform:
``audit_billing_readiness`` discovered that every tenant was missing a stamped rate
card — so every call would have raised ``RateNotConfigured`` — and
``backfill_wallet_stamps`` is what repaired it.

The recurring principle: **reporting is automatic, repairing is deliberate.**
Reconciliation never fixes drift on its own, and the backfill never grants money
without being asked, because both would hide the bug that made them necessary.
"""

from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection

from onboard.constants.entitlement_resource import EntitlementResource
from onboard.services import subscriptions as subscription_service
from onboard.tests.billing_factories import (
    add_rates,
    make_agent,
    make_plan,
    make_tenant,
)
from tenant.models import Tenant, TenantSettings
from wallet.models import CreditLedger, CreditRate, Wallet
from wallet.services import wallet as wallet_service

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Operation = CreditRate.Operation
Resource = EntitlementResource


def run(command, *args, **options):
    """Run a command, capturing both streams."""
    out, err = StringIO(), StringIO()
    options.setdefault("stdout", out)
    options.setdefault("stderr", err)
    call_command(command, *args, **options)
    return out.getvalue(), err.getvalue()


class ReconcileWalletsTests(TestCase):
    """CM-01 to CM-05."""

    def setUp(self):
        self.ctx = make_tenant("reconcile-cmd")
        self.tenant = self.ctx.tenant
        self.plan = make_plan("reconcile-cmd-plan", allocated_credits=1000)
        subscription_service.activate(self.tenant, self.plan)
        wallet_service.grant(
            self.tenant, entry_type=EntryType.MANUAL_TOP_UP, amount=500,
            bucket=Bucket.PURCHASED, reference_id="reconcile-cmd-top-up",
        )

    def drift(self, **fields):
        Wallet.objects.filter(tenant=self.tenant).update(**fields)

    @property
    def wallet(self):
        return Wallet.objects.get(tenant=self.tenant)

    def test_cm01_a_healthy_platform_exits_cleanly(self):
        """CM-01."""
        out, _ = run("reconcile_wallets")

        self.assertIn("reconcile against their ledger", out)

    def test_cm02_drift_is_reported_and_exits_non_zero(self):
        """CM-02: non-zero so CI and the scheduled task both notice."""
        self.drift(allocated_balance=900, balance=1400)

        with self.assertRaises(SystemExit) as caught:
            run("reconcile_wallets")

        self.assertEqual(caught.exception.code, 1)

    def test_cm03_fix_posts_an_auditable_correction(self):
        """CM-03: **the correction is itself a ledger row.**

        Editing ``Wallet.balance`` directly would repair the symptom and destroy the
        only evidence that it was ever wrong.
        """
        self.drift(allocated_balance=900, balance=1400)

        # Still exits non-zero: drift *was* found, and the run should not report a
        # clean bill of health just because it also repaired it.
        with self.assertRaises(SystemExit):
            run("reconcile_wallets", fix=True, reason="TICKET-1234")

        wallet = self.wallet
        self.assertEqual(wallet.balance, 1500)
        correction = CreditLedger.all_objects.get(
            tenant=self.tenant, entry_type=EntryType.ADMIN_ADJUSTMENT
        )
        self.assertEqual(correction.amount, 100)
        self.assertIn("TICKET-1234", correction.notes)
        self.assertTrue(correction.reference_id.startswith("reconcile:"))

    def test_cm04_fix_without_a_reason_still_works(self):
        """CM-04: an operator repairing an outage at 3am should not be blocked by a
        missing ticket number."""
        self.drift(allocated_balance=900, balance=1400)

        with self.assertRaises(SystemExit):
            run("reconcile_wallets", fix=True)

        self.assertEqual(self.wallet.balance, 1500)
        self.assertTrue(
            CreditLedger.all_objects.filter(
                tenant=self.tenant, entry_type=EntryType.ADMIN_ADJUSTMENT
            ).exists()
        )

    def test_cm05_the_scan_does_not_grow_with_the_number_of_wallets(self):
        """CM-05: **this job is a full table scan by definition.**

        The readable version was three queries per wallet — fine at seventeen,
        unusable at a hundred thousand. The query count must not move when wallets
        are added.
        """
        with CaptureQueriesContext(connection) as small:
            run("reconcile_wallets")
        baseline = len(small.captured_queries)

        for index in range(10):
            other = make_tenant(f"reconcile-cmd-bulk-{index}")
            subscription_service.activate(other, self.plan)

        with CaptureQueriesContext(connection) as large:
            run("reconcile_wallets")

        self.assertEqual(
            len(large.captured_queries), baseline,
            "the reconciliation scan is doing per-wallet queries",
        )


class AuditReadinessTests(TestCase):
    """CM-06 to CM-13."""

    def setUp(self):
        self.plan = make_plan(
            "audit-plan", allocated_credits=1000, cost_per_additional_credit=Decimal("1.00")
        )

    def subscribe(self, name):
        ctx = make_tenant(name)
        return ctx, subscription_service.activate(ctx, self.plan)

    def test_cm06_a_clean_tenant_is_reported_ready(self):
        """CM-06."""
        self.subscribe("audit-clean")

        out, _ = run("audit_billing_readiness")

        self.assertIn("ready for enforcement : 1", out)
        self.assertIn("blocked by findings   : 0", out)

    def test_cm07_ledger_drift_blocks_a_tenant(self):
        """CM-07: **drift means a bug, not a policy decision.** Never enable
        enforcement on a tenant whose own numbers disagree."""
        ctx, _ = self.subscribe("audit-drifted")
        Wallet.objects.filter(tenant=ctx.tenant).update(
            allocated_balance=900, balance=900
        )

        out, _ = run("audit_billing_readiness")

        self.assertIn("LEDGER DRIFT", out)
        self.assertIn("blocked by findings   : 1", out)

    def test_cm08_a_tenant_over_a_limit_is_blocked_with_the_numbers(self):
        """CM-08: **enabling caps on a tenant already over one breaks a working
        account with no warning.** The numbers are what make the decision possible."""
        ctx, _ = self.subscribe("audit-over")
        for _ in range(5):
            make_agent(ctx.tenant)

        out, _ = run("audit_billing_readiness")

        self.assertIn("OVER LIMIT", out)
        self.assertIn("agent 5/2", out)

    def test_cm09_a_wallet_with_no_rate_card_is_blocked(self):
        """CM-09: **this is the finding that mattered.**

        Every tenant on this platform subscribed before ``activate()`` existed, so
        none had a stamped rate card — and every call would have raised
        ``RateNotConfigured``.
        """
        ctx, _ = self.subscribe("audit-unstamped")
        Wallet.objects.filter(tenant=ctx.tenant).update(
            rate_snapshot=None, rate_snapshot_at=None
        )

        out, _ = run("audit_billing_readiness")

        self.assertIn("no rate card stamped", out)
        self.assertIn("blocked by findings   : 1", out)

    def test_cm10_a_tenant_with_no_plan_is_counted_separately(self):
        """CM-10: not ready and not blocked — there is simply nothing to enforce."""
        make_tenant("audit-no-plan")

        out, _ = run("audit_billing_readiness")

        self.assertIn("no active subscription: 1", out)

    def test_cm11_a_plan_missing_a_rate_is_flagged_before_any_tenant_hits_it(self):
        """CM-11: **a missing rate is not a free call** — it raises at consumption
        time. Better learned here than from a tenant whose campaign stopped."""
        holey = make_plan("audit-holey-plan", with_rates=False)
        add_rates(holey, {Operation.CONVERSATION_HIGH_LATENCY: 5})

        out, _ = run("audit_billing_readiness")

        self.assertIn("audit-holey-plan", out)
        self.assertIn("no rate for", out)
        self.assertIn(Operation.CONVERSATION_LOW_LATENCY, out)

    def test_cm12_enable_safe_skips_every_tenant_with_a_finding(self):
        """CM-12: **the whole point of auditing before switching on.**"""
        clean, _ = self.subscribe("audit-enable-clean")
        blocked, _ = self.subscribe("audit-enable-blocked")
        for _ in range(5):
            make_agent(blocked.tenant)

        run("audit_billing_readiness", enable_safe=True)

        clean_settings = TenantSettings.objects.get(tenant=clean.tenant)
        self.assertTrue(clean_settings.credits_enforced)
        self.assertTrue(clean_settings.entitlements_enforced)

        self.assertFalse(
            TenantSettings.objects.filter(
                tenant=blocked.tenant, credits_enforced=True
            ).exists()
        )

    def test_cm13_verbose_shows_usage_for_tenants_with_no_findings(self):
        """CM-13."""
        ctx, _ = self.subscribe("audit-verbose")
        make_agent(ctx.tenant)

        out, _ = run("audit_billing_readiness", verbose=True)

        self.assertIn("agent", out)
        self.assertIn("1/2", out)


class BackfillStampTests(TestCase):
    """CM-14 to CM-18."""

    def setUp(self):
        self.plan = make_plan("backfill-plan", allocated_credits=1000)

    def bare_subscription(self, name="backfill"):
        """A subscription as it existed before ``activate()`` — no wallet state."""
        from onboard.models import Subscription

        ctx = make_tenant(name)
        subscription = Subscription.objects.create(tenant=ctx.tenant, plan=self.plan)
        return ctx, subscription

    def wallet(self, ctx):
        return Wallet.objects.get(tenant=ctx.tenant)

    def test_cm14_an_unstamped_wallet_gets_its_plans_rate_card(self):
        """CM-14: **stamping is configuration.** A wallet should always carry the
        prices of the plan its tenant is on, so this runs by default."""
        ctx, _ = self.bare_subscription("backfill-unstamped")

        out, _ = run("backfill_wallet_stamps")

        wallet = self.wallet(ctx)
        self.assertEqual(wallet.rate_snapshot["plan_code"], self.plan.code)
        self.assertIn("1 stamped", out)

    def test_cm15_an_already_stamped_wallet_is_left_alone(self):
        """CM-15: re-stamping a wallet mid-cycle would silently move a tenant onto
        prices they did not agree to."""
        ctx = make_tenant("backfill-stamped")
        subscription_service.activate(ctx, self.plan)
        before = self.wallet(ctx).rate_snapshot_at

        out, _ = run("backfill_wallet_stamps")

        self.assertIn("1 already had a rate card", out)
        self.assertEqual(self.wallet(ctx).rate_snapshot_at, before)

    def test_cm16_soft_deleted_tenants_are_not_processed(self):
        """CM-16: stamping a wallet nobody can reach only makes the audit harder to
        read — and this database has more deleted tenants than live ones."""
        ctx, _ = self.bare_subscription("backfill-deleted")
        Tenant.objects.filter(pk=ctx.tenant.pk).update(is_deleted=True)

        out, _ = run("backfill_wallet_stamps")

        self.assertIn("0 stamped", out)
        self.assertFalse(Wallet.objects.filter(tenant=ctx.tenant).exists())

    def test_cm17_granting_allocations_is_opt_in_and_happens_once(self):
        """CM-17: **granting the cycle's credits is money**, not data repair.

        Whether a historical subscription is owed an allocation it never received is
        a business decision, so it has to be said out loud — and saying it twice
        must not pay twice.
        """
        ctx, _ = self.bare_subscription("backfill-grant")

        run("backfill_wallet_stamps", grant_allocations=True)
        self.assertEqual(self.wallet(ctx).balance, 1000)

        run("backfill_wallet_stamps", grant_allocations=True)

        self.assertEqual(self.wallet(ctx).balance, 1000)
        self.assertEqual(
            CreditLedger.all_objects.filter(
                tenant=ctx.tenant, entry_type=EntryType.CYCLE_ALLOCATION
            ).count(),
            1,
        )

    def test_cm18_a_dry_run_leaves_the_database_untouched(self):
        """CM-18: the whole reason an operator would run this first."""
        ctx, _ = self.bare_subscription("backfill-dry")

        out, _ = run("backfill_wallet_stamps", grant_allocations=True, dry_run=True)

        self.assertIn("DRY RUN", out)
        self.assertFalse(
            Wallet.objects.filter(
                tenant=ctx.tenant, rate_snapshot__isnull=False
            ).exists()
        )
        self.assertFalse(
            CreditLedger.all_objects.filter(tenant=ctx.tenant).exists()
        )
